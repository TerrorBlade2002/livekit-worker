"""
latency_probe.py — component latency probe for the VTA cascaded pipeline.

Measures the two biggest *controllable* contributors to first-audio latency
against the live LiveKit Inference gateway, using the exact models configured
in agent.py (LLM_MODEL, TTS_MODEL, etc.):

  * LLM TTFT  — time to first token from the configured LLM, with the real
                ~8k-token system prompt + a representative user turn.
  * TTS TTFB  — time to first audio byte from the configured TTS.

This is NOT a full end-to-end SIP test (no telephony/STT audio path here). STT
commit delay and end-of-utterance delay are measured live on real calls via the
livekit_transcription_delay_seconds / livekit_eou_delay_seconds metrics.

Usage:
    python latency_probe.py            # 3 iterations each (after 1 warmup)
    PROBE_ITERS=5 python latency_probe.py
    PROBE_LLM=0 python latency_probe.py # TTS only
"""

import asyncio
import os
import statistics
import time

os.environ.setdefault("METRICS_ENABLED", "false")  # don't bind the scrape port

import agent  # noqa: E402  (imports configured factories + prompt helpers)
from livekit.agents.llm import ChatContext  # noqa: E402

ITERS = int(os.getenv("PROBE_ITERS", "3"))
RUN_LLM = os.getenv("PROBE_LLM", "1") not in ("0", "false", "no")
RUN_TTS = os.getenv("PROBE_TTS", "1") not in ("0", "false", "no")

USER_TURN = os.getenv("PROBE_USER_TURN", "Yeah, this is speaking.")
TTS_TEXT = os.getenv(
    "PROBE_TTS_TEXT",
    "Thanks — one moment while I pull that up for you.",
)


def _rendered_system_prompt() -> str:
    prompt_vars = {
        "full_name": agent.DEFAULT_FULL_NAME,
        "company_name": agent.DEFAULT_COMPANY_NAME,
        "company_address": agent.DEFAULT_COMPANY_ADDRESS,
        "call_back_number": agent.DEFAULT_CALLBACK_NUMBER,
        "current_time": agent.get_est_time(),
    }
    body = agent.load_prompt("system_prompt.md")
    # Match whichever instruction-assembly the agent currently uses.
    if hasattr(agent, "build_call_context"):
        return body + agent.build_call_context(prompt_vars)
    if hasattr(agent, "render_prompt"):
        return agent.render_prompt(body, prompt_vars)
    return body


def _summarize(name: str, unit: str, samples: list[float]) -> None:
    if not samples:
        print(f"  {name}: no samples")
        return
    ms = [s * 1000 for s in samples]
    p50 = statistics.median(ms)
    print(
        f"  {name}: p50={p50:7.1f}{unit}  min={min(ms):7.1f}{unit}  "
        f"max={max(ms):7.1f}{unit}  n={len(ms)}  [{', '.join(f'{v:.0f}' for v in ms)}]"
    )


async def probe_llm(llm, system_prompt: str) -> None:
    print(f"\n[LLM] model={agent.LLM_MODEL} reasoning_effort={agent.LLM_REASONING_EFFORT}")
    ttfts: list[float] = []
    totals: list[float] = []
    for i in range(ITERS + 1):  # +1 warmup
        chat_ctx = ChatContext.empty()
        chat_ctx.add_message(role="system", content=system_prompt)
        chat_ctx.add_message(role="user", content=USER_TURN)
        t0 = time.perf_counter()
        first = None
        try:
            stream = llm.chat(chat_ctx=chat_ctx)
            async for _chunk in stream:
                if first is None:
                    first = time.perf_counter()
            t1 = time.perf_counter()
        except Exception as e:
            print(f"  iter {i}: ERROR {type(e).__name__}: {e}")
            continue
        if i == 0:
            print(f"  warmup: ttft={ (first - t0)*1000 if first else -1 :.0f}ms")
            continue
        if first is not None:
            ttfts.append(first - t0)
            totals.append(t1 - t0)
    _summarize("TTFT       ", "ms", ttfts)
    _summarize("full reply ", "ms", totals)


async def probe_tts(tts) -> None:
    print(f"\n[TTS] model={agent.TTS_MODEL} voice={agent.TTS_VOICE}")
    ttfbs: list[float] = []
    totals: list[float] = []
    for i in range(ITERS + 1):  # +1 warmup
        t0 = time.perf_counter()
        first = None
        try:
            stream = tts.synthesize(TTS_TEXT)
            async for _ev in stream:
                if first is None:
                    first = time.perf_counter()
            t1 = time.perf_counter()
        except Exception as e:
            print(f"  iter {i}: ERROR {type(e).__name__}: {e}")
            continue
        if i == 0:
            print(f"  warmup: ttfb={ (first - t0)*1000 if first else -1 :.0f}ms")
            continue
        if first is not None:
            ttfbs.append(first - t0)
            totals.append(t1 - t0)
    _summarize("TTFB       ", "ms", ttfbs)
    _summarize("full audio ", "ms", totals)


async def main() -> None:
    print("=" * 70)
    print("VTA cascaded pipeline — component latency probe")
    print(f"iterations={ITERS} (after 1 warmup each)")
    print("=" * 70)

    if RUN_LLM:
        llm = agent.build_llm()
        await probe_llm(llm, _rendered_system_prompt())
    if RUN_TTS:
        tts = agent.build_tts()
        await probe_tts(tts)

    print("\nNote: STT commit / end-of-utterance delay are measured live on real")
    print("calls (livekit_transcription_delay_seconds, livekit_eou_delay_seconds).")


if __name__ == "__main__":
    asyncio.run(main())
