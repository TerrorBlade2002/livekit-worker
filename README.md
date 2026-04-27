# LiveKit VTA Worker

Standalone Python worker for the LiveKit VTA flow.

This folder is intentionally separate from the existing webhook app in [retell-vta-webhook/retell-vta-webhook](../retell-vta-webhook/retell-vta-webhook).
Do not merge them into one deploy unless you intentionally want a combined architecture.

## What this worker does
- Connects to LiveKit Cloud as agent `vta-emma`
- Handles inbound SIP-dispatched calls
- Runs **cascaded xAI Grok pipeline** (STT + reasoning LLM + TTS, all xAI)
- Fetches customer data from the existing Railway webhook server
- Logs dispositions back to the same Railway server
- Sends `call_ended` notifications before disconnecting

## Pipeline (cascaded Grok — switched from Realtime)

| Stage | Provider | Default                       | Override env             |
| ----- | -------- | ----------------------------- | ------------------------ |
| STT   | xAI      | `en` (English-pinned)         | —                        |
| LLM   | xAI      | `grok-4-fast-reasoning`       | `XAI_LLM_MODEL`, `XAI_REASONING_EFFORT`, `XAI_LLM_TEMPERATURE` |
| TTS   | xAI      | `ara` (female)                | `GROK_VOICE` (`ara`, `eve`, `leo`, `rex`, `sal`) |
| VAD   | Silero   | (loaded via `prewarm_fnc`)    | —                        |

**Why cascaded with reasoning instead of Realtime:**
- Realtime exposes only `grok-4-1-fast-non-reasoning` — no chain-of-thought.
  On a long branchy verification prompt (7 end-call statuses), it would
  unreliably call `log_verification` or paraphrase the closing line, which
  broke production handoffs to TCN.
- Chat-completions API supports rigid OpenAI-compatible function calling.
- Reasoning lets the model PLAN the right tool call before speaking.
- Standalone TTS (`xai.TTS`) speaks **literal text** — guaranteed verbatim
  closings, impossible to paraphrase.
- Still single `XAI_API_KEY`.

**Latency cost:** ~300-500ms higher TTFT per turn vs Realtime. For a
verification call where any missed `log_verification` is a customer routed
wrong, that trade is worth it.

## Latency engineering

Every stage of the linkback path is timed and logged. Grep Railway logs for
`[TTFT:` to see the full timeline of each call:

```
[TTFT:vta-call-...] +    0.0ms (total=    0.0ms)  __start__
[TTFT:vta-call-...] +   89.4ms (total=   89.4ms)  ctx.connect done
[TTFT:vta-call-...] +    1.2ms (total=   90.6ms)  http session up
[TTFT:vta-call-...] +  142.1ms (total=  232.7ms)  SIP participant resolved
[TTFT:vta-call-...] +    2.1ms (total=  234.8ms)  customer info fetch fired (async)
[TTFT:vta-call-...] +    1.8ms (total=  236.6ms)  cascaded pipeline constructed
[TTFT:vta-call-...] +  187.6ms (total=  424.2ms)  customer info ready
[TTFT:vta-call-...] +  312.0ms (total=  736.2ms)  session.start done
[TTFT:vta-call-...] +    0.5ms (total=  736.7ms)  opening line spoken
[METRICS:STTMetrics] duration=...ms
[METRICS:LLMMetrics] ttft=421.0ms duration=2840.2ms
[METRICS:TTSMetrics] ttft=180.0ms duration=2200.0ms
```

Knobs we tune for low TTFT (all overridable via env):
- `AEC_WARMUP_DURATION=0` — default in livekit-agents is 3.0s. SIP audio is
  unidirectional through TCN's gateway so AEC isn't needed. **Single biggest
  TTFT win.**
- `PREEMPTIVE_GENERATION=true` — LLM starts composing before user's turn
  fully ends. **Mid-call latency win** (200-500ms per turn).
- `XAI_REASONING_EFFORT=low` — Grok thinks just enough to plan tool calls,
  no longer.
- `MIN_ENDPOINTING_DELAY=0.4` / `MAX_ENDPOINTING_DELAY=3.0` — tune turn-end
  detection sensitivity.
- Customer info fetch is **fired in parallel** with model setup.
- Persistent `aiohttp.ClientSession` shared across Railway calls — saves
  TLS handshake on every request (~50-150ms each).
