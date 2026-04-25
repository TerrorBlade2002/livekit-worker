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
    RunContext,
    function_tool,
)
from livekit.agents.voice import room_io
from livekit.plugins import openai, silero
from livekit.plugins.google.beta import GeminiTTS

load_dotenv()

logger = logging.getLogger("vta-agent")
logger.setLevel(logging.INFO)

RAILWAY_SERVER_URL = os.getenv("RAILWAY_SERVER_URL", "https://virtual-transfer-agent-production.up.railway.app")

# Cascaded pipeline knobs (env-overridable so we don't have to redeploy to swap voice/model).
# gpt-4o (not -mini) — gpt-4o-mini was unreliable about firing the terminal
# log_verification tool against this long, branchy system prompt. gpt-4o follows
# the Terminal Tool Rule consistently. Override via env if cost is critical.
OPENAI_LLM_MODEL = os.getenv("OPENAI_LLM_MODEL", "gpt-4o")
OPENAI_LLM_TEMPERATURE = float(os.getenv("OPENAI_LLM_TEMPERATURE", "0.7"))
OPENAI_STT_MODEL = os.getenv("OPENAI_STT_MODEL", "whisper-1")
GEMINI_TTS_MODEL = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
# Female, conversational, customer-service-friendly Gemini voice.
# Other good picks: "Leda", "Kore", "Vindemiatrix", "Achird".
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Aoede")
# Gemini API key. Plugin defaults to reading GOOGLE_API_KEY, but our deploy
# uses GEMINI_API_KEY — we read either and pass it explicitly to the plugin.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

CONFIG_DIR = Path(__file__).parent / "config"

# Closing messages are controlled server-side (deterministic) so hangup is
# reliable and in Emma's voice; the LLM MUST NOT speak its own goodbye.
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


async def fetch_customer_info(phone: str) -> dict:
    """Call the Railway server's /retell-webhook to look up customer data."""
    normalized = normalize_phone(phone)
    payload = {"call_inbound": {"from_number": f"+1{normalized}"}}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{RAILWAY_SERVER_URL}/retell-webhook",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
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
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{RAILWAY_SERVER_URL}/retell-call-ended",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                logger.info(f"call_ended notify for {normalized}: HTTP {resp.status}")
    except Exception as e:
        logger.error(f"Error notifying call_ended: {e}")


async def log_verification_to_server(phone: str, status: str, summary: str, full_name: str) -> dict:
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
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{RAILWAY_SERVER_URL}/log-verification",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()
                logger.info(f"Log verification response for {normalized}: {data}")
                return data
    except Exception as e:
        logger.error(f"Error logging verification: {e}")
        return {"error": str(e)}


