import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

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
from livekit.plugins.xai import realtime as xai_realtime

# ---------------------------------------------------------------------------
# .env loading — robust against arbitrary cwd
#
# load_dotenv() with no arguments looks at the CURRENT WORKING DIRECTORY (and
# walks up). That's fragile for two reasons:
#  1) If you launch the worker from any directory other than livekit-worker/
#     (e.g. cd .. && python livekit-worker/agent.py), .env isn't found.
#  2) LiveKit's job-process spawn (`multiprocessing_context="spawn"`)
#     re-imports this module in a fresh subprocess. Depending on the OS
#     and how spawn is configured, the child's cwd may not match the
#     parent's, so a .env that loaded fine in the supervisor may NOT load
#     in the spawned job process.
#
# Fix: pin the search to the .env that lives next to this file.
# Override=True so a child process picks up any later changes.
# ---------------------------------------------------------------------------
_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=True)

logger = logging.getLogger("vta-agent")
logger.setLevel(logging.INFO)

# Surface where we tried to load .env from — useful when debugging
# "key not loaded" issues across spawn/Docker/Railway boundaries.
logger.info(
    f"[BOOT] .env path: {_ENV_PATH} (exists={_ENV_PATH.exists()})"
)


def _resolve_xai_api_key() -> str | None:
    """Read XAI_API_KEY (or GROK_API_KEY) at call time, not at import time.

    Reading at call time is important because:
     - In dev, you might edit .env between worker restarts; load_dotenv
       runs again at module import, but only if the module is re-imported.
     - In subprocess spawn, the env is re-loaded but anyone who captured
       it into a module-level constant would still hold the OLD value.

    Returns the key (str) or None if neither var is set.
    """
    return os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY")


RAILWAY_SERVER_URL = os.getenv(
    "RAILWAY_SERVER_URL", "https://virtual-transfer-agent-production.up.railway.app"
)

# ---------------------------------------------------------------------------
# Grok Realtime config
#
# We use the xAI Realtime API end-to-end (single STT+LLM+TTS model) to
# minimise per-turn latency. Underlying model is `grok-4-1-fast-non-reasoning`
# (hardcoded by the xai plugin's RealtimeModel as of livekit-plugins-xai 1.5.x).
# This is NOT a chain-of-thought model — if you need reasoning, switch to the
# cascaded path (LLM.with_x_ai(model="grok-4-1-fast-reasoning")), which costs
# ~300-600ms more TTFT per turn.
# ---------------------------------------------------------------------------
# Grok voices: Ara (female, default), Eve (female), Leo (male), Rex (male), Sal (male).
GROK_VOICE = os.getenv("GROK_VOICE", "Ara")

# AgentSession latency knobs — see docstrings on AgentSession for full details.
# aec_warmup_duration default is 3.0s, which delays the opening line by
# the same amount. SIP calls don't need AEC (audio is unidirectional through
# TCN's SIP gateway), so we set it to 0 to claw back ~3s on the first reply.
AEC_WARMUP_DURATION = float(os.getenv("AEC_WARMUP_DURATION", "0"))
# preemptive_generation lets the LLM start composing a reply BEFORE the
# user's turn is fully ended — significant mid-call latency win.
PREEMPTIVE_GENERATION = os.getenv("PREEMPTIVE_GENERATION", "true").lower() == "true"
# Endpointing — how long to wait after user stops speaking before declaring
# turn end. Lower = snappier but more false interruptions.
MIN_ENDPOINTING_DELAY = float(os.getenv("MIN_ENDPOINTING_DELAY", "0.4"))
MAX_ENDPOINTING_DELAY = float(os.getenv("MAX_ENDPOINTING_DELAY", "3.0"))

CONFIG_DIR = Path(__file__).parent / "config"

# Closing messages are deterministic so hangup is reliable and in Emma's
# voice; the LLM MUST NOT speak its own goodbye. We pass them through
# generate_reply with verbatim instructions because Realtime models speak
# via their own internal TTS pipeline (no separate session.say-able TTS).
CLOSING_MESSAGES = {
    "verified": (
        "Thank you. We're calling regarding a personal business matter of yours. "
        "Please hold for a moment while I transfer you to our representative who can assist you further."
    ),
    "customer_wants_human": (
        "Please hold for a moment while I connect you to an agent to assist you further."
    ),
    "wrong_number": (
        "I apologize for the inconvenience — I'll go ahead and remove this number from our list "
        "so you won't get any more calls from us. Thank you, goodbye."
    ),
    "third_party_end": (
        "Thank you for your time. Have a nice day!"
    ),
    "consumer_busy_end": (
        "Thank you for your time. Have a nice day!"
    ),
    "dnc": (
        "I apologize for the inconvenience — I'll go ahead and remove your number from our list "
        "so you won't get any more calls from us. Thank you, goodbye."
    ),
    "other": (
        "I apologize if this call caused any inconvenience. Thank you for your time — "
        "our representatives may try again later or contact you regarding the matter. Goodbye."
    ),
}

