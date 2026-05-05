"""
VTA Emma — LiveKit Voice Agent (xAI Grok Realtime, non-thinking)

Architecture (TCN 3-way call):
  - TCN leg A: TCN <-> Customer (TCN owns this — never touched here)
  - TCN leg B: TCN <-> LiveKit SIP gateway (SIP participant in this room)
  - LiveKit room: { SIP participant, VTA agent }

End-of-call flow (proven Riley pattern — session.shutdown lifecycle):
  1. LLM speaks the closing line (per prompt's CLOSING PROTOCOL)
  2. LLM calls log_verification tool (closing audio still streaming)
  3. Tool: disallow_interruptions + arm speech_handle.done callback
  4. Tool: POST disposition to Railway (data-safe first)
  5. Tool returns "" — model receives empty result on a doomed session
  6. Closing audio finishes → callback fires → session.shutdown()
  7. Session "close" event → remove SIP participant → BYE to TCN
  8. job_ctx.shutdown() → worker cleanup
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
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    CloseEvent,
    JobContext,
    RunContext,
    ToolError,
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    cli,
    function_tool,
    get_job_context,
    room_io,
    utils,
)
from livekit.agents.llm import Toolset
from livekit.agents.voice.speech_handle import SpeechHandle
from livekit.plugins import xai as xai_plugin

# ---------------------------------------------------------------------------
# .env loading — robust against arbitrary cwd
# ---------------------------------------------------------------------------
_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=True)

logger = logging.getLogger("vta-agent")
logger.setLevel(logging.INFO)

logger.info(f"[BOOT] .env path: {_ENV_PATH} (exists={_ENV_PATH.exists()})")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
RAILWAY_SERVER_URL = os.getenv(
    "RAILWAY_SERVER_URL", "https://virtual-transfer-agent-production.up.railway.app"
)

# Grok Realtime model config
GROK_VOICE = os.getenv("GROK_VOICE", "Ara")
GROK_REALTIME_MODEL = os.getenv("GROK_REALTIME_MODEL", "")
GROK_TEMPERATURE = float(os.getenv("GROK_TEMPERATURE", "0.7"))

# AgentSession latency knobs
MIN_ENDPOINTING_DELAY = float(os.getenv("MIN_ENDPOINTING_DELAY", "0.4"))
MAX_ENDPOINTING_DELAY = float(os.getenv("MAX_ENDPOINTING_DELAY", "3.0"))

# Silence handling
USER_AWAY_TIMEOUT = float(os.getenv("USER_AWAY_TIMEOUT", "10"))
SILENCE_TOTAL_SECONDS = float(os.getenv("SILENCE_TOTAL_SECONDS", "60"))
SILENCE_FOLLOWUP_DELAY = max(1.0, SILENCE_TOTAL_SECONDS - USER_AWAY_TIMEOUT)
SILENCE_PROMPT_TEXT = os.getenv("SILENCE_PROMPT_TEXT", "Are you still there?")

# Max call duration — hard cap
MAX_CALL_DURATION = float(os.getenv("MAX_CALL_DURATION", "300"))

# Tool nudge delay — fallback when model speaks closing but forgets to call tool
TOOL_NUDGE_DELAY = float(os.getenv("TOOL_NUDGE_DELAY", "1.5"))

# ---------------------------------------------------------------------------
# Dynamic variable defaults — overridden by Railway webhook response
# ---------------------------------------------------------------------------
DEFAULT_FULL_NAME = os.getenv("DEFAULT_FULL_NAME", "the customer")
DEFAULT_COMPANY_NAME = os.getenv("DEFAULT_COMPANY_NAME", "our company")
DEFAULT_COMPANY_ADDRESS = os.getenv("DEFAULT_COMPANY_ADDRESS", "")
DEFAULT_CALLBACK_NUMBER = os.getenv("DEFAULT_CALLBACK_NUMBER", "")

CONFIG_DIR = Path(__file__).parent / "config"

OPENING_LINE_TEMPLATE = "Hi, this call is for {full_name}."

# System closing for force_end_call (silence timeout, max duration)
SYSTEM_CLOSING_OTHER = (
    "I apologize if this call caused any inconvenience. Thank you for your time — "
    "our representatives may try again later or contact you regarding the matter. Goodbye."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_phone(raw: str) -> str:
    """Normalize phone to last 10 digits."""
    digits = re.sub(r"\D", "", raw)
    return digits[-10:] if len(digits) >= 10 else digits


def get_est_time() -> str:
    """Return current time in EST as a readable string."""
    est = timezone(timedelta(hours=-5))
    now = datetime.now(est)
    return now.strftime("%I:%M %p EST, %A %B %d, %Y")


def load_prompt(filename: str) -> str:
    """Read a prompt template from the config directory."""
    path = CONFIG_DIR / filename
    return path.read_text(encoding="utf-8")


def render_prompt(template: str, variables: dict[str, str]) -> str:
    """Replace {variable} placeholders in the prompt template."""
    result = template
    for key, value in variables.items():
        result = result.replace("{" + key + "}", value)
    return result


def extract_phone_from_participant(participant: rtc.RemoteParticipant) -> str:
    """Extract phone number from SIP participant attributes/metadata."""
    attrs = participant.attributes or {}
    metadata = {}
    if participant.metadata:
        try:
            metadata = json.loads(participant.metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}

    candidates = [
        attrs.get("sip.phoneNumber", ""),
        attrs.get("phone", ""),
        attrs.get("customer_phone", ""),
        metadata.get("phone", ""),
        metadata.get("caller_id", ""),
        participant.identity or "",
    ]

    for candidate in candidates:
        phone = normalize_phone(candidate)
        if len(phone) == 10:
            return phone
    return ""


def find_primary_sip_participant(
    room: rtc.Room,
    preferred_identity: str = "",
) -> rtc.RemoteParticipant | None:
    """Pick the customer-facing SIP leg."""
    participants = list(room.remote_participants.values())

    if preferred_identity:
        for p in participants:
            if p.identity == preferred_identity and p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                return p

    sip_participants = [
        p for p in participants
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
    ]
    if not sip_participants:
        return None

    def rank(p: rtc.RemoteParticipant) -> tuple[int, int]:
        status = ((p.attributes or {}).get("sip.callStatus", "") or "").lower()
        return (0 if status == "active" else 1, 0 if extract_phone_from_participant(p) else 1)

    sip_participants.sort(key=rank)
    return sip_participants[0]


# ---------------------------------------------------------------------------
# HTTP helpers — Railway server integration
# ---------------------------------------------------------------------------
async def fetch_customer_info(phone: str, http: aiohttp.ClientSession) -> dict:
    """Call Railway /retell-webhook to look up customer data."""
    normalized = normalize_phone(phone)
    payload = {"call_inbound": {"from_number": f"+1{normalized}"}}
    timeout = aiohttp.ClientTimeout(total=3)
    try:
        async with http.post(
            f"{RAILWAY_SERVER_URL}/retell-webhook", json=payload, timeout=timeout
        ) as resp:
            if resp.status != 200:
                logger.warning(f"Webhook returned {resp.status} for {normalized}")
                return {}
            data = await resp.json()
        logger.info(f"Customer info for {normalized}: {data}")
        inbound = data.get("call_inbound") or {}
        dynvars = inbound.get("dynamic_variables") or {}
        meta = inbound.get("metadata") or {}
        return {**dynvars, **meta}
    except Exception as e:
        logger.error(f"Error fetching customer info: {e}")
        return {}


async def log_verification_to_server(
    phone: str,
    status: str,
    summary: str,
    full_name: str,
    http: aiohttp.ClientSession,
) -> dict:
    """POST disposition to Railway /log-verification."""
    normalized = normalize_phone(phone)
    payload = {
        "args": {"status": status, "summary": summary, "full_name": full_name},
        "call": {"from_number": f"+1{normalized}"},
    }
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with http.post(
            f"{RAILWAY_SERVER_URL}/log-verification", json=payload, timeout=timeout
        ) as resp:
            data = await resp.json()
        logger.info(f"Log verification response for {normalized}: {data}")
        return data
    except Exception as e:
        logger.error(f"Error logging verification: {e}")
        return {"error": str(e)}


async def notify_call_ended(
    phone: str,
    call_id: str,
    duration_ms: int,
    disconnection_reason: str,
    http: aiohttp.ClientSession,
) -> None:
    """Fire /retell-call-ended for disposition enrichment."""
    normalized = normalize_phone(phone)
    payload = {
        "event": "call_ended",
        "call": {
            "call_id": call_id,
            "from_number": f"+1{normalized}" if normalized else "",
            "duration_ms": duration_ms,
            "disconnection_reason": disconnection_reason,
        },
    }
    timeout = aiohttp.ClientTimeout(total=3)
    try:
        async with http.post(
            f"{RAILWAY_SERVER_URL}/retell-call-ended", json=payload, timeout=timeout
        ) as resp:
            logger.info(f"call_ended notify for {normalized}: HTTP {resp.status}")
    except Exception as e:
        logger.error(f"Error notifying call_ended: {e}")


# ---------------------------------------------------------------------------
# LogVerificationTool — proven Riley shutdown lifecycle
#
# Key insight: session.shutdown() is what KILLS the LLM, preventing it from
# generating follow-up speech after the tool returns. The old approach only
# removed the SIP participant but left the session alive — model would then
# produce a second "thank you, I'll update…" turn.
#
# Lifecycle:
#   1. Arm speech_handle.done callback → session.shutdown()
#   2. Register session "close" handler → SIP removal + job shutdown
#   3. POST to Railway (data-safe)
#   4. Return "" — model gets result but session is doomed
#   5. Speech finishes → callback fires → session dies → close handler fires
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
                    "verified",
                    "wrong_number",
                    "third_party_end",
                    "consumer_busy_end",
                    "dnc",
                    "customer_wants_human",
                    "other",
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
    """Unified tool: logs disposition to Railway, then ends the call.

    Mirrors the shutdown lifecycle of Riley's MarkDispositionAndEndCallTool:
    speech finishes → session.shutdown() → SIP removal → job shutdown.
    """

    def __init__(
        self,
        *,
        phone: str,
        full_name: str,
        call_started_at: float,
        sip_identity: str = "",
        http: aiohttp.ClientSession,
    ) -> None:
        tool = function_tool(
            self._log_verification,
            raw_schema=_LOG_VERIFICATION_SCHEMA,
        )
        super().__init__(id="log_verification", tools=[tool])

        self._phone = phone
        self._full_name = full_name
        self._call_started_at = call_started_at
        self._sip_identity = sip_identity
        self._http = http
        self._ending = False
        self._call_end_notified = False

    async def _log_verification(
        self, raw_arguments: dict[str, object], ctx: RunContext
    ) -> str:
        """Log disposition and end the call. No further speech after this."""

        if self._ending:
            logger.warning("[END_CALL] duplicate tool call — ignored")
            return ""
        self._ending = True

        ctx.disallow_interruptions()

        # ------------------------------------------------------------------
        # ARM SHUTDOWN: when closing-line audio finishes → kill the session.
        # This is the key that prevents the model from speaking again: once
        # session.shutdown() fires, the Realtime WS is closed and no new
        # generation can happen.
        # ------------------------------------------------------------------
        def _on_speech_done(_: SpeechHandle) -> None:
            logger.info("[END_CALL] closing audio done — shutting down session")
            ctx.session.shutdown()

        ctx.speech_handle.add_done_callback(_on_speech_done)

        # When the session closes, handle SIP removal + job shutdown.
        ctx.session.once("close", self._on_session_close)

        # ------------------------------------------------------------------
        # POST disposition to Railway (data-safe: logged before teardown).
        # Even if the callback never fires, the data is persisted.
        # ------------------------------------------------------------------
        status = str(raw_arguments.get("status", "other"))
        summary = str(raw_arguments.get("summary", ""))
        full_name = str(raw_arguments.get("full_name", self._full_name))
        self._full_name = full_name or self._full_name

        logger.info(
            f"[END_CALL] tool: status={status} phone={self._phone} "
            f"summary={summary!r} full_name={full_name!r}"
        )

        try:
            await log_verification_to_server(
                self._phone, status, summary, self._full_name, http=self._http
            )
        except Exception as e:
            logger.error(f"[END_CALL] log_verification_to_server failed: {e}")

        # Notify call ended
        if not self._call_end_notified:
            self._call_end_notified = True
            job_ctx = get_job_context()
            room_name = (job_ctx.room.name if job_ctx.room else "") or ""
            duration_ms = max(0, int((time.monotonic() - self._call_started_at) * 1000))
            try:
                await notify_call_ended(
                    phone=self._phone,
                    call_id=room_name,
                    duration_ms=duration_ms,
                    disconnection_reason=f"agent_end_call:{status}:tool",
                    http=self._http,
                )
            except Exception as e:
                logger.error(f"[END_CALL] notify_call_ended failed: {e}")

        # ------------------------------------------------------------------
        # SAFETY NET: if speech_handle.done never fires (e.g., Realtime WS
        # disconnect), force shutdown after 10s so the call doesn't hang.
        # ------------------------------------------------------------------
        async def _safety_timeout() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return
            logger.warning("[END_CALL] safety timeout — forcing session shutdown")
            try:
                ctx.session.shutdown()
            except Exception:
                pass

        asyncio.create_task(_safety_timeout())

        # Return empty — model sees no content to follow up on.
        return ""

    def _on_session_close(self, ev: CloseEvent) -> None:
        """Fires when AgentSession shuts down. Remove SIP participant + exit job."""
        job_ctx = get_job_context()

        async def _on_shutdown() -> None:
            """Remove SIP participant (clean BYE to TCN), then let job exit."""
            room_name = (job_ctx.room.name if job_ctx.room else "") or ""

            # Try to remove just the SIP participant for a clean BYE
            if room_name and self._sip_identity:
                try:
                    from livekit import api
                    await job_ctx.api.room.remove_participant(
                        api.RoomParticipantIdentity(
                            room=room_name,
                            identity=self._sip_identity,
                        )
                    )
                    logger.info(
                        f"[TEARDOWN] SIP participant {self._sip_identity} removed "
                        f"from {room_name} — BYE sent to TCN"
                    )
                    return
                except Exception as e:
                    logger.warning(f"[TEARDOWN] remove_participant failed: {e}")

            # Fallback: delete the entire room (also sends BYE)
            if room_name:
                try:
                    logger.info(f"[TEARDOWN] fallback: deleting room {room_name}")
                    await job_ctx.delete_room()
                except Exception as e:
                    logger.error(f"[TEARDOWN] delete_room failed: {e}")

        job_ctx.add_shutdown_callback(_on_shutdown)
        job_ctx.shutdown(reason=ev.reason.value if hasattr(ev.reason, "value") else str(ev.reason))


# ---------------------------------------------------------------------------
# VTAAgent — Virtual Transfer Agent
# ---------------------------------------------------------------------------
class VTAAgent(Agent):
    """Virtual Transfer Agent — Emma (xAI Grok Realtime)."""

    def __init__(self, *, instructions: str, tools: list, full_name: str) -> None:
        super().__init__(instructions=instructions, tools=tools)
        self._full_name = full_name

    async def on_enter(self):
        """Speak the hardcoded opening greeting."""
        opening = OPENING_LINE_TEMPLATE.format(full_name=self._full_name)
        await self.session.generate_reply(
            instructions=(
                "Speak the following opening line EXACTLY as written, "
                "word-for-word, in a warm professional tone, then stop and "
                "wait silently for the caller's reply. Do not add a preamble, "
                "do not greet in any other way, do not ask anything else.\n\n"
                f"OPENING LINE:\n{opening}"
            ),
            allow_interruptions=False,
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
server = AgentServer()


@server.rtc_session(agent_name=os.getenv("AGENT_NAME", "vta-emma"))
async def entrypoint(ctx: JobContext):
    """Main entrypoint — dispatched for each inbound SIP call from TCN."""
    call_started_at = time.monotonic()

    # Shared HTTP session — saves TLS+connection-setup on every Railway call.
    http_session = aiohttp.ClientSession()

    try:
        # ------------------------------------------------------------------
        # SIP participant discovery — figure out who we're talking to
        # ------------------------------------------------------------------
        phone = ""
        sip_identity = ""
        linked_identity = ""

        def refresh_sip_context() -> None:
            nonlocal phone, sip_identity, linked_identity
            sip_participant = find_primary_sip_participant(ctx.room, preferred_identity=sip_identity)
            if sip_participant is not None:
                sip_identity = sip_participant.identity or sip_identity
                linked_identity = sip_identity or linked_identity
                extracted = extract_phone_from_participant(sip_participant)
                if extracted:
                    nonlocal phone
                    phone = extracted
                logger.info(
                    "Primary SIP: identity=%s callStatus=%s phone=%s",
                    sip_participant.identity,
                    (sip_participant.attributes or {}).get("sip.callStatus", ""),
                    phone,
                )
                return

            # Fallback: standard participant (agent console / dev)
            for p in ctx.room.remote_participants.values():
                if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD:
                    linked_identity = p.identity or linked_identity
                    logger.info("Standard participant for console/dev: identity=%s", p.identity)
                    break

        refresh_sip_context()

        if not phone and not linked_identity:
            participant_connected = asyncio.Event()

            @ctx.room.on("participant_connected")
            def on_participant_connected(participant: rtc.RemoteParticipant):
                refresh_sip_context()
                if phone or linked_identity:
                    participant_connected.set()

            try:
                await asyncio.wait_for(participant_connected.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("No SIP participant joined within 10s")

        # Try job metadata for phone
        if not phone and ctx.job.metadata:
            try:
                meta = json.loads(ctx.job.metadata)
                phone = normalize_phone(meta.get("phone", "") or meta.get("caller_id", ""))
            except (json.JSONDecodeError, TypeError):
                pass

        # Try room name as last resort
        if not phone:
            room_match = re.search(r"(\d{10,})", ctx.room.name)
            if room_match:
                phone = normalize_phone(room_match.group(1))

        logger.info(f"Caller phone: {phone}")

        # ------------------------------------------------------------------
        # Fetch customer info from Railway
        # ------------------------------------------------------------------
        customer_info: dict = {}
        if phone:
            try:
                customer_info = await fetch_customer_info(phone, http=http_session)
            except Exception as e:
                logger.error(f"Customer info fetch failed: {e}")

        # ------------------------------------------------------------------
        # Build dynamic variables (Railway response > env defaults)
        # ------------------------------------------------------------------
        full_name = customer_info.get("full_name") or DEFAULT_FULL_NAME
        company_name = customer_info.get("company_name") or DEFAULT_COMPANY_NAME
        company_address = customer_info.get("company_address") or DEFAULT_COMPANY_ADDRESS
        call_back_number = customer_info.get("call_back_number") or DEFAULT_CALLBACK_NUMBER
        current_time = get_est_time()

        prompt_vars = {
            "full_name": full_name,
            "company_name": company_name,
            "company_address": company_address,
            "call_back_number": call_back_number,
            "current_time": current_time,
        }

        logger.info(f"Dynamic vars: full_name={full_name}, company={company_name}, time={current_time}")

        # ------------------------------------------------------------------
        # Build the agent
        # ------------------------------------------------------------------
        instructions = render_prompt(load_prompt("system_prompt.md"), prompt_vars)

        log_tool = LogVerificationTool(
            phone=phone,
            full_name=full_name,
            call_started_at=call_started_at,
            sip_identity=sip_identity,
            http=http_session,
        )

        vta_agent = VTAAgent(
            instructions=instructions,
            tools=[log_tool],
            full_name=full_name,
        )

        # ------------------------------------------------------------------
        # Build Grok Realtime model
        # ------------------------------------------------------------------
        model_kwargs: dict = {"voice": GROK_VOICE}
        if GROK_REALTIME_MODEL:
            model_kwargs["model"] = GROK_REALTIME_MODEL
        try:
            model_kwargs["temperature"] = GROK_TEMPERATURE
            rt_model = xai_plugin.realtime.RealtimeModel(**model_kwargs)
        except TypeError:
            rt_model = xai_plugin.realtime.RealtimeModel(voice=GROK_VOICE)

        # ------------------------------------------------------------------
        # Create and start the session
        # ------------------------------------------------------------------
        session = AgentSession(
            llm=rt_model,
            min_endpointing_delay=MIN_ENDPOINTING_DELAY,
            max_endpointing_delay=MAX_ENDPOINTING_DELAY,
            user_away_timeout=USER_AWAY_TIMEOUT,
        )

        # Room options — link to the SIP participant
        room_opts = room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(),
        )
        if linked_identity:
            room_opts = room_io.RoomOptions(
                audio_input=room_io.AudioInputOptions(),
                participant_identity=linked_identity,
            )

        await session.start(
            agent=vta_agent,
            room=ctx.room,
            room_options=room_opts,
        )

        # Update sip_identity from linked participant after session starts
        linked_participant = getattr(getattr(session, "room_io", None), "linked_participant", None)
        if linked_participant is not None and linked_participant.identity:
            if linked_participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                sip_identity = linked_participant.identity
                log_tool._sip_identity = sip_identity
            if not phone:
                phone = extract_phone_from_participant(linked_participant)
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
            logger.info("BackgroundAudioPlayer started (OFFICE_AMBIENCE)")
        except Exception as e:
            logger.error(f"BackgroundAudioPlayer failed: {e}")
            background_audio = None

        # ------------------------------------------------------------------
        # Silence handling
        # ------------------------------------------------------------------
        silence_state: dict[str, object] = {"warning_said": False, "hangup_task": None}

        async def _silence_hangup_after(delay: float) -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if log_tool._ending:
                return
            current_state = getattr(session, "user_state", "listening")
            if current_state != "away":
                return
            logger.info(f"[SILENCE] {SILENCE_TOTAL_SECONDS}s total — force ending call")
            await _force_end_call(session, "other",
                f"Caller silent for {int(SILENCE_TOTAL_SECONDS)}s after agent spoke")

        @session.on("user_state_changed")
        def on_user_state(ev) -> None:
            try:
                if log_tool._ending:
                    return
                new_state = getattr(ev, "new_state", None)
                if new_state == "away":
                    if silence_state["warning_said"]:
                        return
                    silence_state["warning_said"] = True
                    logger.info(f"[SILENCE] user_state -> away; prompting + arming hangup timer")
                    try:
                        session.say(SILENCE_PROMPT_TEXT, allow_interruptions=True)
                    except Exception as e:
                        logger.warning(f"[SILENCE] session.say failed: {e}")
                    silence_state["hangup_task"] = asyncio.create_task(
                        _silence_hangup_after(SILENCE_FOLLOWUP_DELAY)
                    )
                elif new_state in ("speaking", "listening"):
                    task = silence_state["hangup_task"]
                    if task is not None and not task.done():
                        task.cancel()
                    silence_state["hangup_task"] = None
                    silence_state["warning_said"] = False
            except Exception as e:
                logger.exception(f"[SILENCE] handler failed: {e}")

        # ------------------------------------------------------------------
        # Tool nudge — fallback if model speaks closing but doesn't call tool
        # ------------------------------------------------------------------
        _nudge_task: asyncio.Task | None = None

        @session.on("agent_state_changed")
        def on_agent_state(ev) -> None:
            nonlocal _nudge_task
            try:
                if log_tool._ending:
                    return
                new_state = getattr(ev, "new_state", None)
                if new_state == "listening":
                    if _nudge_task is not None and not _nudge_task.done():
                        _nudge_task.cancel()

                    async def _nudge() -> None:
                        try:
                            await asyncio.sleep(TOOL_NUDGE_DELAY)
                        except asyncio.CancelledError:
                            return
                        if log_tool._ending:
                            return
                        if getattr(session, "user_state", "listening") == "speaking":
                            return
                        elapsed = time.monotonic() - call_started_at
                        if elapsed < 10.0:
                            return
                        logger.info("[NUDGE] prompting model to call log_verification")
                        try:
                            await session.generate_reply(
                                instructions=(
                                    "If your very last spoken response was a closing "
                                    "or farewell line (e.g. goodbye, transfer, have a "
                                    "nice day), you MUST call log_verification now with "
                                    "the appropriate status and summary. Do not speak "
                                    "again — just call the function."
                                ),
                            )
                        except Exception as e:
                            logger.warning(f"[NUDGE] generate_reply failed: {e}")

                    _nudge_task = asyncio.create_task(_nudge())

                elif new_state in ("speaking", "thinking"):
                    if _nudge_task is not None and not _nudge_task.done():
                        _nudge_task.cancel()
                        _nudge_task = None
            except Exception as e:
                logger.exception(f"[NUDGE] handler failed: {e}")

        @session.on("user_state_changed")
        def on_user_state_nudge_cancel(ev) -> None:
            nonlocal _nudge_task
            if getattr(ev, "new_state", None) == "speaking":
                if _nudge_task is not None and not _nudge_task.done():
                    _nudge_task.cancel()
                    _nudge_task = None

        # ------------------------------------------------------------------
        # Max call duration watchdog
        # ------------------------------------------------------------------
        async def _max_duration_watchdog() -> None:
            try:
                await asyncio.sleep(MAX_CALL_DURATION)
            except asyncio.CancelledError:
                return
            if log_tool._ending:
                return
            logger.info(f"[WATCHDOG] max duration {MAX_CALL_DURATION}s — force ending")
            await _force_end_call(session, "other",
                f"Max call duration {int(MAX_CALL_DURATION)}s reached")

        max_duration_task = asyncio.create_task(_max_duration_watchdog())

        # ------------------------------------------------------------------
        # System-driven force_end_call (silence timeout, max duration)
        # ------------------------------------------------------------------
        async def _force_end_call(sess: AgentSession, status: str, summary: str) -> None:
            """System-initiated ending: speak closing, log, remove SIP, shutdown."""
            if log_tool._ending:
                return
            log_tool._ending = True

            logger.info(f"[END_CALL] system: status={status} summary={summary!r}")

            # Interrupt any in-flight speech
            try:
                sess.interrupt()
            except Exception:
                pass

            # Speak system closing
            try:
                handle = sess.say(SYSTEM_CLOSING_OTHER, allow_interruptions=False)
                if handle is not None and hasattr(handle, "wait_for_playout"):
                    await handle.wait_for_playout()
            except Exception as e:
                logger.error(f"[END_CALL] session.say failed: {e}")
                await asyncio.sleep(3.0)

            # Log to Railway
            try:
                await log_verification_to_server(
                    phone, status, summary, full_name, http=http_session
                )
            except Exception as e:
                logger.error(f"[END_CALL] log_verification_to_server failed: {e}")

            if not log_tool._call_end_notified:
                log_tool._call_end_notified = True
                room_name = (ctx.room.name if ctx.room else "") or ""
                duration_ms = max(0, int((time.monotonic() - call_started_at) * 1000))
                try:
                    await notify_call_ended(
                        phone=phone, call_id=room_name, duration_ms=duration_ms,
                        disconnection_reason=f"agent_end_call:{status}:system",
                        http=http_session,
                    )
                except Exception as e:
                    logger.error(f"[END_CALL] notify_call_ended failed: {e}")

            # Remove SIP participant → BYE to TCN
            room_name = (ctx.room.name if ctx.room else "") or ""
            if room_name and sip_identity:
                try:
                    from livekit import api
                    await ctx.api.room.remove_participant(
                        api.RoomParticipantIdentity(room=room_name, identity=sip_identity)
                    )
                    logger.info(f"[TEARDOWN] SIP {sip_identity} removed — BYE to TCN")
                except Exception as e:
                    logger.warning(f"[TEARDOWN] remove_participant failed: {e}")
                    try:
                        await ctx.delete_room()
                    except Exception:
                        pass
            elif room_name:
                try:
                    await ctx.delete_room()
                except Exception:
                    pass

            # Shutdown
            try:
                ctx.shutdown(reason=f"agent_end_call:{status}")
            except Exception:
                pass

        # ------------------------------------------------------------------
        # Cleanup callback
        # ------------------------------------------------------------------
        async def _cleanup():
            if not max_duration_task.done():
                max_duration_task.cancel()
            if _nudge_task is not None and not _nudge_task.done():
                _nudge_task.cancel()
            hangup_task = silence_state.get("hangup_task")
            if hangup_task is not None and not hangup_task.done():
                hangup_task.cancel()
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
            f"VTA agent started for {phone} ({full_name}) | "
            f"voice={GROK_VOICE} model={GROK_REALTIME_MODEL or 'default'} | "
            f"silence={USER_AWAY_TIMEOUT}s/{SILENCE_TOTAL_SECONDS}s max={MAX_CALL_DURATION}s"
        )

    except Exception:
        try:
            await http_session.close()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    cli.run_app(server)
