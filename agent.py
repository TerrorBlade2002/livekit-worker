"""
VTA Emma — LiveKit Voice Agent (xAI Grok Realtime, non-thinking)

Architecture (TCN 3-way call):
  - TCN leg A: TCN <-> Customer (TCN owns this — never touched here)
  - TCN leg B: TCN <-> LiveKit SIP gateway (SIP participant in this room)
  - LiveKit room: { SIP participant, VTA agent }

End-of-call flow:
  1. LLM speaks the closing line (per prompt's CLOSING PROTOCOL)
  2. LLM calls log_verification tool (SAME response — audio still streaming)
  3. Tool arms speech_handle.done callback, THEN logs to Railway inline
  4. Closing audio finishes playing → callback fires
  5. Callback removes SIP participant → clean BYE to TCN
  6. TCN sees "Action OK" and routes leg A onward (data dip -> hunt group)
  7. Tool shuts down job context → worker cleanup
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv
from livekit import agents, api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    JobProcess,
    RunContext,
    function_tool,
    metrics,
)
from livekit.agents.voice import room_io
from livekit.plugins import xai as xai_plugin

# ---------------------------------------------------------------------------
# .env loading — robust against arbitrary cwd
#
# Pin the search to the .env that lives next to this file so it works
# regardless of cwd, Docker WORKDIR, or multiprocessing spawn context.
# ---------------------------------------------------------------------------
_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=True)

logger = logging.getLogger("vta-agent")
logger.setLevel(logging.INFO)

logger.info(
    f"[BOOT] .env path: {_ENV_PATH} (exists={_ENV_PATH.exists()})"
)


def _resolve_xai_api_key() -> str | None:
    """Read XAI_API_KEY (or GROK_API_KEY) at call time, not at import time."""
    return os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY")


RAILWAY_SERVER_URL = os.getenv(
    "RAILWAY_SERVER_URL", "https://virtual-transfer-agent-production.up.railway.app"
)

# ---------------------------------------------------------------------------
# Grok Realtime config
#
# Uses the xai plugin's RealtimeModel directly. The plugin handles the
# xAI Realtime WebSocket connection and reads XAI_API_KEY from the env.
#
# Voices (case-insensitive): Ara, Eve, Leo, Rex, Sal
# Model: defaults to the plugin's built-in non-thinking model.
#   Override via GROK_REALTIME_MODEL env var if needed.
# ---------------------------------------------------------------------------
GROK_VOICE = os.getenv("GROK_VOICE", "Ara")
GROK_REALTIME_MODEL = os.getenv("GROK_REALTIME_MODEL", "")  # empty = plugin default
GROK_TEMPERATURE = float(os.getenv("GROK_TEMPERATURE", "0.7"))

# AgentSession latency knobs
AEC_WARMUP_DURATION = float(os.getenv("AEC_WARMUP_DURATION", "0"))
PREEMPTIVE_GENERATION = os.getenv("PREEMPTIVE_GENERATION", "true").lower() == "true"
MIN_ENDPOINTING_DELAY = float(os.getenv("MIN_ENDPOINTING_DELAY", "0.4"))
MAX_ENDPOINTING_DELAY = float(os.getenv("MAX_ENDPOINTING_DELAY", "3.0"))

# ---------------------------------------------------------------------------
# Silence handling
#
# After the agent finishes speaking, the user_state goes to "listening". If
# no user audio arrives for USER_AWAY_TIMEOUT seconds, AgentSession emits
# user_state_changed -> "away". We hook that to:
#   1. Speak "Are you still there?" once
#   2. Start a SILENCE_FOLLOWUP_DELAY timer
#   3. If still away when timer fires -> force_end_call(status="other")
#   4. If user comes back -> cancel the timer and reset
# ---------------------------------------------------------------------------
USER_AWAY_TIMEOUT = float(os.getenv("USER_AWAY_TIMEOUT", "10"))
SILENCE_TOTAL_SECONDS = float(os.getenv("SILENCE_TOTAL_SECONDS", "60"))
SILENCE_FOLLOWUP_DELAY = max(1.0, SILENCE_TOTAL_SECONDS - USER_AWAY_TIMEOUT)
SILENCE_PROMPT_TEXT = os.getenv("SILENCE_PROMPT_TEXT", "Are you still there?")

# Max call duration watchdog — hard cap to prevent runaway calls.
MAX_CALL_DURATION = float(os.getenv("MAX_CALL_DURATION", "300"))  # 5 minutes

CONFIG_DIR = Path(__file__).parent / "config"

OPENING_LINE_TEMPLATE = "Hi, this call is for {full_name}."

# Closing message for system-initiated endings (silence timeout, max duration).
# The normal path has the LLM speak closings per the prompt; this is only for
# force_end_call where the system itself must speak a closing.
SYSTEM_CLOSING_OTHER = (
    "I apologize if this call caused any inconvenience. Thank you for your time — "
    "our representatives may try again later or contact you regarding the matter. Goodbye."
)

TCN_TRANSFER_STATUSES = {"verified", "customer_wants_human"}

# ---------------------------------------------------------------------------
# Tool schema — explicit raw_schema so the Realtime model gets EXACTLY the
# right function definition. Auto-generated schemas from @function_tool()
# can produce formats the xAI Realtime model doesn't handle cleanly (e.g.
# narrating the tool call aloud, or not triggering the call at all).
#
# Key line: "Do not produce any further speech once this is called."
# Without it, the Realtime model will speak the function call parameters.
# ---------------------------------------------------------------------------
_LOG_VERIFICATION_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "log_verification",
    "description": (
        "Log the disposition status before ending the call along with a brief "
        "description of what happened and the reason for disposing of a particular "
        "status, then immediately end the call. This is the ONLY way to end the "
        "call — always call this AFTER speaking the closing line, never before. "
        "Do not produce any further speech once this is called."
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
                    "Brief description of what happened on the call, including if "
                    "any callback information exchanges like call back number or "
                    "time provided by the consumer, and the reason for the "
                    "disposition status."
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


# ---------------------------------------------------------------------------
# Observability — Timeline helper
# ---------------------------------------------------------------------------
class Timeline:
    """Wall-clock stage tracker for a single call's startup path."""

    def __init__(self, label: str):
        self.label = label or "?"
        self.t0 = time.monotonic()
        self.last = self.t0
        logger.info(f"[TTFT:{self.label}] +    0.0ms (total=    0.0ms)  __start__")

    def mark(self, name: str) -> None:
        now = time.monotonic()
        delta = (now - self.last) * 1000.0
        total = (now - self.t0) * 1000.0
        logger.info(
            f"[TTFT:{self.label}] +{delta:7.1f}ms (total={total:7.1f}ms)  {name}"
        )
        self.last = now