TCN_TRANSFER_STATUSES = {"verified", "customer_wants_human"}


# ---------------------------------------------------------------------------
# Observability — Timeline helper
#
# Every entrypoint creates a Timeline and marks the wall-clock delta at each
# stage. Logs go out as `[TTFT:<room>] +<delta>ms (total=<total>ms) <stage>`
# so you can grep Railway logs for `[TTFT:` and see exactly where time was
# spent on the linkback path.
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
    """Read a prompt template from the config directory.

    Templates use Python str.format placeholders (e.g. {full_name}).
    Edit the files in config/ to tweak prompts without touching code.
    """
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
    """Extract a real customer phone number from participant state.

    Do not fall back to `sip.callID` here. That is a SIP call tag, not the
    customer phone number that the webhook server and TCN expect.
    """
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


async def fetch_customer_info(phone: str, http: aiohttp.ClientSession | None = None) -> dict:
    """Call the Railway server's /retell-webhook to look up customer data.

    Accepts an optional shared aiohttp session — saves ~50-100ms vs spinning
    up a new ClientSession per call (TLS + connection setup).
    """
    normalized = normalize_phone(phone)
    payload = {"call_inbound": {"from_number": f"+1{normalized}"}}
    timeout = aiohttp.ClientTimeout(total=3)  # tightened from 5s — server is local-region, p99 well under 1s
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
    """Log verification result to Railway server, same contract as Retell's log_verification function."""
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


