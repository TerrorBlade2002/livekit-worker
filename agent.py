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
from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    RunContext,
    function_tool,
)
from livekit.plugins import openai
from livekit.plugins.openai.realtime.realtime_model import AudioTranscription

load_dotenv()

logger = logging.getLogger("vta-agent")
logger.setLevel(logging.INFO)

RAILWAY_SERVER_URL = os.getenv("RAILWAY_SERVER_URL", "https://virtual-transfer-agent-production.up.railway.app")

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

    def __init__(self, phone: str, customer_info: dict):
        self._phone = phone
        self._customer_info = customer_info
        self._call_started_at = time.monotonic()
        self._call_end_notified = False
        self._ending = False  # prevents double-disconnect

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

    @function_tool()
    async def log_verification(
        self,
        context: RunContext,
        status: str,
        summary: str,
        full_name: str,
    ) -> str:
        """Log the verification outcome to the server AND end the call.

        You MUST call this exactly once before ending any call. After this function
        is called, the system will speak the appropriate closing message and then
        disconnect — DO NOT speak a closing or goodbye yourself.

        Args:
            status: The verification outcome. Must be one of:
                "verified", "wrong_number", "third_party_end",
                "consumer_busy_end", "dnc", "customer_wants_human", "other"
            summary: Brief one-line description of what happened during the call.
            full_name: The customer's name.
        """
        logger.info(f"log_verification called: phone={self._phone}, status={status}, summary={summary}")

        if self._ending:
            logger.warning("log_verification called twice — ignoring second invocation")
            return json.dumps({"success": True, "status": status, "message": "Already ending."})
        self._ending = True

        # 1. Log verification to server (Retell-compatible endpoint).
        await log_verification_to_server(self._phone, status, summary, full_name)

        # 2. Kick off the deterministic closing + disconnect in the background so
        #    this tool-call returns promptly and the model doesn't synthesize its
        #    own goodbye over ours.
        asyncio.create_task(self._speak_closing_and_disconnect(context, status))

        return json.dumps({
            "success": True,
            "status": status,
            "message": (
                "Verification logged. The system will now speak the closing "
                "message and end the call. Do NOT speak anything else."
            ),
        })

    async def _speak_closing_and_disconnect(self, context: RunContext, status: str) -> None:
        """Speak a deterministic closing line, wait for playout, notify server, then disconnect."""
        try:
            session: AgentSession = context.session
            room = session.room
            closing = CLOSING_MESSAGES.get(status, CLOSING_MESSAGES["other"])

            # Small grace period to let the Realtime model's tool-result echo settle
            # before we queue our closing utterance.
            await asyncio.sleep(0.3)

            # session.say() returns a SpeechHandle. Await its playout so we only
            # disconnect after Emma has actually said the closing line.
            try:
                handle = await session.say(closing, allow_interruptions=False)
                if handle is not None and hasattr(handle, "wait_for_playout"):
                    await handle.wait_for_playout()
                else:
                    # Fallback: length-based estimate (~14 chars/sec TTS).
                    await asyncio.sleep(max(3.0, len(closing) / 14.0))
            except Exception as e:
                logger.error(f"Error during session.say closing: {e}")
                # Fallback wait so we don't cut off a partial playout.
                await asyncio.sleep(5.0)

            # 3. Notify the server that the call ended (Retell-compatible webhook).
            if not self._call_end_notified:
                duration_ms = max(0, int((time.monotonic() - self._call_started_at) * 1000))
                await notify_call_ended(
                    phone=self._phone,
                    call_id=room.name or "",
                    duration_ms=duration_ms,
                    disconnection_reason=f"agent_disconnect_after_log_verification:{status}",
                )
                self._call_end_notified = True

            # 4. Hang up — drop the agent's SIP leg. TCN detects disconnect and moves on.
            await room.disconnect()
            logger.info(f"Agent disconnected from room for phone {self._phone} (status={status})")
        except Exception as e:
            logger.error(f"Error in _speak_closing_and_disconnect: {e}")


async def entrypoint(ctx: agents.JobContext):
    """Main entrypoint — dispatched for each inbound SIP call from TCN."""
    logger.info(f"Agent entrypoint called. Room: {ctx.room.name}")

    await ctx.connect()

    phone = ""
    customer_info = {}

    logger.info("Waiting for SIP participant...")

    def find_sip_phone() -> str:
        for p in ctx.room.remote_participants.values():
            identity = p.identity or ""
            logger.info(f"Participant: identity={identity}, name={p.name}, metadata={p.metadata}")
            attrs = p.attributes or {}
            sip_phone = attrs.get("sip.phoneNumber", "") or attrs.get("sip.callID", "")
            if sip_phone:
                return sip_phone
            phone_match = re.search(r"\+?1?(\d{10})", identity)
            if phone_match:
                return phone_match.group(1)
            if p.metadata:
                try:
                    meta = json.loads(p.metadata)
                    if "phone" in meta:
                        return meta["phone"]
                except (json.JSONDecodeError, TypeError):
                    pass
        return ""

    phone = find_sip_phone()

    if not phone:
        participant_connected = asyncio.Event()

        @ctx.room.on("participant_connected")
        def on_participant_connected(participant: rtc.RemoteParticipant):
            nonlocal phone
            phone = find_sip_phone()
            participant_connected.set()

        try:
            await asyncio.wait_for(participant_connected.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("No SIP participant joined within 30s. Using room metadata.")

    if not phone and ctx.job.metadata:
        try:
            meta = json.loads(ctx.job.metadata)
            phone = meta.get("phone", "") or meta.get("caller_id", "")
        except (json.JSONDecodeError, TypeError):
            pass

    if not phone:
        room_match = re.search(r"(\d{10,})", ctx.room.name)
        if room_match:
            phone = room_match.group(1)

    logger.info(f"Caller phone: {phone}")

    if phone:
        customer_info = await fetch_customer_info(phone)

    if not customer_info.get("full_name"):
        customer_info["full_name"] = "the customer"
        logger.warning(f"No customer info found for phone {phone}")

    vta_agent = VTAAgent(phone=phone, customer_info=customer_info)

    # Pin input transcription to whisper-1 + English.
    # IMPORTANT: gpt-4o-realtime-preview only accepts "whisper-1" as the
    # input_audio_transcription model — other model names cause the Realtime
    # session update to be rejected by OpenAI, which silently kills audio.
    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            voice="shimmer",
            model="gpt-4o-realtime-preview",
            temperature=0.8,
            modalities=["audio", "text"],
            input_audio_transcription=AudioTranscription(
                model="whisper-1",
                language="en",
                prompt="English only. Transcribe non-English words phonetically in English characters.",
            ),
        ),
    )

    await session.start(
        room=ctx.room,
        agent=vta_agent,
    )

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