def load_prompt(filename: str) -> str:
    """Read a prompt template from the config directory."""
    path = CONFIG_DIR / filename
    return path.read_text(encoding="utf-8")


def normalize_phone(raw: str) -> str:
    """Normalize phone to last 10 digits, matching the Railway server logic."""
    digits = re.sub(r"\D", "", raw)
    return digits[-10:] if len(digits) >= 10 else digits


def tcn_http_code_for_status(status: str) -> int:
    """Map the final agent status to the HTTP code TCN should later receive."""
    return 200 if status in TCN_TRANSFER_STATUSES else 409


def extract_phone_from_participant(participant: rtc.RemoteParticipant) -> str:
    """Extract a real customer phone number from participant state."""
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
    """Pick the customer-facing SIP leg the agent should listen to and remove."""
    participants = list(room.remote_participants.values())

    if preferred_identity:
        for participant in participants:
            if (
                participant.identity == preferred_identity
                and participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            ):
                return participant

    sip_participants = [
        participant
        for participant in participants
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
    ]
    if not sip_participants:
        return None

    def participant_rank(participant: rtc.RemoteParticipant) -> tuple[int, int]:
        status = ((participant.attributes or {}).get("sip.callStatus", "") or "").lower()
        active_rank = 0 if status == "active" else 1
        missing_phone_rank = 0 if extract_phone_from_participant(participant) else 1
        return (active_rank, missing_phone_rank)

    sip_participants.sort(key=participant_rank)
    return sip_participants[0]


def find_primary_standard_participant(
    room: rtc.Room,
    preferred_identity: str = "",
) -> rtc.RemoteParticipant | None:
    """Pick a standard participant for Agent Console and other non-SIP testing."""
    participants = list(room.remote_participants.values())

    if preferred_identity:
        for participant in participants:
            if (
                participant.identity == preferred_identity
                and participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD
            ):
                return participant

    for participant in participants:
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD:
            return participant

    return None


# ---------------------------------------------------------------------------
# HTTP helpers — Railway server integration
# ---------------------------------------------------------------------------
async def fetch_customer_info(phone: str, http: aiohttp.ClientSession | None = None) -> dict:
    """Call the Railway server's /retell-webhook to look up customer data."""
    normalized = normalize_phone(phone)
    payload = {"call_inbound": {"from_number": f"+1{normalized}"}}
    timeout = aiohttp.ClientTimeout(total=3)
    try:
        if http is None:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{RAILWAY_SERVER_URL}/retell-webhook", json=payload, timeout=timeout
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"Webhook returned {resp.status} for {normalized}")
                        return {}
                    data = await resp.json()
        else:
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


async def notify_call_ended(
    phone: str,
    call_id: str,
    duration_ms: int,
    disconnection_reason: str,
    http: aiohttp.ClientSession | None = None,
) -> None:
    """Fire /retell-call-ended so the Railway server can enrich or backfill disposition data."""
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
        if http is None:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{RAILWAY_SERVER_URL}/retell-call-ended", json=payload, timeout=timeout
                ) as resp:
                    logger.info(f"call_ended notify for {normalized}: HTTP {resp.status}")
        else:
            async with http.post(
                f"{RAILWAY_SERVER_URL}/retell-call-ended", json=payload, timeout=timeout
            ) as resp:
                logger.info(f"call_ended notify for {normalized}: HTTP {resp.status}")
    except Exception as e:
        logger.error(f"Error notifying call_ended: {e}")