class VTAAgent(Agent):
    """Virtual Transfer Agent — Emma (LiveKit replacement for Retell's Emma)."""

    def __init__(
        self,
        phone: str,
        customer_info: dict,
        ctx: agents.JobContext | None = None,
        sip_identity: str = "",
    ):
        self._phone = phone
        self._customer_info = customer_info
        self._call_started_at = time.monotonic()
        self._call_end_notified = False
        self._ctx = ctx
        self._sip_identity = sip_identity
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
        """Best-effort: figure out which participant identity is the SIP leg from TCN."""
        linked_participant = getattr(getattr(session, "room_io", None), "linked_participant", None)
        if (
            linked_participant is not None
            and linked_participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        ):
            self._sip_identity = linked_participant.identity or self._sip_identity

        participant = find_primary_sip_participant(session.room, preferred_identity=self._sip_identity)
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
    # We follow LiveKit's canonical end-call pattern (telephony docs):
    # do EVERYTHING inline-awaited inside the tool body. This guarantees
    # the LLM cannot generate a stray follow-up reply before the SIP leg
    # is gone, and that the BYE reaches TCN cleanly so the broadcast
    # template fires Action OK and advances:
    #   Linkback (Action OK) -> Data Dip -> Set Value -> Hunt Group.
    #
    # Specifically NOT used here:
    #   - asyncio.create_task(...) for the end-call sequence — the prior
    #     fire-and-forget pattern raced with the LLM's auto-generated
    #     follow-up reply, often resulting in either a half-spoken closing
    #     or a missed BYE that TCN treated as Action Error.
    #   - session.interrupt() — wrong tool for the job in cascaded mode;
    #     it cancels the speech queue but doesn't prevent the LLM from
    #     enqueueing fresh audio after the tool returns.
    #   - delete_room — produces disconnect_reason=ROOM_DELETED on the BYE
    #     which some TCN templates treat as abnormal. We use
    #     RemoveParticipant on SIP only (PARTICIPANT_REMOVED) so TCN sees
    #     a clean leg-B teardown and fires Action OK.
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
        logger.info(
            f"log_verification called: phone={self._phone} status={status} "
            f"summary={summary!r}"
        )

        # Idempotency — if the LLM somehow calls this twice, no-op the second.
        if self._ending:
            logger.warning("log_verification called twice — second call ignored")
            return None
        self._ending = True
        self._full_name = full_name or self._full_name

        # Disable interruptions for the rest of this tool call. Per LiveKit
        # docs, this is required for any tool that takes non-rollbackable
        # external actions (we're about to log + speak + hang up SIP).
        try:
            context.disallow_interruptions()
        except Exception as e:
            logger.warning(f"disallow_interruptions failed (continuing): {e}")

        session: AgentSession = context.session
        room = session.room
        room_name = room.name or ""
        closing = CLOSING_MESSAGES.get(status, CLOSING_MESSAGES["other"])

        # Step 1 — log to the Railway server (Retell-compatible contract).
        # Best-effort: failure here doesn't block hangup; the server-side
        # pruner reconciles via /retell-call-ended below.
        try:
            await log_verification_to_server(self._phone, status, summary, full_name)
        except Exception as e:
            logger.error(f"log_verification_to_server failed; continuing: {e}")

        # Step 2 — drain any speech that's already in flight (e.g. the LLM
        # was mid-sentence when it decided to call this tool). Per docs:
        # "Use ctx.wait_for_playout() to wait for any pre-tool speech to finish."
        try:
            await context.wait_for_playout()
        except Exception as e:
            logger.warning(f"wait_for_playout (pre-closing) failed: {e}")

        # Step 3 — speak the deterministic closing INLINE and wait for it
        # to fully play out. allow_interruptions=False so caller speech
        # (or anything else) can't truncate it.
        try:
            handle = await session.say(closing, allow_interruptions=False)
            if handle is not None and hasattr(handle, "wait_for_playout"):
                await handle.wait_for_playout()
            else:
                # Fallback timing estimate (~14 chars/sec TTS rate).
                await asyncio.sleep(max(3.0, len(closing) / 14.0))
        except Exception as e:
            logger.error(f"session.say closing failed ({status}): {e}")
            # Don't let TTS failure block hangup. Short safety wait so any
            # in-flight audio drains before we drop the SIP leg.
            await asyncio.sleep(1.5)

        # Step 4 — notify the Railway server the call ended (Retell-compatible).
        # Best-effort, fire-and-forget on failure.
        if not self._call_end_notified:
            duration_ms = max(0, int((time.monotonic() - self._call_started_at) * 1000))
            try:
                await notify_call_ended(
                    phone=self._phone,
                    call_id=room_name,
                    duration_ms=duration_ms,
                    disconnection_reason=f"agent_end_call:{status}",
                )
            except Exception as e:
                logger.error(f"notify_call_ended failed ({status}): {e}")
            self._call_end_notified = True

        # Step 5 — locate the customer-facing SIP leg (TCN's leg B).
        sip_identity = self._resolve_sip_identity(session)

        # Step 6 — surgical hangup: remove ONLY the SIP participant.
        # This sends a clean BYE on leg B back to TCN with
        # disconnect_reason=PARTICIPANT_REMOVED. TCN's broadcast template
        # treats this as Action OK and advances:
        #   Linkback -> Data Dip (/verification-status) -> Set Value -> Hunt Group
        # Leg A (TCN <-> Customer) is untouched.
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
                    f"— SIP participant removed, BYE en route to TCN"
                )
            except Exception as e:
                logger.error(
                    f"[END_CALL] remove_participant failed for "
                    f"{sip_identity} in {room_name}: {e}"
                )

        # Step 7 — fallback if we couldn't identify/remove the SIP participant.
        # delete_room is a heavier hammer (BYE with disconnect_reason=ROOM_DELETED)
        # but at least guarantees the call ends rather than leaving a stuck leg.
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
                    # Last resort — disconnect ourselves so the worker job doesn't hang.
                    try:
                        await room.disconnect()
                    except Exception as e2:
                        logger.error(f"[END_CALL] room.disconnect last-resort failed: {e2}")

        # Return None so the LLM has no string to chew on. The SIP leg is
        # already gone by this point, so any follow-up the LLM tries to emit
        # has no audience.
        return None


