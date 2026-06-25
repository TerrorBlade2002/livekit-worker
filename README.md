# LiveKit VTA Worker

Standalone Python worker for the LiveKit VTA flow.

This folder is intentionally separate from the existing webhook app in [retell-vta-webhook/retell-vta-webhook](../retell-vta-webhook/retell-vta-webhook).
Do not merge them into one deploy unless you intentionally want a combined architecture.

## What this worker does
- Connects to LiveKit Cloud as agent `vta-emma`
- Handles inbound SIP-dispatched calls
- Runs a **cascaded streaming pipeline** (Deepgram Flux STT → GPT-5.1 Chat LLM
  → Cartesia Sonic 3.5 TTS), replacing the previous Grok Realtime
  (speech-to-speech) model
- Fetches customer data from the existing Railway webhook server
- Logs dispositions back to the same Railway server
- Sends `call_ended` notifications before disconnecting

## Pipeline (cascaded STT → LLM → TTS)

| Stage             | Default                       | Provider           | Override env |
| ----------------- | ----------------------------- | ------------------ | ------------ |
| VAD               | Silero (prewarmed)            | local              | — |
| STT               | `deepgram/flux-general` (`en`)| Deepgram Flux      | `STT_PROVIDER`, `STT_MODEL`, `STT_LANGUAGE`, `STT_EAGER_EOT_THRESHOLD` |
| LLM               | `openai/gpt-5.1-chat-latest`  | OpenAI             | `LLM_MODEL`, `LLM_REASONING_EFFORT`, `LLM_TEMPERATURE` |
| TTS               | `cartesia/sonic-3.5`          | Cartesia           | `TTS_MODEL`, `TTS_VOICE`, `TTS_LANGUAGE` |
| Turn detection    | `stt` (Flux integrated EOT)   | Deepgram Flux      | `TURN_DETECTION` (`stt`/`model`/`vad`) |
| Interruption      | `adaptive` (barge-in model)   | LiveKit            | `INTERRUPTION_MODE`, `INTERRUPTION_MIN_DURATION`, `INTERRUPTION_MIN_WORDS` |
| Noise cancel (SIP)| Krisp `BVCTelephony`          | LiveKit            | `NOISE_CANCELLATION` (`bvc_telephony`/`bvc`/`off`) |

