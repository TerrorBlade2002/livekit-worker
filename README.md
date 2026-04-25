# LiveKit VTA Worker

Standalone Python worker for the LiveKit VTA flow.

This folder is intentionally separate from the existing webhook app in [retell-vta-webhook/retell-vta-webhook](../retell-vta-webhook/retell-vta-webhook).
Do not merge them into one deploy unless you intentionally want a combined architecture.

## What this worker does
- Connects to LiveKit Cloud as agent `vta-emma`
- Handles inbound SIP-dispatched calls
- Runs a **cascaded STT → LLM → TTS pipeline** (see below)
- Fetches customer data from the existing Railway webhook server
- Logs dispositions back to the same Railway server
- Sends `call_ended` notifications before disconnecting

## Pipeline (cascaded — switched from OpenAI Realtime)

| Stage | Provider | Default model                  | Override env             |
| ----- | -------- | ------------------------------ | ------------------------ |
| STT   | OpenAI   | `whisper-1` (English-pinned)   | `OPENAI_STT_MODEL`       |
| LLM   | OpenAI   | `gpt-4o-mini`                  | `OPENAI_LLM_MODEL`, `OPENAI_LLM_TEMPERATURE` |
| TTS   | Google   | `gemini-2.5-flash-preview-tts` | `GEMINI_TTS_MODEL`, `GEMINI_VOICE` |
| VAD   | Silero   | (loaded locally)               | —                        |

**Why cascaded:** the Realtime model paraphrased compliance-sensitive closing
lines (DNC, callback number, etc.) even when instructed not to. The Gemini TTS
plugin's default instruction is *"Say the text with a proper tone, don't omit
or add any words"* — so `session.say(closing)` is now reliably verbatim.

**End-call detail:** in `_end_call_sequence` we now call `session.interrupt()`
right before `session.say(closing)`. Cascaded LLMs tend to emit a short
acknowledgment after a tool call ("Okay, thank you...") which would otherwise
be TTS'd in front of our deterministic closing — the interrupt cancels it.

## Existing Railway server used by this worker
This worker is designed to call your already-running webhook server for:
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
- `OPENAI_API_KEY` — used for both STT (Whisper) and LLM
- `GEMINI_API_KEY` — used for Gemini TTS (passed explicitly into the plugin; `GOOGLE_API_KEY` also accepted as a fallback)
- `RAILWAY_SERVER_URL`

Optional knobs (sensible defaults — only set to override):
- `OPENAI_LLM_MODEL` (default `gpt-4o-mini`)
- `OPENAI_LLM_TEMPERATURE` (default `0.7`)
- `OPENAI_STT_MODEL` (default `whisper-1`)
- `GEMINI_TTS_MODEL` (default `gemini-2.5-flash-preview-tts`)
- `GEMINI_VOICE` (default `Aoede` — also good: `Leda`, `Kore`, `Vindemiatrix`, `Achird`)

## Local run
Install dependencies and run the worker:
- `pip install -r requirements.txt`
- `python agent.py start`

For local testing, use:
- `python agent.py dev`

## Railway deploy recommendation
Deploy this folder as a **separate Railway service** from the existing Node webhook app.

Recommended Railway setup:
- Source repo: this worker repo only
- Start command: `python agent.py start`
- Variables: same keys listed above

## GitHub recommendation
Create a new GitHub repo for this folder only, for example:
- `virtual-transfer-agent-worker`

That keeps it separate from the webhook repo and avoids accidental deploy coupling.

## Suggested push flow
From inside this folder:
- `git init`
- `git branch -M main`
- `git add .`
- `git commit -m "Initial LiveKit worker"`
- `git remote add origin <new-worker-repo-url>`
- `git push -u origin main`

## Important
The existing webhook repo should continue to own only the Node webhook server in [retell-vta-webhook/retell-vta-webhook](../retell-vta-webhook/retell-vta-webhook).
