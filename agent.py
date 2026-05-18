"""
VTA Emma — LiveKit Voice Agent (xAI Grok Realtime, non-thinking)

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
    RunContext,
    TurnHandlingOptions,
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    cli,
    function_tool,
    get_job_context,
    room_io,
)
from livekit.agents.llm import Toolset
from livekit.plugins import xai as xai_plugin

# ---------------------------------------------------------------------------
# .env loading — robust against arbitrary cwd
# ---------------------------------------------------------------------------
_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=True)

logger = logging.getLogger("vta-agent")
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
RAILWAY_SERVER_URL = os.getenv(
    "RAILWAY_SERVER_URL", "https://virtual-transfer-agent-production.up.railway.app"
)

GROK_VOICE = os.getenv("GROK_VOICE", "Ara")
GROK_REALTIME_MODEL = os.getenv("GROK_REALTIME_MODEL", "")
GROK_TEMPERATURE = float(os.getenv("GROK_TEMPERATURE", "0.7"))

MIN_ENDPOINTING_DELAY = float(os.getenv("MIN_ENDPOINTING_DELAY", "0.4"))
MAX_ENDPOINTING_DELAY = float(os.getenv("MAX_ENDPOINTING_DELAY", "3.0"))

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


def render_prompt(template: str, variables: dict[str, str]) -> str:
    result = template
    for key, value in variables.items():
        result = result.replace("{" + key + "}", value)
    return result


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
            return ""
        self._ending = True

        status = str(raw_arguments.get("status", "other"))
        summary = str(raw_arguments.get("summary", ""))
        full_name = str(raw_arguments.get("full_name", self._full_name))

        logger.info(f"[END_CALL] status={status} phone={self._phone} summary={summary!r}")

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
# Entrypoint
# ---------------------------------------------------------------------------
server = AgentServer()


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
        # 2. PARALLEL: Build Grok model + fetch full_name from Railway
        #    Both happen while we wait for SIP participant.
        # ------------------------------------------------------------------
        model_kwargs: dict = {"voice": GROK_VOICE}
        if GROK_REALTIME_MODEL:
            model_kwargs["model"] = GROK_REALTIME_MODEL
        try:
            model_kwargs["temperature"] = GROK_TEMPERATURE
            rt_model = xai_plugin.realtime.RealtimeModel(**model_kwargs)
        except TypeError:
            rt_model = xai_plugin.realtime.RealtimeModel(voice=GROK_VOICE)

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
        # ------------------------------------------------------------------
        instructions = render_prompt(load_prompt("system_prompt.md"), prompt_vars)

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
            llm=rt_model,
            turn_handling=TurnHandlingOptions(
                endpointing={
                    "mode": "fixed",
                    "min_delay": MIN_ENDPOINTING_DELAY,
                    "max_delay": MAX_ENDPOINTING_DELAY,
                },
            ),
            user_away_timeout=USER_AWAY_TIMEOUT,
        )

        room_opts = room_io.RoomOptions(audio_input=room_io.AudioInputOptions())
        if linked_identity:
            room_opts = room_io.RoomOptions(
                audio_input=room_io.AudioInputOptions(),
                participant_identity=linked_identity,
            )

        await session.start(agent=vta_agent, room=ctx.room, room_options=room_opts)

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
        # Tool nudge + post-speech force-end (DECOUPLED)
        #
        # Architecture:
        #   - NUDGE: marker-gated. After agent speech, if the text matches a
        #     closing marker, send a strong "call log_verification" instruction
        #     with tool_choice="required". 0.5s delay.
        #   - POST-SPEECH FORCE-END: marker-INDEPENDENT. After agent speech,
        #     ALWAYS arm a timer. Fires silently (no system closing speech)
        #     if no activity happens within the window.
        #       - If marker matched   → 5s  (model should've called tool)
        #       - If marker mismatched → 12s (safety net for paraphrased closings)
        #     Cancelled by: agent speaks, user speaks, or tool is called.
        #
        # Why decoupled: previous design gated the force-end behind the marker
        # check inside the nudge function. If the model paraphrased its closing
        # (e.g., "Please hold while I transfer you" without "for a moment"),
        # the marker missed, the force-end never armed, and the call hung
        # indefinitely waiting for the model to call the tool.
        # ------------------------------------------------------------------
        TERMINAL_FORCE_END_DELAY = 5.0   # fast path — closing detected
        STUCK_FORCE_END_DELAY = 12.0     # safety net — closing not detected

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
                # tool_choice MUST be a string for xAI Realtime
                # (per_response_tool_choice=False). "required" forces a tool
                # call; since log_verification is our only tool, that's it.
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
                    # Agent finished speaking. Two things in parallel:
                    #   1. Nudge (marker-gated) — fast path
                    #   2. Force-end timer (always armed) — safety net
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
                    else:
                        _arm_force_end_timer(
                            STUCK_FORCE_END_DELAY,
                            "post-speech-stuck",
                        )

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
            f"voice={GROK_VOICE} silence={USER_AWAY_TIMEOUT}s/{SILENCE_TOTAL_SECONDS}s "
            f"max={MAX_CALL_DURATION}s"
        )

    except Exception:
        try:
            await http_session.close()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    cli.run_app(server)