class VTAAgent(Agent):
    """Virtual Transfer Agent — Emma (LiveKit replacement for Retell's Emma).

    Now powered by xAI Grok Realtime — single end-to-end voice model, no
    cascaded STT/LLM/TTS pipeline. This collapses per-turn latency to the
    Realtime model's TTFT (typically 300-600ms) instead of
    STT_latency + LLM_latency + TTS_first_chunk_latency (1-2s aggregate).
    """

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
        self._http = http  # shared aiohttp session from prewarm
        self._ending = False  # guards against double-trigger of the end-call sequence

        full_name = customer_info.get("full_name", "the customer")
        company_name = customer_info.get("company_name", "our company")
        company_address = customer_info.get("company_address", "")
        call_back_number = customer_info.get("call_back_number", "")
        now_utc = datetime.now(timezone.utc).strftime("%A, %B %d, %Y %H:%M UTC")

        instructions = load_prompt("system_prompt.md").format(
            full_name=full_name,
            company_name=company_name,
            company_address=company_address,
            call_back_number=call_back_number,
            now_utc=now_utc,
        )

        super().__init__(instructions=instructions)
        self._full_name = full_name

    def _resolve_sip_identity(self, session: AgentSession) -> str:
        """Best-effort: figure out which participant identity is the SIP leg from TCN.

        AgentSession in livekit-agents 1.x does NOT expose `.room` directly —
        the room lives on `session.room_io.room` AND on `self._ctx.room`. We
        prefer `self._ctx.room` (JobContext.room) because it's stable from the
        moment the job starts. We also try `room_io.linked_participant` as a
        fast path for the "this is definitely the SIP leg" case.
        """
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
    # log_verification — the single terminal tool.
    #
    # Mirrors TCN's bridge architecture:
    #   - TCN leg A: TCN <-> Customer (TCN owns this — never touched here)
    #   - TCN leg B: TCN <-> LiveKit SIP gateway (the SIP participant in
    #     this room is the LiveKit-side endpoint of leg B)
    #   - LiveKit room: { SIP participant, vta-emma agent }
    #
    # With Realtime models, we use generate_reply(instructions=...) for the
    # closing because session.say bypasses the model's own TTS in some
    # configurations. generate_reply with verbatim instructions makes the
    # Realtime model speak the exact closing through its own audio pipeline.
    # ------------------------------------------------------------------
    @function_tool()
    async def log_verification(
        self,
        context: RunContext,
        status: str,
        summary: str,
        full_name: str,
    ) -> None:
        """Log the terminal call outcome AND end the call.

        Call this exactly ONCE per call, at the terminal point of the
        conversation. The system will speak the appropriate closing line
        and tear down the SIP leg back to TCN. You MUST NOT speak any
        goodbye yourself — the system owns the closing phrase.

        Args:
            status: The verification outcome. Must be one of:
                "verified", "wrong_number", "third_party_end",
                "consumer_busy_end", "dnc", "customer_wants_human", "other"
            summary: Brief one-line description of what happened during the call.
            full_name: The customer's name.
        """
        # Outermost shield: this tool MUST NOT raise. If it raises, the LLM
        # gets an error response and starts ad-libbing ("An internal error
        # has occurred"), which doesn't end the call. Every code path below
        # is wrapped, and we always return None.
        try:
            tool_t0 = time.monotonic()
            logger.info(
                f"log_verification called: phone={self._phone} status={status} "
                f"summary={summary!r}"
            )

            if self._ending:
                logger.warning("log_verification called twice — second call ignored")
                return None
            self._ending = True
            self._full_name = full_name or self._full_name

            try:
                context.disallow_interruptions()
            except Exception as e:
                logger.warning(f"disallow_interruptions failed (continuing): {e}")

            session: AgentSession = context.session
            room: rtc.Room | None = None
            if self._ctx is not None:
                room = getattr(self._ctx, "room", None)
            if room is None:
                room = getattr(getattr(session, "room_io", None), "room", None)

            room_name = (room.name if room is not None else "") or ""
            closing = CLOSING_MESSAGES.get(status, CLOSING_MESSAGES["other"])

            # Step 1 — log to the Railway server (Retell-compatible contract).
            try:
                await log_verification_to_server(self._phone, status, summary, full_name, http=self._http)
            except Exception as e:
                logger.error(f"log_verification_to_server failed; continuing: {e}")
            logger.info(f"[END_CALL_TIMING] log_to_server +{(time.monotonic()-tool_t0)*1000:.1f}ms")

            # Step 2 — drain any speech already in flight.
            try:
                await context.wait_for_playout()
            except Exception as e:
                logger.warning(f"wait_for_playout (pre-closing) failed: {e}")

            # Step 3 — speak the verbatim closing through the Realtime model.
            # We use generate_reply with strict verbatim instructions because
            # Realtime models route ALL audio output through their internal
            # pipeline; session.say with no separate TTS configured falls
            # back to the realtime model anyway.
            spoke_ok = False
            speak_t0 = time.monotonic()
            try:
                speech_handle = await session.generate_reply(
                    instructions=(
                        "Speak the following text VERBATIM, exactly word-for-word. "
                        "Do NOT add, remove, summarize, paraphrase, or change anything. "
                        "Speak in a warm, polite tone, then stop:\n\n"
                        f'"{closing}"'
                    ),
                )
                if speech_handle is not None and hasattr(speech_handle, "wait_for_playout"):
                    await speech_handle.wait_for_playout()
                spoke_ok = True
            except Exception as e:
                logger.error(f"generate_reply closing failed ({status}): {e}")

            if not spoke_ok:
                # Fallback timing estimate (~14 chars/sec TTS rate).
                try:
                    await asyncio.sleep(max(3.0, len(closing) / 14.0))
                except Exception:
                    pass
            logger.info(f"[END_CALL_TIMING] closing_spoken +{(time.monotonic()-speak_t0)*1000:.1f}ms")

            # Step 4 — notify the Railway server the call ended.
            if not self._call_end_notified:
                duration_ms = max(0, int((time.monotonic() - self._call_started_at) * 1000))
                try:
                    await notify_call_ended(
                        phone=self._phone,
                        call_id=room_name,
                        duration_ms=duration_ms,
                        disconnection_reason=f"agent_end_call:{status}",
                        http=self._http,
                    )
                except Exception as e:
                    logger.error(f"notify_call_ended failed ({status}): {e}")
                self._call_end_notified = True

            # Step 5 — locate the customer-facing SIP leg (TCN's leg B).
            sip_identity = ""
            try:
                sip_identity = self._resolve_sip_identity(session)
            except Exception as e:
                logger.error(f"[END_CALL] _resolve_sip_identity failed: {e}")

            # Step 6 — surgical hangup: remove ONLY the SIP participant.
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
                        f"[END_CALL] status={status} phone={self._phone} "
                        f"room={room_name} sip_identity={sip_identity} "
                        f"tcn_http={tcn_http_code_for_status(status)} "
                        f"total={(time.monotonic()-tool_t0)*1000:.1f}ms "
                        f"— SIP participant removed, BYE en route to TCN"
                    )
                except Exception as e:
                    logger.error(
                        f"[END_CALL] remove_participant failed for "
                        f"{sip_identity} in {room_name}: {e}"
                    )

            # Step 7 — fallback if we couldn't identify/remove the SIP participant.
            if not removed_ok:
                logger.warning(
                    f"[END_CALL] no SIP identity to remove "
                    f"(sip_identity={sip_identity or 'not-found'}); falling back to delete_room"
                )
                if self._ctx is not None and room_name:
                    try:
                        await self._ctx.api.room.delete_room(
                            api.DeleteRoomRequest(room=room_name)
                        )
                        logger.info(f"[END_CALL] delete_room fallback fired for room={room_name}")
                    except Exception as e:
                        logger.error(f"[END_CALL] delete_room fallback failed: {e}")
                        if room is not None:
                            try:
                                await room.disconnect()
                            except Exception as e2:
                                logger.error(f"[END_CALL] room.disconnect last-resort failed: {e2}")

            return None

        except Exception as fatal:
            logger.exception(f"[END_CALL] FATAL in log_verification: {fatal}")
            try:
                if self._ctx is not None:
                    fallback_room = getattr(self._ctx, "room", None)
                    fallback_name = (fallback_room.name if fallback_room is not None else "") or ""
                    if fallback_name:
                        try:
                            await self._ctx.api.room.delete_room(
                                api.DeleteRoomRequest(room=fallback_name)
                            )
                            logger.info(
                                f"[END_CALL] FATAL-path delete_room fired for room={fallback_name}"
                            )
                        except Exception as e:
                            logger.error(f"[END_CALL] FATAL-path delete_room failed: {e}")
            except Exception as e:
                logger.error(f"[END_CALL] FATAL-path shield itself failed: {e}")
            return None