async def log_verification_to_server(
    phone: str,
    status: str,
    summary: str,
    full_name: str,
    http: aiohttp.ClientSession | None = None,
) -> dict:
    """Log verification result to Railway server, same contract as Retell's log_verification."""
    normalized = normalize_phone(phone)
    payload = {
        "args": {
            "status": status,
            "summary": summary,
            "full_name": full_name,
        },
        "call": {
            "from_number": f"+1{normalized}",
        },
    }
    timeout = aiohttp.ClientTimeout(total=3)
    try:
        if http is None:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{RAILWAY_SERVER_URL}/log-verification", json=payload, timeout=timeout
                ) as resp:
                    data = await resp.json()
        else:
            async with http.post(
                f"{RAILWAY_SERVER_URL}/log-verification", json=payload, timeout=timeout
            ) as resp:
                data = await resp.json()

        logger.info(f"Log verification response for {normalized}: {data}")
        return data
    except Exception as e:
        logger.error(f"Error logging verification: {e}")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# VTAAgent — Virtual Transfer Agent
#
# End-call architecture:
#   NORMAL PATH (LLM-driven):
#     1. LLM speaks closing line per the prompt's CLOSING PROTOCOL
#     2. LLM calls log_verification tool
#     3. Tool: disallow_interruptions -> log to Railway -> remove SIP -> return ""
#     4. Session auto-closes when SIP participant leaves (close_on_disconnect)
#
#   SYSTEM PATH (silence timeout / max duration):
#     1. System calls force_end_call() on the agent
#     2. force_end_call: interrupt in-flight speech -> say() closing -> _teardown()
#     3. _teardown: log to Railway -> remove SIP -> session auto-closes
# ---------------------------------------------------------------------------
class VTAAgent(Agent):
    """Virtual Transfer Agent — Emma (xAI Grok Realtime, non-thinking)."""

    def __init__(
        self,
        phone: str,
        customer_info: dict,
        ctx: agents.JobContext | None = None,
        sip_identity: str = "",
        http: aiohttp.ClientSession | None = None,
    ):
        self._phone = phone
        self._customer_info = customer_info
        self._call_started_at = time.monotonic()
        self._call_end_notified = False
        self._ctx = ctx
        self._sip_identity = sip_identity
        self._http = http
        self._ending = False  # guards against double-trigger of end-call

        full_name = customer_info.get("full_name", "the customer")
        company_name = customer_info.get("company_name", "our company")
        company_address = customer_info.get("company_address", "")
        call_back_number = customer_info.get("call_back_number", "")

        # Use str.replace() instead of str.format() — safe with literal {}
        # in JSON examples inside the prompt.
        instructions = (
            load_prompt("system_prompt.md")
            .replace("{full_name}", full_name)
            .replace("{company_name}", company_name)
            .replace("{company_address}", company_address)
            .replace("{call_back_number}", call_back_number)
        )

        # Register the tool with an explicit raw_schema so the Realtime
        # model gets the exact function definition (including "Do not produce
        # any further speech once this is called.").
        _tool = function_tool(
            self._log_verification,
            raw_schema=_LOG_VERIFICATION_SCHEMA,
        )
        super().__init__(instructions=instructions, tools=[_tool])
        self._full_name = full_name

    async def on_enter(self):
        """Speak the opening greeting when the agent enters the session.

        This is called automatically by AgentSession after session.start().
        The opening line is hardcoded (OPENING_LINE_TEMPLATE) so the LLM
        cannot rewrite it.
        """
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

    def _resolve_sip_identity(self, session: AgentSession) -> str:
        """Best-effort: figure out which participant identity is the SIP leg from TCN."""
        room_io_obj = getattr(session, "room_io", None)
        linked_participant = getattr(room_io_obj, "linked_participant", None)
        if (
            linked_participant is not None
            and getattr(linked_participant, "kind", None) == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        ):
            self._sip_identity = linked_participant.identity or self._sip_identity

        room: rtc.Room | None = None
        if self._ctx is not None:
            room = getattr(self._ctx, "room", None)
        if room is None and room_io_obj is not None:
            room = getattr(room_io_obj, "room", None)

        if room is not None:
            participant = find_primary_sip_participant(room, preferred_identity=self._sip_identity)
            if participant is not None:
                self._sip_identity = participant.identity or self._sip_identity

        return self._sip_identity

    # ------------------------------------------------------------------
    # Shared teardown: log + notify + remove SIP
    # ------------------------------------------------------------------
    async def _teardown(
        self,
        status: str,
        summary: str,
        *,
        session: AgentSession,
        trigger: str = "tool",
        skip_logging: bool = False,
    ) -> None:
        """Common teardown: log disposition, notify call ended, remove SIP participant.

        Called by both log_verification (LLM-driven) and force_end_call (system-driven).

        When skip_logging=True, Steps 1-2 (Railway log + notify) are skipped because
        the caller already did them inline (e.g. log_verification logs immediately so
        data is never lost, then defers SIP removal to a speech_handle callback).
        """
        room: rtc.Room | None = None
        if self._ctx is not None:
            room = getattr(self._ctx, "room", None)
        if room is None:
            room = getattr(getattr(session, "room_io", None), "room", None)
        room_name = (room.name if room is not None else "") or ""

        if not skip_logging:
            # Step 1 — log to the Railway server (Retell-compatible contract)
            try:
                await log_verification_to_server(
                    self._phone, status, summary, self._full_name, http=self._http
                )
            except Exception as e:
                logger.error(f"[TEARDOWN] log_verification_to_server failed ({trigger}): {e}")

            # Step 2 — notify call ended
            if not self._call_end_notified:
                duration_ms = max(0, int((time.monotonic() - self._call_started_at) * 1000))
                try:
                    await notify_call_ended(
                        phone=self._phone,
                        call_id=room_name,
                        duration_ms=duration_ms,
                        disconnection_reason=f"agent_end_call:{status}:{trigger}",
                        http=self._http,
                    )
                except Exception as e:
                    logger.error(f"[TEARDOWN] notify_call_ended failed ({trigger}): {e}")
                self._call_end_notified = True

        # Step 3 — surgical hangup: remove ONLY the SIP participant
        sip_identity = ""
        try:
            sip_identity = self._resolve_sip_identity(session)
        except Exception as e:
            logger.error(f"[TEARDOWN] _resolve_sip_identity failed: {e}")

        removed_ok = False
        if self._ctx is not None and room_name and sip_identity:
            try:
                await self._ctx.api.room.remove_participant(
                    api.RoomParticipantIdentity(
                        room=room_name,
                        identity=sip_identity,
                    )
                )
                removed_ok = True
                logger.info(
                    f"[TEARDOWN] done trigger={trigger} status={status} phone={self._phone} "
                    f"room={room_name} sip_identity={sip_identity} "
                    f"tcn_http={tcn_http_code_for_status(status)} "
                    f"— SIP participant removed, BYE en route to TCN"
                )
            except Exception as e:
                logger.error(
                    f"[TEARDOWN] remove_participant failed for "
                    f"{sip_identity} in {room_name}: {e}"
                )

        # Step 4 — fallback if we couldn't identify/remove the SIP participant
        if not removed_ok:
            logger.warning(
                f"[TEARDOWN] no SIP identity to remove "
                f"(sip_identity={sip_identity or 'not-found'}); falling back to delete_room"
            )
            if self._ctx is not None and room_name:
                try:
                    await self._ctx.api.room.delete_room(
                        api.DeleteRoomRequest(room=room_name)
                    )
                    logger.info(f"[TEARDOWN] delete_room fallback fired for room={room_name}")
                except Exception as e:
                    logger.error(f"[TEARDOWN] delete_room fallback failed: {e}")
                    if room is not None:
                        try:
                            await room.disconnect()
                        except Exception as e2:
                            logger.error(f"[TEARDOWN] room.disconnect last-resort failed: {e2}")

    # ------------------------------------------------------------------
    # LLM-driven end-call: log_verification tool
    #
    # The LLM has ALREADY spoken the closing line before calling this tool
    # (per the prompt's CLOSING PROTOCOL). This tool just logs the
    # disposition and tears down the SIP leg.
    #
    # RACE-CONDITION FIX (ref: Riley agent pattern):
    #   The closing-line audio and this tool call are part of the SAME
    #   Realtime response (same speech_handle). Audio is still streaming
    #   to the customer when the tool fires. If we remove the SIP
    #   participant inline, the audio gets cut off mid-sentence.
    #
    #   Solution — three-phase lifecycle:
    #     Phase 1: Arm speech_handle.add_done_callback FIRST (before any
    #              I/O) so the call WILL end even if the HTTP POST hangs.
    #     Phase 2: Log disposition to Railway INLINE (data must never be
    #              lost). Signal _logging_complete when done.
    #     Phase 3: (in callback) Wait for _logging_complete, THEN remove
    #              SIP participant, THEN shut down the job context.
    #
    #   Safety net: 10s timeout fires teardown if the callback never does.
    # ------------------------------------------------------------------
    async def _log_verification(
        self,
        raw_arguments: dict[str, object],
        ctx: RunContext,
    ) -> str:
        """Tool implementation — called by the framework with raw_schema args.

        Registered via function_tool(raw_schema=_LOG_VERIFICATION_SCHEMA) in
        __init__, NOT via @function_tool() decorator. This ensures the Realtime
        model gets the exact schema (with "Do not produce any further speech")
        and never narrates the function call aloud.
        """
        # Extract parameters from the raw JSON dict (reference pattern)
        status = str(raw_arguments.get("status", "other"))
        summary = str(raw_arguments.get("summary") or "")
        full_name = str(raw_arguments.get("full_name") or "")

        if self._ending:
            logger.warning("[END_CALL] already ending — duplicate tool call ignored")
            return ""
        self._ending = True
        self._full_name = full_name or self._full_name

        # Prevent user speech from interrupting the teardown sequence
        try:
            ctx.disallow_interruptions()
        except Exception as e:
            logger.warning(f"disallow_interruptions failed (continuing): {e}")

        logger.info(
            f"[END_CALL] tool: status={status} phone={self._phone} "
            f"summary={summary!r} full_name={full_name!r}"
        )

        session = ctx.session

        # Coordination event: _finish() waits on this before touching the
        # SIP leg, so the http_session is never closed out from under an
        # in-flight POST.
        _logging_complete = asyncio.Event()
        _teardown_done = False

        async def _finish() -> None:
            """SIP teardown + job shutdown — runs exactly once.

            Called from either the speech_handle callback (normal path) or
            the safety timeout (fallback). Guarded by _teardown_done flag.
            """
            nonlocal _teardown_done
            if _teardown_done:
                return
            _teardown_done = True

            # Wait for the inline HTTP logging to complete before we touch
            # the session/room — ctx.shutdown() would close the http_session.
            try:
                await asyncio.wait_for(_logging_complete.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "[END_CALL] logging didn't complete in 5s; "
                    "proceeding with SIP teardown anyway"
                )

            logger.info(
                "[END_CALL] executing deferred SIP teardown "
                "(closing audio has finished playing)"
            )

            # SIP removal — sends clean BYE to TCN
            try:
                await self._teardown(
                    status, summary, session=session, trigger="tool",
                    skip_logging=True,  # already logged inline
                )
            except Exception as e:
                logger.error(f"[END_CALL] deferred SIP teardown failed: {e}")

            # Clean shutdown so the worker doesn't idle until job timeout.
            # Triggers entrypoint's _cleanup callback (cancels watchdogs,
            # closes http_session, stops background audio).
            if self._ctx is not None:
                try:
                    self._ctx.shutdown(reason=f"agent_end_call:{status}")
                except Exception as e:
                    logger.warning(f"[END_CALL] ctx.shutdown failed: {e}")

        # ---- PHASE 1: Arm shutdown callbacks BEFORE the HTTP POST ----
        # Even if the POST hangs for its full 3s timeout, the call will
        # still end as soon as the closing audio finishes playing.
        callback_armed = False
        try:
            def _on_speech_done(_) -> None:
                asyncio.ensure_future(_finish())

            ctx.speech_handle.add_done_callback(_on_speech_done)
            callback_armed = True
            logger.info(
                "[END_CALL] SIP teardown deferred — armed on speech_handle.done "
                "(customer will hear full closing line before BYE)"
            )
        except Exception as e:
            logger.warning(
                f"[END_CALL] speech_handle.add_done_callback unavailable ({e}); "
                "will fall back to inline teardown after logging"
            )

        # Safety timeout — if the speech_handle callback never fires (e.g.
        # Realtime WS disconnect), force teardown after 10s so the call
        # doesn't hang indefinitely.
        if callback_armed:
            async def _safety_timeout() -> None:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    return
                if not _teardown_done:
                    logger.warning(
                        "[END_CALL] speech_handle callback didn't fire in 10s; "
                        "forcing SIP teardown (safety timeout)"
                    )
                    await _finish()

            asyncio.create_task(_safety_timeout())

        # ---- PHASE 2: Log to Railway INLINE (immediate, data-safe) ----
        # Disposition data is persisted before we touch anything else.
        # Even if the callback never fires, the data is safe.
        room: rtc.Room | None = None
        if self._ctx is not None:
            room = getattr(self._ctx, "room", None)
        if room is None:
            room = getattr(getattr(session, "room_io", None), "room", None)
        room_name = (room.name if room is not None else "") or ""

        try:
            await log_verification_to_server(
                self._phone, status, summary, self._full_name, http=self._http
            )
        except Exception as e:
            logger.error(f"[END_CALL] log_verification_to_server failed: {e}")

        if not self._call_end_notified:
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
            self._call_end_notified = True

        # Signal that all HTTP logging is done — _finish() can now safely
        # proceed to SIP removal and ctx.shutdown() without killing the
        # http_session out from under an in-flight POST.
        _logging_complete.set()

        # ---- PHASE 3: Fallback if callback couldn't be armed ----
        if not callback_armed:
            logger.warning(
                "[END_CALL] no speech callback available; "
                "executing SIP teardown inline (closing audio may clip)"
            )
            await _finish()

        # Return empty string — LLM should produce no follow-up speech.
        return ""

    # ------------------------------------------------------------------
    # System-driven end-call: force_end_call
    #
    # Called by the silence timeout handler or max-duration watchdog.
    # Unlike log_verification, this must speak the closing line itself
    # since the LLM isn't driving the end-of-call.
    # ------------------------------------------------------------------
    async def force_end_call(
        self,
        status: str,
        summary: str,
        *,
        session: AgentSession,
    ) -> None:
        """System-initiated call ending (silence timeout, max duration).

        Interrupts any in-flight LLM speech, speaks the system closing line
        via session.say(), then tears down the SIP leg.
        """
        if self._ending:
            logger.info("[END_CALL] force_end_call skipped — already ending")
            return
        self._ending = True

        logger.info(
            f"[END_CALL] system: status={status} phone={self._phone} "
            f"summary={summary!r}"
        )

        # Interrupt any in-flight LLM speech so the closing is clean
        try:
            session.interrupt()
        except Exception as e:
            logger.warning(f"session.interrupt() before system closing failed: {e}")

        # Speak the system closing line
        try:
            handle = session.say(SYSTEM_CLOSING_OTHER, allow_interruptions=False)
            if handle is not None and hasattr(handle, "wait_for_playout"):
                await handle.wait_for_playout()
        except Exception as e:
            logger.error(f"[END_CALL] session.say(system closing) failed: {e}")
            # Sleep as fallback so any partial audio can drain
            try:
                await asyncio.sleep(3.0)
            except Exception:
                pass

        await self._teardown(
            status, summary, session=session, trigger="system"
        )

        # Clean shutdown so the worker doesn't idle until job timeout.
        if self._ctx is not None:
            try:
                self._ctx.shutdown(reason=f"agent_end_call:{status}")
            except Exception as e:
                logger.warning(f"[END_CALL] ctx.shutdown failed (system path): {e}")