async def entrypoint(ctx: agents.JobContext):
    """Main entrypoint — dispatched for each inbound SIP call from TCN."""
    logger.info(f"Agent entrypoint called. Room: {ctx.room.name}")

    await ctx.connect()

    phone = ""
    sip_identity = ""
    linked_identity = ""
    customer_info = {}

    logger.info("Waiting for SIP participant...")

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
                "Primary SIP participant: identity=%s callStatus=%s phone=%s metadata=%s",
                sip_participant.identity,
                (sip_participant.attributes or {}).get("sip.callStatus", ""),
                phone,
                sip_participant.metadata,
            )
            return

        standard_participant = find_primary_standard_participant(
            ctx.room,
            preferred_identity=linked_identity,
        )
        if standard_participant is not None:
            linked_identity = standard_participant.identity or linked_identity
            logger.info(
                "Primary standard participant for console/dev: identity=%s metadata=%s",
                standard_participant.identity,
                standard_participant.metadata,
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
            await asyncio.wait_for(participant_connected.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("No SIP participant joined within 30s. Using room metadata.")

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

    if phone:
        customer_info = await fetch_customer_info(phone)

    if not customer_info.get("full_name"):
        customer_info["full_name"] = "the customer"
        logger.warning(f"No customer info found for phone {phone}")

    vta_agent = VTAAgent(
        phone=phone,
        customer_info=customer_info,
        ctx=ctx,
        sip_identity=sip_identity,
    )

    # Cascaded STT -> LLM -> TTS pipeline.
    #
    # Why cascaded (vs the previous Realtime model):
    #   - Verbatim closings: TTS faithfully speaks the exact CLOSING_MESSAGES
    #     text. Realtime models paraphrase even when instructed not to.
    #   - Cheaper per minute (Whisper + 4o-mini + Gemini Flash TTS).
    #   - Easy to swap any leg independently via env vars.
    #
    # STT: OpenAI Whisper, English-pinned (avoid silent failures from foreign
    #      transcripts upstream of the LLM).
    # LLM: OpenAI gpt-4o by default — gpt-4o-mini was unreliable about calling
    #      the terminal log_verification tool against this long system prompt.
    #      Override via OPENAI_LLM_MODEL if cost is critical.
    # TTS: Google Gemini 2.5 Flash Preview TTS. The plugin's default
    #      `instructions` ("Say the text with a proper tone, don't omit or
    #      add any words") is exactly the verbatim guarantee we want for
    #      compliance-sensitive closings.
    # VAD: Silero (required for cascaded pipelines — drives turn detection).
    session = AgentSession(
        stt=openai.STT(
            model=OPENAI_STT_MODEL,
            language="en",
            prompt="English only. Transcribe non-English words phonetically in English characters.",
        ),
        llm=openai.LLM(
            model=OPENAI_LLM_MODEL,
            temperature=OPENAI_LLM_TEMPERATURE,
        ),
        tts=GeminiTTS(
            model=GEMINI_TTS_MODEL,
            voice_name=GEMINI_VOICE,
            api_key=GEMINI_API_KEY,
        ),
        vad=silero.VAD.load(),
    )

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

    linked_participant = getattr(getattr(session, "room_io", None), "linked_participant", None)
    if linked_participant is not None and linked_participant.identity:
        linked_identity = linked_participant.identity
        if linked_participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            vta_agent._sip_identity = linked_participant.identity
        if not phone:
            phone = extract_phone_from_participant(linked_participant)
            vta_agent._phone = phone

    # Ambient call-center background audio — published on a separate outbound
    # track, never mixed into the Realtime TTS stream or fed back into STT.
    # Wrapped in try/except so a BackgroundAudioPlayer failure can never abort
    # the call — Emma will still speak, just without ambience.
    background_audio = None
    try:
        background_audio = BackgroundAudioPlayer(
            ambient_sound=AudioConfig(BuiltinAudioClip.OFFICE_AMBIENCE, volume=0.35),
            thinking_sound=[
                AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING, volume=0.4, probability=0.7),
                AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING2, volume=0.4, probability=0.3),
            ],
        )
        await background_audio.start(room=ctx.room, agent_session=session)
        logger.info("BackgroundAudioPlayer started (OFFICE_AMBIENCE)")
    except Exception as e:
        logger.error(f"BackgroundAudioPlayer failed to start (call continues without it): {e}")
        background_audio = None

    async def _cleanup_background_audio():
        if background_audio is not None:
            try:
                await background_audio.aclose()
            except Exception as e:
                logger.error(f"Error closing background audio: {e}")

    ctx.add_shutdown_callback(_cleanup_background_audio)

    full_name = customer_info.get("full_name", "the customer")
    await session.generate_reply(
        instructions=load_prompt("opening_line.md").format(full_name=full_name)
    )

    logger.info(f"VTA agent started for {phone} ({full_name})")


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="vta-emma",
        )
    )