**Access path — LiveKit Inference (default).** Every stage runs through
[LiveKit Inference](https://docs.livekit.io/agents/models/inference), so the
models are co-located with the agent on LiveKit Cloud (lowest round-trip;
Deepgram Flux even has a Mumbai deployment). Benefits:
- **One key.** No separate Deepgram / Cartesia / AssemblyAI keys — billing and
  rate limits flow through LiveKit Cloud. (Plugin mode below if you want your own.)
- **Adaptive interruption works.** It requires aligned (word-timed) transcripts;
  all Inference STT models provide them.
- **Swappable by one string.** A/B a provider by changing a model id env var.

**Plugin mode (own keys).** Set `STT_BACKEND` / `LLM_BACKEND` / `TTS_BACKEND` =
`plugin` to use the direct provider plugins with your own `DEEPGRAM_API_KEY` /
`OPENAI_API_KEY` / `CARTESIA_API_KEY` / `ASSEMBLYAI_API_KEY`.

**A/B alternatives (already wired):**
- STT: `STT_PROVIDER=assemblyai` → AssemblyAI Universal Streaming (better for
  names, numbers, account IDs).
- LLM: default `openai/gpt-5.1-chat-latest` is non-reasoning (~1.3s TTFT
  measured). `openai/gpt-4.1-mini` is fastest (~0.9s) for the ~870ms first-audio
  budget; `openai/gpt-5.1` adds full reasoning (~2.2s TTFT, tune
  `LLM_REASONING_EFFORT`) — reserve it for escalation, not the live path.

## Latency engineering

Target budget: first audio ≈ 1s, e2e p95 < 1.5s. How we get there:

- **Co-located Inference** — STT/LLM/TTS run in LiveKit Cloud next to the agent,
  not round-tripped to each provider's own API.
- **`PREEMPTIVE_GENERATION=true`** (`AgentSession` default) — the LLM starts
  composing before the user's end-of-turn is confirmed. Mid-call win.
- **Deepgram Flux eager end-of-turn** (`STT_EAGER_EOT_THRESHOLD=0.4`) — lets the
  LLM start before the turn is fully committed; Flux reports ~260 ms p50 EOT.
- **Cartesia Sonic** — ~90 ms first-byte streaming TTS.
- **Customer info fetch fired in parallel** with pipeline construction.
- **Persistent `aiohttp.ClientSession`** shared across Railway calls — saves a
  TLS handshake (~50–150 ms) each.
- **Silero VAD prewarmed once per worker** via `server.setup_fnc` (not per call).
- **`MIN_ENDPOINTING_DELAY=0.4` / `MAX_ENDPOINTING_DELAY=3.0`** — turn-end
  sensitivity (in `stt` mode this adds to the Flux signal).

Per-turn latency is exported to Prometheus: `livekit_llm_ttft_seconds`,
`livekit_tts_ttfb_seconds`, `livekit_transcription_delay_seconds` (STT commit),
`livekit_eou_delay_seconds`, and `livekit_e2e_latency_seconds` (headline).

> **Prompt caching note.** The ~8k-token system prompt weaves the customer's
> name (`{full_name}`) through 40+ scripted lines, so OpenAI's automatic prefix
> caching only covers the small block before the first name occurrence. A larger
> win is available by refactoring the prompt to keep an invariant static prefix
> and append per-call context (name, time, call state) as a suffix — tracked as
> a follow-up, not done here to avoid altering the tuned script.

## Tool-call rigidity

The system prompt is hardened with explicit, concrete tool-call examples
(see [config/system_prompt.md](config/system_prompt.md) — the
"Terminal Tool Rule" section). GPT-5.1 Chat follows tool-call instructions
well, and the post-speech nudge forces `tool_choice="required"` once a closing
marker is detected, giving reliable adherence:

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

Required (default Inference path):
- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`
- `RAILWAY_SERVER_URL`

That's it for the default stack — STT/LLM/TTS bill through LiveKit Inference, so
no provider keys are needed. Provider keys are required only in plugin mode:
- `DEEPGRAM_API_KEY` (if `STT_BACKEND=plugin`)
- `OPENAI_API_KEY` (if `LLM_BACKEND=plugin`)
- `CARTESIA_API_KEY` (if `TTS_BACKEND=plugin`)
- `ASSEMBLYAI_API_KEY` (if `STT_BACKEND=plugin` + `STT_PROVIDER=assemblyai`)

Optional pipeline knobs (sensible defaults — see [.env.example](.env.example)):
- `STT_PROVIDER` (`deepgram_flux` | `assemblyai`), `STT_MODEL`, `STT_LANGUAGE`,
  `STT_EAGER_EOT_THRESHOLD` (default `0.4`)
- `LLM_MODEL` (default `openai/gpt-5.1-chat-latest`), `LLM_REASONING_EFFORT`
  (default `low`, reasoning models only), `LLM_TEMPERATURE` (default `0.4`,
  non-reasoning models)
- `TTS_MODEL` (default `cartesia/sonic-3.5`), `TTS_VOICE`, `TTS_LANGUAGE`
- `TURN_DETECTION` (`stt` | `model` | `vad`), `INTERRUPTION_MODE`
  (`adaptive` | `vad`)
- `NOISE_CANCELLATION` (`bvc_telephony` | `bvc` | `off`)
- `PREEMPTIVE_GENERATION` (default `true`)
- `MIN_ENDPOINTING_DELAY` (default `0.4`) / `MAX_ENDPOINTING_DELAY` (default `3.0`)
- `*_BACKEND` (`inference` | `plugin`) per stage

## Local run

Using `pip`:
```
pip install -r requirements.txt
python agent.py dev      # auto-dispatches to any room (agent console works)
python agent.py start    # production mode (explicit dispatch only)
```

Using `uv`:
```
uv add "livekit-agents[deepgram,openai,cartesia,assemblyai,silero,turn-detector]~=1.5" \
       livekit-plugins-noise-cancellation livekit-api python-dotenv aiohttp
uv run python agent.py dev
```

## Railway deploy
- Source repo: this worker repo only
- Start command: `python agent.py start`
- Variables: at minimum `LIVEKIT_*` and `RAILWAY_SERVER_URL` (default Inference
  path). Add provider keys only if you switch a stage to `*_BACKEND=plugin`.
- The old `XAI_API_KEY` / `GEMINI_API_KEY` env vars are no longer used and can
  be removed.

## Observability quick reference

| Log prefix         | What it tells you                                          |
| ------------------ | ---------------------------------------------------------- |
| `[BOOT]`           | One-time startup info (.env path, dispatch mode)           |
| `[PREWARM]`        | One-time-per-process worker init (vad loaded, key present) |
| `[TTFT:<room>]`    | Per-stage wall clock from entrypoint to opening line       |
| `[METRICS:...]`    | Per-turn pipeline metrics (STT/LLM/TTS/EOU ttft+duration)  |
| `[END_CALL]`       | Final hangup result + total time + SIP identity removed    |
| `[END_CALL_TIMING]`| Per-step timing inside `log_verification`                  |
| `[METRICS]`        | Prometheus exposition mode (scrape port / remote-write)    |

### Prometheus + Grafana metrics

Beyond logs, the agent exports Prometheus metrics (latency, tokens, call
dispositions, reliability) via [`observability.py`](observability.py). Because
the managed Cloud agent has no scrapeable port and scales to zero, it pushes to
Grafana Cloud (`METRICS_MODE=remote_write`); locally it exposes `/metrics`
(`METRICS_MODE=scrape`). Full setup, dashboard, and alerts:
[`../monitoring/README.md`](../monitoring/README.md). Configure via the
`METRICS_*` vars in [.env.example](.env.example).

### Transcripts → Loki

At the end of every call (agent hangup **or** customer drop), the agent ships
the full transcript + a call-summary analytics record to Loki via
[`transcripts.py`](transcripts.py) — available immediately, not on LiveKit's
delayed publish. No-op until `LOKI_PUSH_URL` is set. Setup, LogQL queries, and
Grafana Cloud Loki config: [`../monitoring/README.md` §7](../monitoring/README.md#7-transcripts--loki).
Configure via the `LOKI_*` vars in [.env.example](.env.example).

## Important
The existing webhook repo should continue to own only the Node webhook server in [retell-vta-webhook/retell-vta-webhook](../retell-vta-webhook/retell-vta-webhook).