# ---------------------------------------------------------------------------
# prewarm — runs ONCE per worker process at startup, before any jobs land.
# ---------------------------------------------------------------------------
def prewarm(proc: JobProcess) -> None:
    """Pre-load expensive, reusable resources before the first job arrives."""
    t0 = time.monotonic()
    load_dotenv(dotenv_path=_ENV_PATH, override=True)
    xai_present = bool(_resolve_xai_api_key())
    elapsed = (time.monotonic() - t0) * 1000.0
    logger.info(
        f"[PREWARM] worker process initialized in {elapsed:.1f}ms "
        f"(env={_ENV_PATH}, exists={_ENV_PATH.exists()}, xai_key_loaded={xai_present})"
    )


async def entrypoint(ctx: agents.JobContext):
    """Main entrypoint — dispatched for each inbound SIP call from TCN."""
    timeline = Timeline(ctx.room.name or "unknown")

    # CONNECT FIRST — join the room before doing anything else.
    try:
        await ctx.connect()
    except Exception as e:
        logger.exception(f"ctx.connect() failed — cannot proceed: {e}")
        return
    timeline.mark("ctx.connect done")

    # Shared HTTP session — saves TLS+connection-setup on every Railway call.
    http_session = aiohttp.ClientSession()
    timeline.mark("http session up")

    # Re-read .env at job time and re-resolve the key.
    load_dotenv(dotenv_path=_ENV_PATH, override=True)
    xai_api_key = _resolve_xai_api_key()

    if not xai_api_key:
        related_env = {
            k: ("<set,len=" + str(len(v)) + ">") if v else "<empty>"
            for k, v in os.environ.items()
            if any(needle in k.upper() for needle in ("XAI", "GROK", "X_AI"))
        }
        platform_hint = (
            "Railway / container" if os.getcwd().startswith(("/app", "/workspace")) else "local dev"
        )
        logger.error(
            "\n" + "=" * 70 + "\n"
            "  XAI_API_KEY is NOT SET. Grok Realtime cannot connect.\n"
            f"  Platform appears to be: {platform_hint}\n"
            f"  cwd: {os.getcwd()}\n"
            f"  Tried to load .env from: {_ENV_PATH}\n"
            f"  .env exists at that path: {_ENV_PATH.exists()}\n"
            f"  XAI/GROK-related env vars present: {related_env or '<none>'}\n"
            "\n"
            "  -> If on Railway/container: set XAI_API_KEY in Railway's Variables tab.\n"
            "  -> If on local dev: add XAI_API_KEY=<key> to livekit-worker/.env\n"
            + "=" * 70
        )
        try:
            await http_session.close()
        except Exception:
            pass
        return

    try:

        phone = ""
        sip_identity = ""
        linked_identity = ""

        def refresh_sip_context() -> None:
            nonlocal phone, sip_identity, linked_identity
            sip_participant = find_primary_sip_participant(ctx.room, preferred_identity=sip_identity)
            if sip_participant is not None:
                sip_identity = sip_participant.identity or sip_identity
                linked_identity = sip_identity or linked_identity
                extracted_phone = extract_phone_from_participant(sip_participant)
                if extracted_phone:
                    phone = extracted_phone
                logger.info(
                    "Primary SIP participant: identity=%s callStatus=%s phone=%s",
                    sip_participant.identity,
                    (sip_participant.attributes or {}).get("sip.callStatus", ""),
                    phone,
                )
                return

            standard_participant = find_primary_standard_participant(
                ctx.room, preferred_identity=linked_identity,
            )
            if standard_participant is not None:
                linked_identity = standard_participant.identity or linked_identity
                logger.info(
                    "Primary standard participant for console/dev: identity=%s",
                    standard_participant.identity,
                )

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
                logger.warning("No SIP participant joined within 10s. Using room metadata.")

        timeline.mark("SIP participant resolved")

        if not phone and ctx.job.metadata:
            try:
                meta = json.loads(ctx.job.metadata)
                phone = normalize_phone(meta.get("phone", "") or meta.get("caller_id", ""))
            except (json.JSONDecodeError, TypeError):
                pass

        if not phone:
            room_match = re.search(r"(\d{10,})", ctx.room.name)
            if room_match:
                phone = normalize_phone(room_match.group(1))

        logger.info(f"Caller phone: {phone}")

        # PARALLELISM: kick off customer info fetch while building the model/agent.
        if phone:
            customer_info_task = asyncio.create_task(fetch_customer_info(phone, http=http_session))
        else:
            customer_info_task = None
        timeline.mark("customer info fetch fired (async)")

        # Build Grok Realtime model — xai plugin's non-thinking model.
        # The plugin reads XAI_API_KEY from the environment automatically.
        model_kwargs: dict = {"voice": GROK_VOICE}
        if GROK_REALTIME_MODEL:
            model_kwargs["model"] = GROK_REALTIME_MODEL
        try:
            model_kwargs["temperature"] = GROK_TEMPERATURE
            rt_model = xai_plugin.realtime.RealtimeModel(**model_kwargs)
        except TypeError:
            # temperature or model might not be supported params in this version
            rt_model = xai_plugin.realtime.RealtimeModel(voice=GROK_VOICE)
        timeline.mark("realtime model constructed")

        # Await customer info now.
        if customer_info_task is not None:
            try:
                customer_info = await customer_info_task
            except Exception as e:
                logger.error(f"customer info fetch failed: {e}")
                customer_info = {}
        else:
            customer_info = {}

        if not customer_info.get("full_name"):
            customer_info["full_name"] = "the customer"
            logger.warning(f"No customer info found for phone {phone}")
        timeline.mark("customer info ready")

        vta_agent = VTAAgent(
            phone=phone,
            customer_info=customer_info,
            ctx=ctx,
            sip_identity=sip_identity,
            http=http_session,
        )
        timeline.mark("agent constructed")

        # Build the AgentSession with Realtime LLM and explicit latency knobs.
        session = AgentSession(
            llm=rt_model,
            aec_warmup_duration=AEC_WARMUP_DURATION,
            preemptive_generation=PREEMPTIVE_GENERATION,
            min_endpointing_delay=MIN_ENDPOINTING_DELAY,
            max_endpointing_delay=MAX_ENDPOINTING_DELAY,
            user_away_timeout=USER_AWAY_TIMEOUT,
        )

        # Observability: log per-turn latency from the model itself.
        @session.on("metrics_collected")
        def on_metrics(ev) -> None:
            try:
                m = ev.metrics
                label = type(m).__name__
                ttft = getattr(m, "ttft", None)
                duration = getattr(m, "duration", None)
                request_id = getattr(m, "request_id", "") or ""
                parts = [f"[METRICS:{label}]"]
                if ttft is not None and ttft >= 0:
                    parts.append(f"ttft={ttft*1000:.1f}ms")
                if duration is not None and duration >= 0:
                    parts.append(f"duration={duration*1000:.1f}ms")
                if request_id:
                    parts.append(f"req={request_id}")
                logger.info(" ".join(parts))
            except Exception as e:
                logger.warning(f"metrics handler failed: {e}")

        # ------------------------------------------------------------------
        # Silence handling
        # ------------------------------------------------------------------
        silence_state: dict[str, object] = {
            "warning_said": False,
            "hangup_task": None,
        }

        async def _silence_hangup_after(delay: float) -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                logger.info("[SILENCE] hangup task cancelled — user came back")
                return
            current_state = getattr(session, "user_state", "listening")
            if current_state != "away":
                logger.info(
                    f"[SILENCE] hangup fire suppressed — user_state={current_state}"
                )
                return
            if vta_agent._ending:
                logger.info("[SILENCE] hangup fire suppressed — already ending")
                return
            logger.info(
                f"[SILENCE] {SILENCE_TOTAL_SECONDS}s total silence — "
                "invoking force_end_call(status=other)"
            )
            await vta_agent.force_end_call(
                status="other",
                summary=f"Call ended — caller silent for {int(SILENCE_TOTAL_SECONDS)}s after agent finished speaking",
                session=session,
            )

        @session.on("user_state_changed")
        def on_user_state(ev) -> None:
            try:
                if vta_agent._ending:
                    return
                new_state = getattr(ev, "new_state", None)
                if new_state == "away":
                    if silence_state["warning_said"]:
                        return
                    silence_state["warning_said"] = True
                    logger.info(
                        f"[SILENCE] user_state -> away after {USER_AWAY_TIMEOUT}s; "
                        "prompting and starting hangup timer"
                    )
                    try:
                        session.say(SILENCE_PROMPT_TEXT, allow_interruptions=True)
                    except Exception as e:
                        logger.warning(f"[SILENCE] session.say(prompt) failed: {e}")
                    silence_state["hangup_task"] = asyncio.create_task(
                        _silence_hangup_after(SILENCE_FOLLOWUP_DELAY)
                    )
                elif new_state in ("speaking", "listening"):
                    task = silence_state["hangup_task"]
                    if task is not None and not task.done():
                        task.cancel()
                    silence_state["hangup_task"] = None
                    if silence_state["warning_said"]:
                        logger.info(
                            f"[SILENCE] user_state -> {new_state}; reset"
                        )
                    silence_state["warning_said"] = False
            except Exception as e:
                logger.exception(f"[SILENCE] user_state_changed handler failed: {e}")

        # ------------------------------------------------------------------
        # Max call duration watchdog
        # ------------------------------------------------------------------
        async def _max_duration_watchdog(duration: float) -> None:
            try:
                await asyncio.sleep(duration)
            except asyncio.CancelledError:
                return
            if vta_agent._ending:
                logger.info("[WATCHDOG] max duration reached but call already ending")
                return
            logger.info(
                f"[WATCHDOG] max call duration {duration}s reached — ending call"
            )
            await vta_agent.force_end_call(
                status="other",
                summary=f"Call ended — max duration {int(duration)}s reached",
                session=session,
            )

        max_duration_task = asyncio.create_task(
            _max_duration_watchdog(MAX_CALL_DURATION)
        )

        # ------------------------------------------------------------------
        # Room options
        # ------------------------------------------------------------------
        room_options = room_io.RoomOptions(
            participant_kinds=[
                rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
                rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD,
            ],
            delete_room_on_close=False,
        )
        if linked_identity:
            room_options = room_io.RoomOptions(
                participant_kinds=[
                    rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
                    rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD,
                ],
                participant_identity=linked_identity,
                delete_room_on_close=False,
            )

        await session.start(room=ctx.room, agent=vta_agent, room_options=room_options)
        timeline.mark("session.start done (Grok WS connected)")

        linked_participant = getattr(getattr(session, "room_io", None), "linked_participant", None)
        if linked_participant is not None and linked_participant.identity:
            linked_identity = linked_participant.identity
            if linked_participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                vta_agent._sip_identity = linked_participant.identity
            if not phone:
                phone = extract_phone_from_participant(linked_participant)
                vta_agent._phone = phone

        # Background ambience
        background_audio = None
        try:
            background_audio = BackgroundAudioPlayer(
                ambient_sound=AudioConfig(BuiltinAudioClip.OFFICE_AMBIENCE, volume=0.35),
            )
            await background_audio.start(room=ctx.room, agent_session=session)
            logger.info("BackgroundAudioPlayer started (OFFICE_AMBIENCE)")
        except Exception as e:
            logger.error(f"BackgroundAudioPlayer failed to start (call continues without it): {e}")
            background_audio = None
        timeline.mark("background audio started")

        async def _cleanup():
            # Cancel the max-duration watchdog
            if not max_duration_task.done():
                max_duration_task.cancel()
            # Cancel any pending silence hangup
            hangup_task = silence_state.get("hangup_task")
            if hangup_task is not None and not hangup_task.done():
                hangup_task.cancel()
            if background_audio is not None:
                try:
                    await background_audio.aclose()
                except Exception as e:
                    logger.error(f"Error closing background audio: {e}")
            try:
                await http_session.close()
            except Exception as e:
                logger.error(f"Error closing http session: {e}")

        ctx.add_shutdown_callback(_cleanup)

        # on_enter() handles the opening greeting automatically via the Agent
        # lifecycle — no need to call generate_reply here.

        full_name = customer_info.get("full_name", "the customer")
        logger.info(
            f"VTA agent started for {phone} ({full_name}) using "
            f"xai.realtime.RealtimeModel (voice={GROK_VOICE}, "
            f"model={GROK_REALTIME_MODEL or 'default'}, "
            f"silence={USER_AWAY_TIMEOUT}s/{SILENCE_TOTAL_SECONDS}s, "
            f"max_duration={MAX_CALL_DURATION}s)"
        )

    except Exception:
        # Make sure http session is closed on early failure.
        try:
            await http_session.close()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    # `agent_name` puts the worker in EXPLICIT DISPATCH mode — only jobs
    # that explicitly target "vta-emma" land here. Required for production
    # (TCN's SIP dispatch rule names this agent) but breaks agent console /
    # playground, which create rooms and expect any worker to auto-join.
    #
    # Escape hatch: `python agent.py dev` drops the agent_name so the worker
    # auto-dispatches into any new room (including playground rooms).
    import sys as _sys

    _is_dev_mode = (
        len(_sys.argv) > 1 and _sys.argv[1] == "dev"
    ) or os.getenv("AGENT_AUTO_DISPATCH", "").lower() == "true"

    _agent_name = "" if _is_dev_mode else "vta-emma"
    if _is_dev_mode:
        logger.info(
            "[BOOT] dev mode detected — running with agent_name='' so the "
            "worker auto-dispatches into any new room (agent console / playground will work)"
        )
    else:
        logger.info(f"[BOOT] production mode — explicit dispatch only: agent_name='{_agent_name}'")

    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=_agent_name,
        )
    )