# ---------------------------------------------------------------------------
# prewarm — runs ONCE per worker process at startup, before any jobs land.
#
# Use this to load anything heavy that would otherwise add latency to the
# first call. We share an aiohttp.ClientSession so per-call HTTPS handshakes
# to Railway aren't re-established cold.
# ---------------------------------------------------------------------------
def prewarm(proc: JobProcess) -> None:
    """Pre-load expensive, reusable resources before the first job arrives.

    Also re-loads .env. Reason: with multiprocessing_context="spawn" (the
    default), each job process is a fresh Python interpreter that imports
    this module from scratch. The module-level load_dotenv() runs in that
    context — but if the spawned process's cwd differs from the supervisor's,
    a relative .env lookup would miss. We re-call load_dotenv() with an
    explicit path here as a belt-and-braces guarantee.
    """
    t0 = time.monotonic()
    load_dotenv(dotenv_path=_ENV_PATH, override=True)
    proc.userdata["http_session"] = None
    xai_present = bool(_resolve_xai_api_key())
    elapsed = (time.monotonic() - t0) * 1000.0
    logger.info(
        f"[PREWARM] worker process initialized in {elapsed:.1f}ms "
        f"(env={_ENV_PATH}, exists={_ENV_PATH.exists()}, xai_key_loaded={xai_present})"
    )


