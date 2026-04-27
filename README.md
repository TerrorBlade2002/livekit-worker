# LiveKit VTA Worker

Standalone Python worker for the LiveKit VTA flow.

This folder is intentionally separate from the existing webhook app in [retell-vta-webhook/retell-vta-webhook](../retell-vta-webhook/retell-vta-webhook).
Do not merge them into one deploy unless you intentionally want a combined architecture.

## What this worker does
- Connects to LiveKit Cloud as agent `vta-emma`
- Handles inbound SIP-dispatched calls
- Runs **xAI Grok Realtime** end-to-end (single STT+LLM+TTS model — see below)
- Fetches customer data from the existing Railway webhook server
- Logs dispositions back to the same Railway server
- Sends `call_ended` notifications before disconnecting

## Pipeline (Grok Realtime — switched from cascaded)

| Stage | Provider | Default                          | Override env             |
| ----- | -------- | -------------------------------- | ------------------------ |
| Voice | xAI      | `grok-4-1-fast-non-reasoning`    | (hardcoded by xai plugin) |
| Voice (TTS) | xAI | `Ara` (female)                  | `GROK_VOICE` (`Ara`, `Eve`, `Leo`, `Rex`, `Sal`) |
| VAD   | Server-side (xAI Realtime) | —                  | —                        |

**Why end-to-end Realtime instead of cascaded:**
- One model handles STT + LLM + TTS, collapsing per-turn latency from
  ~1-2s aggregate (cascaded) to ~300-600ms TTFT (Realtime).
- Single API key (`XAI_API_KEY`) instead of three.
- Server-side VAD — no Silero load on the worker.

**Trade-off — no chain-of-thought:** the xAI Realtime API currently exposes
only `grok-4-1-fast-non-reasoning`. If you need explicit reasoning for a
particularly tricky branch in the system prompt, switch back to cascaded
with `LLM.with_x_ai(model="grok-4-1-fast-reasoning")` — costs ~300-600ms
extra TTFT per turn but does chain-of-thought.

## Latency engineering

Every stage of the linkback path is timed and logged. Grep Railway logs for
`[TTFT:` to see the full timeline of each call:

```
[TTFT:vta-call-...] +    0.0ms (total=    0.0ms)  __start__
[TTFT:vta-call-...] +    1.2ms (total=    1.2ms)  http session up
[TTFT:vta-call-...] +   89.4ms (total=   90.6ms)  ctx.connect done
[TTFT:vta-call-...] +  142.1ms (total=  232.7ms)  SIP participant resolved
[TTFT:vta-call-...] +    0.3ms (total=  233.0ms)  customer info fetch fired (async)
[TTFT:vta-call-...] +    0.8ms (total=  233.8ms)  realtime model constructed
[TTFT:vta-call-...] +  187.6ms (total=  421.4ms)  customer info ready
[TTFT:vta-call-...] +    0.5ms (total=  421.9ms)  agent constructed
[TTFT:vta-call-...] +  312.0ms (total=  733.9ms)  session.start done (Grok WS connected)
[TTFT:vta-call-...] +   18.4ms (total=  752.3ms)  background audio started
[TTFT:vta-call-...] +    1.1ms (total=  753.4ms)  opening reply queued
[METRICS:RealtimeModelMetrics] ttft=421.0ms duration=2840.2ms req=...
```

Knobs we tune for low TTFT (all overridable via env):
- `AEC_WARMUP_DURATION=0` — default in livekit-agents is 3.0s. SIP audio is
  unidirectional through TCN's gateway so AEC isn't needed. **Single biggest
  TTFT win.**
- `PREEMPTIVE_GENERATION=true` — LLM starts composing before user's turn
  fully ends. **Mid-call latency win** (200-500ms per turn).
- `MIN_ENDPOINTING_DELAY=0.4` / `MAX_ENDPOINTING_DELAY=3.0` — tune turn-end
  detection sensitivity.
- Customer info fetch is **fired in parallel** with model setup (was
  sequential, blocking ~200ms).
- Persistent `aiohttp.ClientSession` shared across Railway calls — saves
  TLS handshake on every request (~50-150ms each).
- SIP participant wait shortened from 30s to 10s (TCN INVITE arrives in
  <1s in normal operation).
- `prewarm_fnc` registered on `WorkerOptions` — runs once per worker process,
  not per call.

## End-call behavior

`log_verification` is the single terminal tool. When called it:
1. Logs the disposition to Railway server (`/log-verification`)
2. Drains any in-flight speech (`context.wait_for_playout()`)
3. Speaks the **deterministic verbatim closing** via `generate_reply` with
   strict instructions
4. Notifies Railway (`/retell-call-ended`)
5. Removes ONLY the SIP participant (`api.room.remove_participant`) — sends
   clean `disconnect_reason=PARTICIPANT_REMOVED` BYE on TCN's leg B
6. TCN sees clean BYE → fires Action OK → Data Dip → Hunt Group

Wrapped in an outer try/except shield so the tool never raises an exception
back to the LLM (which would cause "An internal error has occurred"
ad-libbing in the customer's ear).

## Existing Railway server used by this worker
- `/retell-webhook`
- `/log-verification`
- `/retell-call-ended`

## Files
- [agent.py](agent.py)
- [setup_sip.py](setup_sip.py)
- [requirements.txt](requirements.txt)
- [config/system_prompt.md](config/system_prompt.md)
- [config/opening_line.md](config/opening_line.md)

## Environment variables
Copy [.env.example](.env.example) to `.env` and set:

Required:
- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`
- `XAI_API_KEY` — used for Grok Realtime (STT+LLM+TTS in one model)
- `RAILWAY_SERVER_URL`

Optional (sensible defaults — only set to override):
- `GROK_VOICE` (default `Ara` — also `Eve`, `Leo`, `Rex`, `Sal`)
- `AEC_WARMUP_DURATION` (default `0` — leave at 0 for SIP)
- `PREEMPTIVE_GENERATION` (default `true`)
- `MIN_ENDPOINTING_DELAY` (default `0.4`)
- `MAX_ENDPOINTING_DELAY` (default `3.0`)

## Local run
- `pip install -r requirements.txt`
- `python agent.py start`

For local testing, use:
- `python agent.py dev`

## Railway deploy
Same Railway service as before — just pushes this repo. After deploy:
- Confirm `XAI_API_KEY` is set in Railway env vars
- Old `OPENAI_API_KEY` / `GEMINI_API_KEY` can be removed (no longer used by
  the Realtime path; keep them only if you might switch back to cascaded)
- Old `OPENAI_LLM_MODEL`, `OPENAI_STT_MODEL`, `GEMINI_TTS_MODEL` env vars
  are unused on the Realtime path

## Observability quick reference

| Log prefix         | What it tells you                                          |
| ------------------ | ---------------------------------------------------------- |
| `[TTFT:<room>]`    | Per-stage wall clock from entrypoint to opening reply      |
| `[METRICS:...]`    | Per-turn model metrics (ttft, duration) emitted live       |
| `[END_CALL]`       | Final hangup result + SIP identity removed                 |
| `[END_CALL_TIMING]`| Per-step timing inside `log_verification`                  |
| `[PREWARM]`        | One-time-per-process worker init                           |

## Important
The existing webhook repo should continue to own only the Node webhook server in [retell-vta-webhook/retell-vta-webhook](../retell-vta-webhook/retell-vta-webhook).
