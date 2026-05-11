"""
VTA Emma — LiveKit Voice Agent (xAI Grok Realtime, non-thinking)

Architecture (TCN 3-way call):
  - TCN leg A: TCN <-> Customer (TCN owns this — never touched here)
  - TCN leg B: TCN <-> LiveKit SIP gateway (SIP participant in this room)
  - LiveKit room: { SIP participant, VTA agent }

End-of-call flow (deterministic, no callbacks):
  1. LLM speaks the closing line (per prompt's CLOSING PROTOCOL)
  2. LLM calls log_verification tool
  3. Tool: disallow_interruptions
  4. Tool: asyncio.sleep(1.5) — fixed delay for closing audio to stream
  5. Tool: remove SIP participant → BYE to TCN (ACTUAL call end)
  6. Tool: fire Railway HTTP logging in background (non-blocking)
  7. Tool: job_ctx.shutdown() → worker cleanup
  8. Tool: return "" (SIP is already gone — model can't speak to anyone)

  NOTE: Do NOT call session.aclose() from inside this tool — it
  deadlocks because the session is waiting for the tool to return.
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
TOOL_NUDGE_DELAY = float(os.getenv("TOOL_NUDGE_DELAY", "1.5"))

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    return digits[-10:] if len(digits) >= 10 else digits


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
    """Logs disposition to Railway, waits for closing audio, removes SIP, shuts down."""

    def __init__(
        self, *, phone: str, full_name: str, call_started_at: float,
        sip_identity: str = "", http: aiohttp.ClientSession,
    ) -> None:
        tool = function_tool(self._log_verification, raw_schema=_LOG_VERIFICATION_SCHEMA)
        super().__init__(id="log_verification", tools=[tool])
        self._phone = phone
        self._full_name = full_name
        self._call_started_at = call_started_at
        self._sip_identity = sip_identity
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

        # 2. Fixed delay — let the closing-line audio play out.
        #    ctx.wait_for_playout() HANGS with xAI Realtime (generation
        #    future never resolves). 1.5s lets most of the closing phrase
        #    stream before we cut the SIP leg.
        await asyncio.sleep(1.5)
        logger.info("[END_CALL] 1.5s playout delay done")

        # 3. REMOVE SIP PARTICIPANT IMMEDIATELY — this is the BYE to TCN.
        #    This is THE action that actually ends the call. Everything
        #    after this is just housekeeping.
        #    NOTE: Do NOT call session.aclose() before this — it deadlocks
        #    because the session is waiting for THIS tool to return.
        job_ctx = get_job_context()
        room_name = (job_ctx.room.name if job_ctx.room else "") or ""
        removed = False
        if room_name and self._sip_identity:
            try:
                await job_ctx.api.room.remove_participant(
                    api.RoomParticipantIdentity(
                        room=room_name, identity=self._sip_identity,
                    )
                )
                removed = True
                logger.info(f"[END_CALL] SIP {self._sip_identity} removed — BYE sent to TCN")
            except Exception as e:
                logger.warning(f"[END_CALL] remove_participant failed: {e}")

        if not removed and room_name:
            try:
                await job_ctx.delete_room()
                logger.info(f"[END_CALL] room {room_name} deleted (fallback)")
            except Exception as e:
                logger.error(f"[END_CALL] delete_room failed: {e}")

        # 4. Fire Railway logging in background — non-blocking.
        asyncio.create_task(_fire_railway_log(
            self._phone, status, summary, full_name,
            self._call_started_at, room_name, self._http,
        ))

        # 5. Shut down the job. This cleans up the session, Realtime WS,
        #    and worker resources. Even if the model generates follow-up
        #    speech, the SIP participant is already gone — no one hears it.
        try:
            job_ctx.shutdown(reason=f"agent_end_call:{status}")
        except Exception:
            pass

        # 6. Return empty string.
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
            min_endpointing_delay=MIN_ENDPOINTING_DELAY,
            max_endpointing_delay=MAX_ENDPOINTING_DELAY,
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
        # Tool nudge
        # ------------------------------------------------------------------
        _nudge_task: asyncio.Task | None = None

        @session.on("agent_state_changed")
        def on_agent_state(ev) -> None:
            nonlocal _nudge_task
            try:
                if log_tool._ending:
                    return
                ns = getattr(ev, "new_state", None)
                if ns == "listening":
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
                        if time.monotonic() - call_started_at < 10.0:
                            return
                        logger.info("[NUDGE] prompting model to call log_verification")
                        try:
                            await session.generate_reply(instructions=(
                                "If your very last spoken response was a closing "
                                "or farewell line (e.g. goodbye, transfer, have a "
                                "nice day), you MUST call log_verification now with "
                                "the appropriate status and summary. Do not speak "
                                "again — just call the function."
                            ))
                        except Exception:
                            pass

                    _nudge_task = asyncio.create_task(_nudge())
                elif ns in ("speaking", "thinking"):
                    if _nudge_task is not None and not _nudge_task.done():
                        _nudge_task.cancel()
                        _nudge_task = None
            except Exception:
                pass

        @session.on("user_state_changed")
        def on_user_nudge_cancel(ev) -> None:
            nonlocal _nudge_task
            if getattr(ev, "new_state", None) == "speaking":
                if _nudge_task is not None and not _nudge_task.done():
                    _nudge_task.cancel()
                    _nudge_task = None

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
        async def _force_end_call(sess: AgentSession, status: str, summary: str) -> None:
            if log_tool._ending:
                return
            log_tool._ending = True
            logger.info(f"[END_CALL] system: status={status}")

            try:
                sess.interrupt()
            except Exception:
                pass

            try:
                handle = sess.say(SYSTEM_CLOSING_OTHER, allow_interruptions=False)
                if handle is not None and hasattr(handle, "wait_for_playout"):
                    await handle.wait_for_playout()
            except Exception:
                await asyncio.sleep(3.0)

            # Fire logging in background
            room_name = (ctx.room.name if ctx.room else "") or ""
            asyncio.create_task(_fire_railway_log(
                phone, status, summary, full_name,
                call_started_at, room_name, http_session,
            ))

            # Remove SIP
            if room_name and sip_identity:
                try:
                    await ctx.api.room.remove_participant(
                        api.RoomParticipantIdentity(room=room_name, identity=sip_identity)
                    )
                except Exception:
                    try:
                        await ctx.delete_room()
                    except Exception:
                        pass
            elif room_name:
                try:
                    await ctx.delete_room()
                except Exception:
                    pass

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