async def entrypoint(ctx: agents.JobContext):
    """Main entrypoint — dispatched for each inbound SIP call from TCN.

    Latency budget on this path (target):
      0-50ms    entrypoint called (job dispatched)
      50-150ms  ctx.connect (room WS handshake)
      150-300ms SIP participant present (TCN INVITE → LiveKit SIP gateway)
      300-400ms customer info fetched (parallel with model setup)
      400-500ms session.start done
      500-1500ms first audio frame from Grok Realtime (TTFT)

    Anything that creeps in beyond that shows up in the [TTFT:...] log lines.
    """
    timeline = Timeline(ctx.room.name or "unknown")

    # CONNECT FIRST. Always join the room before doing anything else so:
    #  (a) the agent visibly appears in agent console / playground / SIP room
    #      even if downstream setup blows up, which makes failures much
    #      easier to debug than a silent no-show.
    #  (b) the LiveKit job is acknowledged — without ctx.connect() the
    #      worker holds the job slot but never actually joins, which looks
    #      identical to "agent isn't running" from the user's side.
    try:
        await ctx.connect()
    except Exception as e:
        logger.exception(f"ctx.connect() failed — cannot proceed: {e}")
        return
    timeline.mark("ctx.connect done")

    # Shared HTTP session — saves TLS+connection-setup on every Railway call (~50-150ms).
    http_session = aiohttp.ClientSession()
    timeline.mark("http session up")

    # Re-read .env at job time (cheap) and re-resolve the key. This protects
    # against the spawn-subprocess case where a stale module-level constant
    # would hold None even though the env var IS set in the spawned process's
    # environment after a re-load.
    load_dotenv(dotenv_path=_ENV_PATH, override=True)
    xai_api_key = _resolve_xai_api_key()

    if not xai_api_key:
        # Loud, visible failure path. We're already in the room so the user
        # can see the agent joined; the error explains why nothing else happens.
        logger.error(
            "\n" + "=" * 70 + "\n"
            "  XAI_API_KEY is NOT SET. Grok Realtime cannot connect.\n"
            f"  Tried to load .env from: {_ENV_PATH}\n"
            f"  .env exists at that path: {_ENV_PATH.exists()}\n"
            f"  cwd: {os.getcwd()}\n"
            "  Set XAI_API_KEY (or GROK_API_KEY) in your .env or Railway env vars.\n"
            "  Get a key from https://console.x.ai/\n"
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
                # Tightened from 30s to 10s — TCN INVITE → LiveKit SIP participant
                # is sub-second in normal operation. 10s is generous for retries.
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

        # PARALLELISM WIN: kick off customer info fetch while we're building
        # the model and agent. Both finish in ~50-200ms; doing them sequentially
        # added 200-400ms to TTFT.
        if phone:
            customer_info_task = asyncio.create_task(fetch_customer_info(phone, http=http_session))
        else:
            customer_info_task = None
        timeline.mark("customer info fetch fired (async)")

        # Build Grok Realtime model. Constructor is cheap — actual WS connection
        # to wss://api.x.ai/v1/realtime opens during session.start.
        # Use the runtime-resolved key (xai_api_key) — the old module-level
        # XAI_API_KEY constant has been removed because it would freeze a
        # stale value at import time across spawn-subprocess boundaries.
        rt_model = xai_realtime.RealtimeModel(
            voice=GROK_VOICE,
            api_key=xai_api_key,
        )
        timeline.mark("realtime model constructed")

        # Await customer info now (will be ready by this point in most cases).
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
            # AEC warmup default is 3.0s — for SIP audio (unidirectional via
            # gateway) AEC isn't needed, so we set this to 0 to claw back ~3s
            # off the opening line TTFT.
            aec_warmup_duration=AEC_WARMUP_DURATION,
            # Start composing replies before the user fully finishes (mid-call
            # latency win — typically 200-500ms saved per turn).
            preemptive_generation=PREEMPTIVE_GENERATION,
            min_endpointing_delay=MIN_ENDPOINTING_DELAY,
            max_endpointing_delay=MAX_ENDPOINTING_DELAY,
        )

        # Observability: log per-turn latency from the model itself.
        # RealtimeModelMetrics has `ttft` (seconds) — that's the model's own
        # time-to-first-token, distinct from our wall-clock end-to-end TTFT.
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

        # Background ambience — wrapped so a player failure never aborts the call.
        background_audio = None
        try:
            background_audio = BackgroundAudioPlayer(
                ambient_sound=AudioConfig(BuiltinAudioClip.OFFICE_AMBIENCE, volume=0.35),
                # No thinking_sound for Realtime — TTFT is sub-second so the
                # keyboard typing sound would clash with the model's own response.
            )
            await background_audio.start(room=ctx.room, agent_session=session)
            logger.info("BackgroundAudioPlayer started (OFFICE_AMBIENCE)")
        except Exception as e:
            logger.error(f"BackgroundAudioPlayer failed to start (call continues without it): {e}")
            background_audio = None
        timeline.mark("background audio started")

        async def _cleanup():
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

        # Trigger the opening line. With Realtime, this is a generate_reply
        # with the verbatim opening as the instruction.
        full_name = customer_info.get("full_name", "the customer")
        opening = load_prompt("opening_line.md").format(full_name=full_name)
        await session.generate_reply(
            instructions=(
                "Speak the following greeting VERBATIM, exactly word-for-word, "
                "in a warm, polite tone, then stop and wait for the caller's reply:\n\n"
                f'"{opening}"'
            ),
        )
        timeline.mark("opening reply queued")

        logger.info(f"VTA agent started for {phone} ({full_name}) using Grok Realtime ({GROK_VOICE})")

    except Exception:
        # Make sure http session is closed on early failure.
        try:
            await http_session.close()
        except Exception:
            pass
        raise


if __name__ == "__main__":
    # `agent_name` puts the worker in EXPLICIT DISPATCH mode — only jobs
    # that explicitly target "vta-emma" land here. That's required for
    # production (TCN's SIP dispatch rule names this agent) but it BREAKS
    # agent console / playground, which create rooms and expect any worker
    # to auto-join.
    #
    # Escape hatch: when running `python agent.py dev`, drop the agent_name
    # so the worker auto-dispatches into any new room (including playground
    # rooms). For `python agent.py start` (production), keep the explicit
    # name. You can also force the dev behavior in any mode by setting
    # AGENT_AUTO_DISPATCH=true in the env.
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
