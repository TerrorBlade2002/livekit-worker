"""
VTA Emma — LiveKit Voice Agent (cascaded STT → LLM → TTS pipeline)

Pipeline (all stages streaming, every stage swappable via env):
  - VAD : Silero                (prewarmed once per worker process)
  - STT : Deepgram Flux         (model-integrated turn detection, eager EOT)
          AssemblyAI Universal Streaming as an A/B alternative
  - LLM : OpenAI GPT-5.1 Chat   (critical path — gpt-5.1-chat-latest, no reasoning step)
  - TTS : Cartesia Sonic 3.5    (fast first-byte streaming)
  - Turn detection : Deepgram Flux EOT ("stt") by default; LiveKit TurnDetector
                     and VAD also available
  - Interruption   : LiveKit Adaptive Interruption Handling (barge-in model)
  - Preemptive generation : LLM starts before end-of-turn is confirmed

Access defaults to LiveKit Inference (co-located with the agent on LiveKit
Cloud → lowest latency, single key, guaranteed aligned transcripts for adaptive
interruption). Set STT_BACKEND / LLM_BACKEND / TTS_BACKEND = "plugin" to use the
direct provider plugins with your own API keys instead.

Architecture (TCN 3-way call):
  - TCN leg A: TCN <-> Customer (TCN owns this — never touched here)
  - TCN leg B: TCN <-> LiveKit SIP gateway (SIP participant in this room)
  - LiveKit room: { SIP participant, VTA agent }

End-of-call flow (deterministic):
  1. LLM speaks the closing line (per prompt's CLOSING PROTOCOL)
  2. LLM calls log_verification tool — same turn
  3. Tool: disallow_interruptions
  4. Tool: register ctx.speech_handle.add_done_callback → shutdown agent
     when closing AUDIO finishes (Riley pattern — no fixed sleep,
     no audio cut-off)
  5. Tool: fire Railway HTTP log in parallel (POST returns 200)
  6. Tool: return ""
  7. When closing audio playback completes → callback fires →
     job_ctx.shutdown() → AGENT disconnects from room
  8. SIP participant STAYS in the room (TCN owns it). TCN uses
     Railway's 200 response to decide what to do with leg A
     (transfer to human, hang up, etc.). We never send BYE.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv
from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    RunContext,
    TurnHandlingOptions,
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    cli,
    function_tool,
    get_job_context,
    inference,
    room_io,
)
from livekit.agents.llm import Toolset
from livekit.plugins import silero

# ---------------------------------------------------------------------------
# .env loading — robust against arbitrary cwd
# ---------------------------------------------------------------------------
_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=True)

logger = logging.getLogger("vta-agent")
logger.setLevel(logging.INFO)

# Prometheus observability. Imported AFTER load_dotenv() above so the module
# reads METRICS_* env vars at import time.
import observability  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
RAILWAY_SERVER_URL = os.getenv(
    "RAILWAY_SERVER_URL", "https://virtual-transfer-agent-production.up.railway.app"
)

# ---------------------------------------------------------------------------
# Cascaded pipeline configuration (STT → LLM → TTS). Defaults are the
# production stack; every stage is swappable via env for A/B testing.
# ---------------------------------------------------------------------------
def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float | None) -> float | None:
    v = os.getenv(name, "")
    return float(v) if v.strip() else default


# -- Backends: "inference" (LiveKit Inference) or "plugin" (own provider key) --
STT_BACKEND = os.getenv("STT_BACKEND", "inference").strip().lower()
LLM_BACKEND = os.getenv("LLM_BACKEND", "inference").strip().lower()
TTS_BACKEND = os.getenv("TTS_BACKEND", "inference").strip().lower()

# -- STT: Deepgram Flux primary; AssemblyAI Universal Streaming A/B -----------
STT_PROVIDER = os.getenv("STT_PROVIDER", "deepgram_flux").strip().lower()
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "en")
STT_MODEL = os.getenv("STT_MODEL", "")  # overrides the provider default if set
STT_EAGER_EOT_THRESHOLD = _env_float("STT_EAGER_EOT_THRESHOLD", 0.4)  # Flux only
STT_EOT_THRESHOLD = _env_float("STT_EOT_THRESHOLD", None)             # Flux only

# -- LLM: OpenAI GPT-5.1 Chat on the critical path ---------------------------
# Default is the NON-reasoning gpt-5.1-chat-latest: GPT-5.1-level quality without
# the reasoning latency (~1.3s vs ~2.2s TTFT measured). build_llm() stays
# reasoning-aware — set LLM_MODEL=openai/gpt-5.1 (reasoning) and it sends
# reasoning_effort instead of temperature; openai/gpt-4.1-mini is the fastest
# (~0.9s TTFT) if you need to chase the ~870ms first-audio budget.
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-5.1-chat-latest")  # inference id
LLM_PLUGIN_MODEL = os.getenv("LLM_PLUGIN_MODEL", "gpt-5.1-chat-latest")
LLM_TEMPERATURE = _env_float("LLM_TEMPERATURE", 0.4)  # non-reasoning models
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "low").strip().lower()

# -- TTS: Cartesia Sonic 3.5 -------------------------------------------------
TTS_MODEL = os.getenv("TTS_MODEL", "cartesia/sonic-3.5")  # inference model id
TTS_PLUGIN_MODEL = os.getenv("TTS_PLUGIN_MODEL", "sonic-3")
TTS_VOICE = os.getenv("TTS_VOICE", "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc")
TTS_LANGUAGE = os.getenv("TTS_LANGUAGE", "en")

# -- Turn detection / interruption / preemptive generation -------------------
# "stt" uses Deepgram Flux's integrated end-of-turn model (acoustic + semantic).
# "model" uses LiveKit's audio TurnDetector; "vad" uses VAD-only.
TURN_DETECTION = os.getenv("TURN_DETECTION", "stt").strip().lower()
INTERRUPTION_MODE = os.getenv("INTERRUPTION_MODE", "adaptive").strip().lower()
INTERRUPTION_MIN_DURATION = _env_float("INTERRUPTION_MIN_DURATION", 0.5)
INTERRUPTION_MIN_WORDS = int(os.getenv("INTERRUPTION_MIN_WORDS", "0") or "0")
ENDPOINTING_MODE = os.getenv("ENDPOINTING_MODE", "fixed").strip().lower()
MIN_ENDPOINTING_DELAY = float(os.getenv("MIN_ENDPOINTING_DELAY", "0.4"))
MAX_ENDPOINTING_DELAY = float(os.getenv("MAX_ENDPOINTING_DELAY", "3.0"))
PREEMPTIVE_GENERATION = _env_bool("PREEMPTIVE_GENERATION", True)

# -- Telephony input cleanup: Krisp BVC for SIP audio (optional) -------------
# "bvc_telephony" (default), "bvc", or "off". Degrades gracefully if the plugin
# is unavailable (e.g. not deployed to LiveKit Cloud).
NOISE_CANCELLATION = os.getenv("NOISE_CANCELLATION", "bvc_telephony").strip().lower()

USER_AWAY_TIMEOUT = float(os.getenv("USER_AWAY_TIMEOUT", "10"))
SILENCE_TOTAL_SECONDS = float(os.getenv("SILENCE_TOTAL_SECONDS", "60"))
SILENCE_FOLLOWUP_DELAY = max(1.0, SILENCE_TOTAL_SECONDS - USER_AWAY_TIMEOUT)
SILENCE_PROMPT_TEXT = os.getenv("SILENCE_PROMPT_TEXT", "Are you still there?")

MAX_CALL_DURATION = float(os.getenv("MAX_CALL_DURATION", "300"))
TOOL_NUDGE_DELAY = float(os.getenv("TOOL_NUDGE_DELAY", "0.5"))
FORCE_END_SPEECH_TIMEOUT = float(os.getenv("FORCE_END_SPEECH_TIMEOUT", "10.0"))

# ---------------------------------------------------------------------------
# Dynamic variable defaults (always used for company/address/callback;
# full_name is the only one that can be overridden by Railway webhook)
# ---------------------------------------------------------------------------
DEFAULT_FULL_NAME = os.getenv("DEFAULT_FULL_NAME", "the customer")
DEFAULT_COMPANY_NAME = os.getenv("DEFAULT_COMPANY_NAME", "our company")
DEFAULT_COMPANY_ADDRESS = os.getenv("DEFAULT_COMPANY_ADDRESS", "")
DEFAULT_CALLBACK_NUMBER = os.getenv("DEFAULT_CALLBACK_NUMBER", "")

CONFIG_DIR = Path(__file__).parent / "config"

SYSTEM_CLOSING_OTHER = (
    "I apologize if this call caused any inconvenience. Thank you for your time — "
    "our representatives may try again later or contact you regarding the matter. Goodbye."
)

SYSTEM_CLOSING_MESSAGES = {
    "verified": (
        "Thank you. So, we're calling regarding a personal business matter of yours. "
        "Please hold for a moment while I transfer you to our representative who can assist you further."
    ),
    "customer_wants_human": (
        "Of course. Please hold for a moment while I connect you to an agent to assist you further."
    ),
    "wrong_number": (
        "I'm so sorry for the confusion. I'll go ahead and update our records so you won't get "
        "any more calls from us. Goodbye."
    ),
    "third_party_end": "Thank you for your time. Have a nice day!",
    "consumer_busy_end": "Thank you for your time. Have a great day!",
    "dnc": (
        "I'm so sorry to bother you. I'll go ahead and update our records right now so you don't "
        "get any more calls from us. Goodbye."
    ),
    "other": SYSTEM_CLOSING_OTHER,
}

TERMINAL_SPEECH_MARKERS = (
    # Transfer / connect markers (verified, customer_wants_human)
    "while i transfer",
    "while i'll transfer",
    "while i connect",
    "transfer you to",
    "transferring you",
    "connect you to",
    "connect you with",
    "connecting you",
    # End-of-call goodbyes
    "have a nice day",
    "have a great day",
    "have a good day",
    "have a wonderful day",
    "have a good evening",
    "have a good one",
    "goodbye",
    "bye-bye",
    "bye bye",
    "take care",
    # Record-update / wrong-number / DNC markers
    "won't get any more calls",
    "don't get any more calls",
    "won't bother you",
    "remove your number",
    "remove you from",
    "update our records",
    # Refusal / hostile
    "end the call here",
    "end the call now",
    "thank you for your time",
    "appreciate your time",
    "appreciate your help",
    "appreciate it",
    # Bereavement / medical
    "sorry to bother",
    "sorry for bothering",
    "sorry for the inconvenience",
    "handle this on our end",
    "make a note",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    return digits[-10:] if len(digits) >= 10 else digits


def looks_like_terminal_agent_speech(text: str | None) -> bool:
    if not text:
        return False
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    return any(marker in normalized for marker in TERMINAL_SPEECH_MARKERS)


def get_est_time() -> str:
    est = timezone(timedelta(hours=-5))
    return datetime.now(est).strftime("%I:%M %p EST, %A %B %d, %Y")


def load_prompt(filename: str) -> str:
    return (CONFIG_DIR / filename).read_text(encoding="utf-8")


# Per-call dynamic variables, in a stable order. Everything that varies from
# call to call lives ONLY in the appended CALL CONTEXT suffix (below) — never in
# the static prompt body — so the OpenAI backend can prompt-cache the large
# byte-identical prefix (the whole of system_prompt.md) across calls.
CALL_CONTEXT_KEYS = (
    "full_name", "company_name", "company_address", "call_back_number", "current_time",
)


def build_call_context(variables: dict[str, str]) -> str:
    """Render the compact, per-call suffix appended AFTER the static prompt body.

    The static body keeps every reference as a literal ``{token}`` placeholder, so
    it never changes between calls. This block maps those tokens to this call's
    real values. Because the values live in the suffix, the whole prompt prefix
    stays byte-identical across calls and remains warm in the provider prompt
    cache — verify the rising hit rate via the
    ``livekit_llm_prompt_cached_tokens_total`` Prometheus metric.
    """
    lines = [
        "",
        "---",
        "",
        "## CALL CONTEXT",
        "Real values for the placeholder tokens used throughout the instructions "
        "above, for THIS call only. Wherever a token appears, speak the value shown "
        'here — never the token, its braces, or the word "placeholder."',
    ]
    for key in CALL_CONTEXT_KEYS:
        lines.append(f"- `{{{key}}}` = {variables.get(key, '')}")
    return "\n" + "\n".join(lines) + "\n"


def extract_phone_from_participant(p: rtc.RemoteParticipant) -> str:
    attrs = p.attributes or {}
    metadata = {}
    if p.metadata:
        try:
            metadata = json.loads(p.metadata)
        except (json.JSONDecodeError, TypeError):
            pass
    for candidate in [
        attrs.get("sip.phoneNumber", ""),
        attrs.get("phone", ""),
        attrs.get("customer_phone", ""),
        metadata.get("phone", ""),
        metadata.get("caller_id", ""),
        p.identity or "",
    ]:
        phone = normalize_phone(candidate)
        if len(phone) == 10:
            return phone
    return ""


def find_primary_sip_participant(
    room: rtc.Room, preferred_identity: str = "",
) -> rtc.RemoteParticipant | None:
    participants = list(room.remote_participants.values())
    if preferred_identity:
        for p in participants:
            if p.identity == preferred_identity and p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                return p
    sip = [p for p in participants if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP]
    if not sip:
        return None
    sip.sort(key=lambda p: (
        0 if ((p.attributes or {}).get("sip.callStatus", "") or "").lower() == "active" else 1,
        0 if extract_phone_from_participant(p) else 1,
    ))
    return sip[0]


# ---------------------------------------------------------------------------
# HTTP helpers — Railway server
# ---------------------------------------------------------------------------
async def fetch_full_name(phone: str, http: aiohttp.ClientSession) -> str:
    """Fetch full_name from Railway. Returns empty string on failure."""
    normalized = normalize_phone(phone)
    payload = {"call_inbound": {"from_number": f"+1{normalized}"}}
    try:
        async with http.post(
            f"{RAILWAY_SERVER_URL}/retell-webhook", json=payload,
            timeout=aiohttp.ClientTimeout(total=2),
        ) as resp:
            if resp.status != 200:
                return ""
            data = await resp.json()
        inbound = data.get("call_inbound") or {}
        dynvars = inbound.get("dynamic_variables") or {}
        meta = inbound.get("metadata") or {}
        name = dynvars.get("full_name") or meta.get("full_name") or ""
        logger.info(f"Railway full_name for {normalized}: {name!r}")
        return name
    except Exception as e:
        logger.warning(f"fetch_full_name failed: {e}")
        return ""


async def _fire_railway_log(
    phone: str, status: str, summary: str, full_name: str,
    call_started_at: float, room_name: str, http: aiohttp.ClientSession,
) -> None:
    """Fire Railway logging calls. Runs as a background task — failures are logged, not raised."""
    normalized = normalize_phone(phone)
    # 1. Log verification
    try:
        async with http.post(
            f"{RAILWAY_SERVER_URL}/log-verification",
            json={"args": {"status": status, "summary": summary, "full_name": full_name},
                  "call": {"from_number": f"+1{normalized}"}},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            logger.info(f"log-verification for {normalized}: HTTP {resp.status}")
    except Exception as e:
        logger.error(f"log-verification failed: {e}")

    # 2. Notify call ended
    duration_ms = max(0, int((time.monotonic() - call_started_at) * 1000))
    try:
        async with http.post(
            f"{RAILWAY_SERVER_URL}/retell-call-ended",
            json={"event": "call_ended", "call": {
                "call_id": room_name,
                "from_number": f"+1{normalized}" if normalized else "",
                "duration_ms": duration_ms,
                "disconnection_reason": f"agent_end_call:{status}:tool",
            }},
            timeout=aiohttp.ClientTimeout(total=3),
        ) as resp:
            logger.info(f"call_ended for {normalized}: HTTP {resp.status}")
    except Exception as e:
        logger.error(f"call_ended failed: {e}")


# ---------------------------------------------------------------------------
# LogVerificationTool — deterministic end-call (no callbacks)
#
# The tool BLOCKS until the SIP leg is gone. By the time it returns "",
# the participant is removed and the job is shutting down. Even if the
# model tries to generate a follow-up turn, there's no audio track to
# send it on — the call is already over.
#
# Sequence inside the tool:
#   1. disallow_interruptions
#   2. asyncio.sleep(1.5) — fixed delay for closing audio to stream
#   3. remove SIP participant — BYE to TCN (actual call end)
#   4. fire railway logging (non-blocking)
#   5. job_ctx.shutdown()
#   6. return ""
#
# IMPORTANT: Do NOT call session.aclose() here — deadlocks because
# the session is waiting for this tool to return.
# ---------------------------------------------------------------------------
_LOG_VERIFICATION_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "log_verification",
    "description": (
        "Log the disposition status before ending the call along with a brief description of "
        "what happened and the reason for disposing of a particular status, then immediately end "
        "the call. This is the ONLY way to end the call — always call this AFTER speaking the "
        "closing line, never before. Do not produce any further speech once this is called."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Disposition based on conversation situation",
                "enum": [
                    "verified", "wrong_number", "third_party_end",
                    "consumer_busy_end", "dnc", "customer_wants_human", "other",
                ],
            },
            "summary": {
                "type": "string",
                "description": (
                    "Brief description of what happened on the call, including if any callback "
                    "information exchanges like call back number or time provided by the consumer, "
                    "and the reason for the disposition status."
                ),
            },
            "full_name": {
                "type": "string",
                "description": "Customer's name",
            },
        },
        "required": ["status"],
    },
}


class LogVerificationTool(Toolset):
    """Logs disposition to Railway, then disconnects the agent when the
    closing-phrase audio finishes playing. SIP participant is left in
    the room — TCN owns its leg and tears it down based on Railway's
    disposition response.
    """

    def __init__(
        self, *, phone: str, full_name: str, call_started_at: float,
        sip_identity: str = "", http: aiohttp.ClientSession,
    ) -> None:
        tool = function_tool(self._log_verification, raw_schema=_LOG_VERIFICATION_SCHEMA)
        super().__init__(id="log_verification", tools=[tool])
        self._phone = phone
        self._full_name = full_name
        self._call_started_at = call_started_at
        self._sip_identity = sip_identity  # kept for legacy callers; not used to remove SIP
        self._http = http
        self._ending = False

    async def _log_verification(
        self, raw_arguments: dict[str, object], ctx: RunContext,
    ) -> str:
        if self._ending:
            observability.record_tool_call("log_verification", "duplicate")
            return ""
        self._ending = True

        status = str(raw_arguments.get("status", "other"))
        summary = str(raw_arguments.get("summary", ""))
        # Per-call values live as {tokens} in the prompt body now (see
        # build_call_context); if the model echoes an unsubstituted token or
        # omits the name, fall back to the authoritative name on file.
        full_name = str(raw_arguments.get("full_name") or "").strip()
        if not full_name or "{" in full_name:
            full_name = self._full_name

        logger.info(f"[END_CALL] status={status} phone={self._phone} summary={summary!r}")
        observability.record_tool_call("log_verification", "ok")
        try:
            observability.note_status(ctx.session, status)
        except Exception:
            pass

        # 1. Lock out user interruptions so closing audio plays cleanly.
        try:
            ctx.disallow_interruptions()
        except Exception:
            pass

        # 2. Register: when the closing-phrase AUDIO finishes playing,
        #    disconnect the AGENT only. SIP participant is left in the
        #    room — TCN owns its leg and will handle it based on the
        #    Railway disposition response (200 → transfer, 204 → hangup).
        #
        #    speech_handle.add_done_callback fires when the model's turn
        #    is complete, which means the closing audio has finished
        #    streaming. No fixed sleep — the audio is never cut off, and
        #    we disconnect IMMEDIATELY after it completes.
        job_ctx = get_job_context()
        room_name = (job_ctx.room.name if job_ctx.room else "") or ""
        ending_status = status

        def _on_speech_done(_handle) -> None:
            logger.info(f"[END_CALL] closing audio done — disconnecting agent (status={ending_status})")
            try:
                job_ctx.shutdown(reason=f"agent_end_call:{ending_status}")
            except Exception as e:
                logger.warning(f"[END_CALL] shutdown failed: {e}")

        try:
            ctx.speech_handle.add_done_callback(_on_speech_done)
            logger.info("[END_CALL] registered speech_handle done callback")
        except Exception as e:
            # Fallback if speech_handle isn't available — disconnect after
            # a short delay to let audio drain.
            logger.warning(f"[END_CALL] speech_handle callback failed: {e}; using fallback")
            async def _fallback_shutdown() -> None:
                await asyncio.sleep(2.0)
                try:
                    job_ctx.shutdown(reason=f"agent_end_call:{ending_status}")
                except Exception:
                    pass
            asyncio.create_task(_fallback_shutdown())

        # 3. POST disposition to Railway in parallel with the audio.
        #    By the time the closing audio finishes (~2-3s), Railway has
        #    responded with 200 (transfer) or 204 (hangup), and TCN has
        #    received the disposition signal. Then the audio-done callback
        #    fires and the agent disconnects.
        asyncio.create_task(_fire_railway_log(
            self._phone, status, summary, full_name,
            self._call_started_at, room_name, self._http,
        ))

        # 4. Return immediately. The audio is still streaming, the model's
        #    turn isn't complete yet, and the disconnect happens via the
        #    speech_handle done callback above.
        return ""



# ---------------------------------------------------------------------------
# VTAAgent
# ---------------------------------------------------------------------------
class VTAAgent(Agent):
    def __init__(self, *, instructions: str, tools: list, full_name: str) -> None:
        super().__init__(instructions=instructions, tools=tools)
        self._full_name = full_name

    async def on_enter(self):
        await self.session.generate_reply(
            instructions=f"Hi, this call is for {self._full_name}.",
            allow_interruptions=False,
        )


# ---------------------------------------------------------------------------
# Pipeline component factories — each stage swappable via env (A/B providers).
# ---------------------------------------------------------------------------
def build_vad(proc: JobProcess | None = None):
    """Silero VAD — reuse the prewarmed instance from the worker process."""
    if proc is not None:
        vad = proc.userdata.get("vad")
        if vad is not None:
            return vad
    return silero.VAD.load()


def build_stt():
    """Speech-to-text. Default: Deepgram Flux (model-integrated turn detection)."""
    if STT_BACKEND == "plugin":
        if STT_PROVIDER == "assemblyai":
            from livekit.plugins import assemblyai

            return assemblyai.STT(model=STT_MODEL or "universal-streaming-english")
        from livekit.plugins import deepgram

        kw: dict = {"model": STT_MODEL or "flux-general-en"}
        if STT_EAGER_EOT_THRESHOLD is not None:
            kw["eager_eot_threshold"] = STT_EAGER_EOT_THRESHOLD
        if STT_EOT_THRESHOLD is not None:
            kw["eot_threshold"] = STT_EOT_THRESHOLD
        return deepgram.STTv2(**kw)

    # LiveKit Inference (default): co-located, single key, aligned transcripts.
    if STT_PROVIDER == "assemblyai":
        return inference.STT(
            model=STT_MODEL or "assemblyai/universal-streaming-english",
            language=STT_LANGUAGE,
        )
    extra: dict = {}
    if STT_EAGER_EOT_THRESHOLD is not None:
        extra["eager_eot_threshold"] = STT_EAGER_EOT_THRESHOLD
    if STT_EOT_THRESHOLD is not None:
        extra["eot_threshold"] = STT_EOT_THRESHOLD
    kw = {"model": STT_MODEL or "deepgram/flux-general", "language": STT_LANGUAGE}
    if extra:
        kw["extra_kwargs"] = extra
    return inference.STT(**kw)


def _is_reasoning_model(model: str) -> bool:
    """OpenAI reasoning models (gpt-5*, o1/o3/o4) take `reasoning_effort` and
    reject `temperature`. The *-chat-latest variants are non-reasoning."""
    m = model.lower().rsplit("/", 1)[-1]
    if "chat" in m:
        return False
    return m.startswith(("gpt-5", "o1", "o3", "o4"))


def build_llm():
    """LLM on the critical path. Default: OpenAI GPT-5.1 (reasoning model).

    Reasoning models take `reasoning_effort` (default "low" for voice latency)
    instead of `temperature`; non-reasoning models take `temperature`. Prompt
    caching is automatic on the OpenAI backend for the repeated static prefix —
    see the livekit_llm_prompt_cached_tokens_total metric to verify hit rate.
    """
    model = LLM_PLUGIN_MODEL if LLM_BACKEND == "plugin" else LLM_MODEL
    params: dict = {}
    if _is_reasoning_model(model):
        if LLM_REASONING_EFFORT:
            params["reasoning_effort"] = LLM_REASONING_EFFORT
    elif LLM_TEMPERATURE is not None:
        params["temperature"] = LLM_TEMPERATURE

    if LLM_BACKEND == "plugin":
        from livekit.plugins import openai

        return openai.LLM(model=model, **params)

    kw = {"model": model}
    if params:
        kw["extra_kwargs"] = params
    return inference.LLM(**kw)


def build_tts():
    """Text-to-speech. Default: Cartesia Sonic 3.5 (fast first-byte streaming)."""
    if TTS_BACKEND == "plugin":
        from livekit.plugins import cartesia

        return cartesia.TTS(model=TTS_PLUGIN_MODEL, voice=TTS_VOICE, language=TTS_LANGUAGE)
    return inference.TTS(model=TTS_MODEL, voice=TTS_VOICE, language=TTS_LANGUAGE)


def resolve_turn_detection():
    """Map TURN_DETECTION to a turn-detection mode for TurnHandlingOptions.

    Default "stt" relies on the STT provider's own end-of-turn model (Deepgram
    Flux / AssemblyAI Universal Streaming). "model" uses LiveKit's audio turn
    detector — exposed as inference.TurnDetector() on newer SDKs, or the
    livekit-plugins-turn-detector model plugin on 1.5.x.
    """
    if TURN_DETECTION == "model":
        if hasattr(inference, "TurnDetector"):
            return inference.TurnDetector()
        try:
            if STT_LANGUAGE.lower().startswith("en"):
                from livekit.plugins.turn_detector.english import EnglishModel

                return EnglishModel()
            from livekit.plugins.turn_detector.multilingual import MultilingualModel

            return MultilingualModel()
        except Exception as e:
            logger.warning(f"turn detector model unavailable, falling back to 'stt': {e}")
            return "stt"
    if TURN_DETECTION in ("stt", "vad", "manual", "realtime_llm"):
        return TURN_DETECTION
    # auto: prefer the STT's integrated endpointing when the provider has one
    return "stt"


def build_noise_cancellation():
    """Krisp background voice cancellation for SIP input (optional, guarded)."""
    if NOISE_CANCELLATION in ("", "off", "none", "false"):
        return None
    try:
        from livekit.plugins import noise_cancellation

        if NOISE_CANCELLATION in ("bvc_telephony", "telephony", "bvctelephony"):
            return noise_cancellation.BVCTelephony()
        return noise_cancellation.BVC()
    except Exception as e:
        logger.warning(f"noise cancellation unavailable ({NOISE_CANCELLATION}): {e}")
        return None


def prewarm(proc: JobProcess) -> None:
    """Load Silero VAD once per worker process — reused across all jobs."""
    proc.userdata["vad"] = silero.VAD.load()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
server = AgentServer()
server.setup_fnc = prewarm

# Start metrics exposition once at process boot (scrape server and/or
# Grafana Cloud remote-write, per METRICS_MODE). Idempotent + non-fatal.
observability.init_exposition()


@server.rtc_session(agent_name=os.getenv("AGENT_NAME", "vta-emma"))
async def entrypoint(ctx: JobContext):
    call_started_at = time.monotonic()
    http_session = aiohttp.ClientSession()

    try:
        # ------------------------------------------------------------------
        # 1. INSTANT: Extract phone from room name (vta-call-<phone>)
        #    This is free — no network, no participant wait.
        # ------------------------------------------------------------------
        phone = ""
        sip_identity = ""
        linked_identity = ""

        # Room name contains phone: vta-call-<digits>
        m = re.search(r"(\d{10,})", ctx.room.name)
        if m:
            phone = normalize_phone(m.group(1))
            logger.info(f"Phone from room name: {phone}")

        # Also check job metadata
        if not phone and ctx.job.metadata:
            try:
                meta = json.loads(ctx.job.metadata)
                phone = normalize_phone(meta.get("phone", "") or meta.get("caller_id", ""))
            except (json.JSONDecodeError, TypeError):
                pass

        # ------------------------------------------------------------------
        # 2. PARALLEL: build pipeline components + fetch full_name from Railway
        #    Both happen while we wait for the SIP participant.
        # ------------------------------------------------------------------
        vad = build_vad(getattr(ctx, "proc", None))
        stt = build_stt()
        llm = build_llm()
        tts = build_tts()

        name_task = asyncio.create_task(fetch_full_name(phone, http_session)) if phone else None

        # ------------------------------------------------------------------
        # 3. SIP participant discovery (short timeout — 3s, not 10s)
        #    We already have the phone from room name; this just gets
        #    the SIP identity for later removal.
        # ------------------------------------------------------------------
        def refresh_sip_context() -> None:
            nonlocal phone, sip_identity, linked_identity
            sip_p = find_primary_sip_participant(ctx.room, preferred_identity=sip_identity)
            if sip_p is not None:
                sip_identity = sip_p.identity or sip_identity
                linked_identity = sip_identity or linked_identity
                extracted = extract_phone_from_participant(sip_p)
                if extracted:
                    phone = extracted
                return
            for p in ctx.room.remote_participants.values():
                if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD:
                    linked_identity = p.identity or linked_identity
                    break

        refresh_sip_context()

        if not sip_identity and not linked_identity:
            participant_ev = asyncio.Event()

            @ctx.room.on("participant_connected")
            def _on_p(p: rtc.RemoteParticipant):
                refresh_sip_context()
                if sip_identity or linked_identity:
                    participant_ev.set()

            try:
                await asyncio.wait_for(participant_ev.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("No SIP participant within 3s — proceeding anyway")

        logger.info(f"Caller phone: {phone} | SIP identity: {sip_identity}")

        # ------------------------------------------------------------------
        # 4. Collect full_name (webhook should be done by now)
        # ------------------------------------------------------------------
        full_name = DEFAULT_FULL_NAME
        if name_task is not None:
            try:
                server_name = await asyncio.wait_for(name_task, timeout=2.0)
                if server_name:
                    full_name = server_name
            except Exception:
                pass

        prompt_vars = {
            "full_name": full_name,
            "company_name": DEFAULT_COMPANY_NAME,
            "company_address": DEFAULT_COMPANY_ADDRESS,
            "call_back_number": DEFAULT_CALLBACK_NUMBER,
            "current_time": get_est_time(),
        }

        logger.info(f"Dynamic vars: full_name={full_name}")

        # ------------------------------------------------------------------
        # Build agent
        #
        # The static system_prompt.md body is sent verbatim (byte-identical
        # across every call) so the OpenAI backend prompt-caches the whole
        # prefix; only the appended CALL CONTEXT suffix carries this call's
        # dynamic values. See build_call_context() and the
        # livekit_llm_prompt_cached_tokens_total metric.
        # ------------------------------------------------------------------
        instructions = load_prompt("system_prompt.md") + build_call_context(prompt_vars)

        log_tool = LogVerificationTool(
            phone=phone, full_name=full_name,
            call_started_at=call_started_at,
            sip_identity=sip_identity, http=http_session,
        )

        vta_agent = VTAAgent(
            instructions=instructions, tools=[log_tool], full_name=full_name,
        )

        # ------------------------------------------------------------------
        # Session
        # ------------------------------------------------------------------
        session = AgentSession(
            vad=vad,
            stt=stt,
            llm=llm,
            tts=tts,
            turn_handling=TurnHandlingOptions(
                turn_detection=resolve_turn_detection(),
                endpointing={
                    "mode": ENDPOINTING_MODE,
                    "min_delay": MIN_ENDPOINTING_DELAY,
                    "max_delay": MAX_ENDPOINTING_DELAY,
                },
                interruption={
                    "mode": INTERRUPTION_MODE,
                    "min_duration": INTERRUPTION_MIN_DURATION,
                    "min_words": INTERRUPTION_MIN_WORDS,
                },
            ),
            # Start LLM generation before the user's end-of-turn is confirmed.
            preemptive_generation=PREEMPTIVE_GENERATION,
            user_away_timeout=USER_AWAY_TIMEOUT,
        )

        nc = build_noise_cancellation()
        audio_in = (
            room_io.AudioInputOptions(noise_cancellation=nc)
            if nc is not None
            else room_io.AudioInputOptions()
        )
        room_opts = room_io.RoomOptions(audio_input=audio_in)
        if linked_identity:
            room_opts = room_io.RoomOptions(
                audio_input=audio_in,
                participant_identity=linked_identity,
            )

        await session.start(agent=vta_agent, room=ctx.room, room_options=room_opts)

        # Attach Prometheus instrumentation to this call (latency, tokens,
        # disposition, active-session gauge, shutdown finalizer).
        observability.instrument_session(
            session,
            ctx,
            model=LLM_MODEL,
            stt_model=(STT_MODEL or STT_PROVIDER),
            tts_model=TTS_MODEL,
        )

        # Update SIP identity after session links
        lp = getattr(getattr(session, "room_io", None), "linked_participant", None)
        if lp is not None and lp.identity:
            if lp.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                sip_identity = lp.identity
                log_tool._sip_identity = sip_identity
            if not phone:
                phone = extract_phone_from_participant(lp)
                log_tool._phone = phone

        # ------------------------------------------------------------------
        # Continuous SIP identity tracker — if SIP joins AFTER the 3s
        # discovery window (or session.start), capture its identity so we
        # can remove it cleanly on end-call.
        # ------------------------------------------------------------------
        @ctx.room.on("participant_connected")
        def _on_late_participant(p: rtc.RemoteParticipant):
            nonlocal sip_identity, phone
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                if not sip_identity:
                    sip_identity = p.identity or ""
                    log_tool._sip_identity = sip_identity
                    logger.info(f"[SIP] late-joined participant captured: {sip_identity}")
                if not phone:
                    extracted = extract_phone_from_participant(p)
                    if extracted:
                        phone = extracted
                        log_tool._phone = phone

        # If SIP already joined silently before we attached this listener,
        # do one final sweep now.
        if not sip_identity:
            sip_p = find_primary_sip_participant(ctx.room)
            if sip_p is not None:
                sip_identity = sip_p.identity or ""
                log_tool._sip_identity = sip_identity
                logger.info(f"[SIP] post-start sweep captured: {sip_identity}")
                if not phone:
                    extracted = extract_phone_from_participant(sip_p)
                    if extracted:
                        phone = extracted
                        log_tool._phone = phone

        # ------------------------------------------------------------------
        # Background audio
        # ------------------------------------------------------------------
        background_audio = None
        try:
            background_audio = BackgroundAudioPlayer(
                ambient_sound=AudioConfig(BuiltinAudioClip.OFFICE_AMBIENCE, volume=0.35),
            )
            await background_audio.start(room=ctx.room, agent_session=session)
        except Exception as e:
            logger.error(f"BackgroundAudioPlayer failed: {e}")
            background_audio = None

        # ------------------------------------------------------------------
        # Silence handling
        # ------------------------------------------------------------------
        silence_state: dict[str, object] = {"warning_said": False, "hangup_task": None}

        async def _silence_hangup(delay: float) -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if log_tool._ending:
                return
            if getattr(session, "user_state", "listening") != "away":
                return
            logger.info(f"[SILENCE] {SILENCE_TOTAL_SECONDS}s — force ending")
            observability.record_forced_end("silence")
            await _force_end_call(session, "other",
                f"Caller silent for {int(SILENCE_TOTAL_SECONDS)}s")

        @session.on("user_state_changed")
        def on_user_state(ev) -> None:
            try:
                if log_tool._ending:
                    return
                ns = getattr(ev, "new_state", None)
                if ns == "away":
                    if silence_state["warning_said"]:
                        return
                    silence_state["warning_said"] = True
                    try:
                        session.say(SILENCE_PROMPT_TEXT, allow_interruptions=True)
                    except Exception:
                        pass
                    silence_state["hangup_task"] = asyncio.create_task(
                        _silence_hangup(SILENCE_FOLLOWUP_DELAY)
                    )
                elif ns in ("speaking", "listening"):
                    t = silence_state["hangup_task"]
                    if t is not None and not t.done():
                        t.cancel()
                    silence_state["hangup_task"] = None
                    silence_state["warning_said"] = False
            except Exception as e:
                logger.exception(f"[SILENCE] handler: {e}")

        # ------------------------------------------------------------------
        # Tool nudge + post-speech force-end
        #
        # Architecture:
        #   - NUDGE: marker-gated. After agent speech, if the text matches a
        #     closing marker, send a strong "call log_verification" instruction
        #     with tool_choice="required". 0.5s delay.
        #   - FORCE-END TIMER: marker-gated. Armed ONLY when a closing marker
        #     matched. Fires silently after 5s if the tool wasn't called.
        #
        # Why marker-gated: arming an unconditional "stuck" timer breaks
        # normal conversation — when the customer takes 12s+ to respond to a
        # mid-call question, we would incorrectly force-end the call. For
        # missed-marker closings, the existing user_away → silence handler
        # (10s "Are you still there?" → 50s silence force-end) is the
        # safety net. Slower but safe.
        # ------------------------------------------------------------------
        TERMINAL_FORCE_END_DELAY = 5.0   # fast path — closing detected

        _nudge_task: asyncio.Task | None = None
        _force_end_timer: asyncio.Task | None = None
        nudge_state: dict[str, bool] = {"last_agent_terminal": False}

        def _cancel_force_end_timer() -> None:
            nonlocal _force_end_timer
            if _force_end_timer is not None and not _force_end_timer.done():
                _force_end_timer.cancel()
            _force_end_timer = None

        def _arm_force_end_timer(delay: float, reason: str) -> None:
            nonlocal _force_end_timer
            _cancel_force_end_timer()

            async def _runner() -> None:
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
                if log_tool._ending:
                    return
                logger.info(
                    f"[FORCE_END] {reason} — {delay}s elapsed, ending silently"
                )
                observability.record_forced_end("terminal_speech")
                # speak=False: agent already said its closing line; do not
                # follow up with another system closing.
                await _force_end_call(
                    session, "other",
                    f"Post-speech force-end ({reason})",
                    speak=False,
                )

            _force_end_timer = asyncio.create_task(_runner())

        @session.on("conversation_item_added")
        def on_conversation_item(ev) -> None:
            try:
                item = getattr(ev, "item", None)
                if getattr(item, "role", None) != "assistant":
                    return
                text_content = getattr(item, "text_content", None)
                text = text_content if isinstance(text_content, str) else ""
                nudge_state["last_agent_terminal"] = looks_like_terminal_agent_speech(text)
                if nudge_state["last_agent_terminal"]:
                    logger.info("[NUDGE] terminal assistant speech detected")
            except Exception:
                pass

        async def _send_nudge() -> None:
            """Marker-gated. Sends a strong 'call the tool' instruction with
            tool_choice='required'. Only fires if a closing marker matched."""
            try:
                await asyncio.sleep(TOOL_NUDGE_DELAY)
            except asyncio.CancelledError:
                return
            if log_tool._ending:
                return
            if getattr(session, "user_state", "listening") == "speaking":
                return
            if time.monotonic() - call_started_at < 5.0:
                return
            if not nudge_state["last_agent_terminal"]:
                return
            logger.info("[NUDGE] prompting model to call log_verification")
            try:
                # tool_choice="required" forces a tool call; since
                # log_verification is our only tool, the model must call it.
                handle = session.generate_reply(
                    instructions=(
                        "You just finished speaking. If that was a "
                        "closing, goodbye, transfer, or farewell "
                        "statement, you MUST call log_verification "
                        "RIGHT NOW with the appropriate status and "
                        "summary. Do NOT speak again — just call "
                        "the function immediately."
                    ),
                    tool_choice="required",
                )

                async def _watch() -> None:
                    try:
                        await asyncio.wait_for(handle.wait_for_playout(), timeout=5.0)
                    except Exception:
                        pass

                asyncio.create_task(_watch())
            except Exception as e:
                logger.warning("[NUDGE] generate_reply failed: %s", e)

        @session.on("agent_state_changed")
        def on_agent_state(ev) -> None:
            nonlocal _nudge_task
            try:
                if log_tool._ending:
                    return
                ns = getattr(ev, "new_state", None)
                if ns == "listening":
                    # Agent finished speaking.
                    #   1. Always send a marker-gated nudge (fast path)
                    #   2. ONLY arm the force-end timer if a closing marker
                    #      matched. Do NOT arm a "stuck" timer for non-marker
                    #      speech — that breaks normal conversation when the
                    #      user takes more than ~12s to respond.
                    #
                    #   For missed closings (model paraphrased and our markers
                    #   don't catch it), the existing user_away → silence
                    #   handler is the safety net (10s "Are you still there?"
                    #   then 50s silence force-end). Slower but safe.
                    if _nudge_task is not None and not _nudge_task.done():
                        _nudge_task.cancel()
                    _nudge_task = asyncio.create_task(_send_nudge())

                    # Skip the very first listening transition (initial opening line)
                    if time.monotonic() - call_started_at < 5.0:
                        return

                    if nudge_state["last_agent_terminal"]:
                        _arm_force_end_timer(
                            TERMINAL_FORCE_END_DELAY,
                            "terminal-speech-detected",
                        )
                    # else: NO force-end armed. Rely on user_away path.

                elif ns in ("speaking", "thinking"):
                    # Agent is actively producing output → conversation alive.
                    # Cancel both nudge and force-end.
                    if _nudge_task is not None and not _nudge_task.done():
                        _nudge_task.cancel()
                        _nudge_task = None
                    _cancel_force_end_timer()
            except Exception:
                pass

        @session.on("user_state_changed")
        def on_user_speech_cancel(ev) -> None:
            """User speaking = conversation alive. Cancel both nudge and timer."""
            nonlocal _nudge_task
            if getattr(ev, "new_state", None) == "speaking":
                if _nudge_task is not None and not _nudge_task.done():
                    _nudge_task.cancel()
                    _nudge_task = None
                _cancel_force_end_timer()

        # ------------------------------------------------------------------
        # Max duration watchdog
        # ------------------------------------------------------------------
        async def _watchdog() -> None:
            try:
                await asyncio.sleep(MAX_CALL_DURATION)
            except asyncio.CancelledError:
                return
            if log_tool._ending:
                return
            logger.info(f"[WATCHDOG] max duration {MAX_CALL_DURATION}s — force ending")
            observability.record_forced_end("max_duration")
            await _force_end_call(session, "other",
                f"Max duration {int(MAX_CALL_DURATION)}s reached")

        watchdog_task = asyncio.create_task(_watchdog())

        # ------------------------------------------------------------------
        # System-driven force_end_call
        # ------------------------------------------------------------------
        async def _speak_force_end_closing(sess: AgentSession, status: str) -> None:
            closing = SYSTEM_CLOSING_MESSAGES.get(status, SYSTEM_CLOSING_OTHER)
            instructions = (
                "Speak exactly and only this closing line to the caller. "
                "Do not call any tools. Do not add extra words. Do not ask a question. "
                f"Closing line: {closing!r}"
            )
            try:
                handle = sess.generate_reply(
                    instructions=instructions,
                    tool_choice="none",
                )
                await asyncio.wait_for(
                    handle.wait_for_playout(),
                    timeout=FORCE_END_SPEECH_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[END_CALL] system closing speech timed out after %.1fs",
                    FORCE_END_SPEECH_TIMEOUT,
                )
            except Exception as e:
                logger.warning("[END_CALL] system closing speech failed: %s", e)
                await asyncio.sleep(min(3.0, FORCE_END_SPEECH_TIMEOUT))

        async def _force_end_call(
            sess: AgentSession, status: str, summary: str, *, speak: bool = True,
        ) -> None:
            """Force-end the call from code.

            speak=True  → speaks a system closing line first (silence/watchdog).
            speak=False → just disconnects (used when the agent already said a
                          closing — no need to follow up with another goodbye).

            We DO NOT remove the SIP participant. TCN owns its leg and will
            tear it down based on the Railway disposition response (200 for
            transfers, 204 for hangups). Only the AGENT disconnects.
            """
            if log_tool._ending:
                return
            log_tool._ending = True
            observability.note_status(sess, status)
            logger.info(f"[END_CALL] system: status={status} speak={speak}")

            try:
                sess.interrupt()
            except Exception:
                pass

            if speak:
                await _speak_force_end_closing(sess, status)

            # Fire Railway logging in background — POST gets 200/204 from
            # Railway, which forwards the disposition to TCN.
            room_name = (ctx.room.name if ctx.room else "") or ""
            asyncio.create_task(_fire_railway_log(
                phone, status, summary, full_name,
                call_started_at, room_name, http_session,
            ))

            # Disconnect AGENT only. SIP participant stays — TCN handles
            # leg B teardown after it acts on the Railway disposition.
            try:
                ctx.shutdown(reason=f"agent_end_call:{status}")
            except Exception:
                pass

        # ------------------------------------------------------------------
        # Cleanup
        # ------------------------------------------------------------------
        async def _cleanup():
            if not watchdog_task.done():
                watchdog_task.cancel()
            if _nudge_task is not None and not _nudge_task.done():
                _nudge_task.cancel()
            if _force_end_timer is not None and not _force_end_timer.done():
                _force_end_timer.cancel()
            t = silence_state.get("hangup_task")
            if t is not None and not t.done():
                t.cancel()
            if background_audio is not None:
                try:
                    await background_audio.aclose()
                except Exception:
                    pass
            try:
                await http_session.close()
            except Exception:
                pass

        ctx.add_shutdown_callback(_cleanup)

        logger.info(
            f"VTA started: {phone} ({full_name}) | "
            f"stt={STT_MODEL or STT_PROVIDER} llm={LLM_MODEL} tts={TTS_MODEL} "
            f"turn={TURN_DETECTION} interrupt={INTERRUPTION_MODE} "
            f"preempt={PREEMPTIVE_GENERATION} | "
            f"silence={USER_AWAY_TIMEOUT}s/{SILENCE_TOTAL_SECONDS}s max={MAX_CALL_DURATION}s"
        )

    except Exception:
        observability.record_error("entrypoint")
        try:
            await http_session.close()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    cli.run_app(server)