- SIP participant wait shortened from 30s to 10s.
- `prewarm_fnc` registered on `WorkerOptions` — loads Silero VAD ONCE per
  worker process, not per call.

## Tool-call rigidity

The system prompt is hardened with explicit, concrete tool-call examples
(see [config/system_prompt.md](config/system_prompt.md) — the
"Terminal Tool Rule" section). Combined with `reasoning_effort` and
`tool_choice="auto"` on the LLM and `parallel_tool_calls=False`
(log_verification is single & terminal — never parallel), this gives
reliable adherence:

- The opening line is **hardcoded** in `agent.py` and spoken via
  `session.say()` straight to TTS — the LLM never touches it, can't paraphrase.
- The closing line is in `CLOSING_MESSAGES` dict, spoken via `session.say()`
  inside `log_verification` — same guarantee.
- The LLM only owns the conversation in between.

## End-call behavior

`log_verification` is the single terminal tool. When called it:
1. Logs the disposition to Railway server (`/log-verification`)
2. Drains any in-flight speech (`context.wait_for_playout()`)
3. Speaks the **deterministic verbatim closing** via `session.say()` (TTS-only,
   no LLM)
4. Notifies Railway (`/retell-call-ended`)
5. Removes ONLY the SIP participant (`api.room.remove_participant`) — sends
   clean `disconnect_reason=PARTICIPANT_REMOVED` BYE on TCN's leg B
6. TCN sees clean BYE → fires Action OK → Data Dip → Hunt Group

Wrapped in an outer try/except shield so the tool never raises an exception
back to the LLM.

## Existing Railway server used by this worker
- `/retell-webhook`
- `/log-verification`
- `/retell-call-ended`

## Files
- [agent.py](agent.py)
- [setup_sip.py](setup_sip.py)
- [requirements.txt](requirements.txt)
- [config/system_prompt.md](config/system_prompt.md) — hardened with explicit tool-call examples

## Environment variables
Copy [.env.example](.env.example) to `.env` and set:

Required:
- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`
- `XAI_API_KEY` — single key for STT + LLM + TTS
- `RAILWAY_SERVER_URL`

Optional (sensible defaults — only set to override):
- `XAI_LLM_MODEL` (default `grok-4-fast-reasoning`)
- `XAI_REASONING_EFFORT` (default `low`)
- `XAI_LLM_TEMPERATURE` (default `0.3`)
- `GROK_VOICE` (default `ara` — also `eve`, `leo`, `rex`, `sal`)
- `AEC_WARMUP_DURATION` (default `0` — leave at 0 for SIP)
- `PREEMPTIVE_GENERATION` (default `true`)
- `MIN_ENDPOINTING_DELAY` (default `0.4`)
- `MAX_ENDPOINTING_DELAY` (default `3.0`)

## Local run

Using `pip`:
```
pip install -r requirements.txt
python agent.py dev      # auto-dispatches to any room (agent console works)
python agent.py start    # production mode (explicit dispatch only)
```

Using `uv`:
```
uv add "livekit-agents[openai,xai]~=1.4" "livekit-plugins-silero~=1.4" \
       livekit-api python-dotenv aiohttp
uv run python agent.py dev
```

## Railway deploy
- Source repo: this worker repo only
- Start command: `python agent.py start`
- Variables: at minimum `XAI_API_KEY`, `LIVEKIT_*`, `RAILWAY_SERVER_URL`
- Old `OPENAI_API_KEY`, `GEMINI_API_KEY` env vars can be removed.

## Observability quick reference

| Log prefix         | What it tells you                                          |
| ------------------ | ---------------------------------------------------------- |
| `[BOOT]`           | One-time startup info (.env path, dispatch mode)           |
| `[PREWARM]`        | One-time-per-process worker init (vad loaded, key present) |
| `[TTFT:<room>]`    | Per-stage wall clock from entrypoint to opening line       |
| `[METRICS:...]`    | Per-turn pipeline metrics (STT/LLM/TTS/EOU ttft+duration)  |
| `[END_CALL]`       | Final hangup result + total time + SIP identity removed    |
| `[END_CALL_TIMING]`| Per-step timing inside `log_verification`                  |

## Important
The existing webhook repo should continue to own only the Node webhook server in [retell-vta-webhook/retell-vta-webhook](../retell-vta-webhook/retell-vta-webhook).
