"""
transcripts.py — ship call transcripts + analytics to Loki at end of call.

WHY THIS EXISTS
---------------
LiveKit Cloud's own session analytics (transcript, per-turn timing, disposition)
is authoritative but is published with a delay. For operational use we want the
record the moment a call ends — whether the agent hangs up (log_verification /
force-end) or the customer drops. This module accumulates the conversation
during the call and, in a shutdown callback, pushes two things to Loki:

  * kind="transcript"    — one log line per turn, stamped at the turn's own time,
                           so Grafana renders the conversation as a timeline.
  * kind="call_summary"  — a single structured line with the whole call: the
                           disposition, durations, per-turn latency summary, and
                           the full transcript inline (self-contained record).

It is fully decoupled from Prometheus (observability.py): metrics are numbers,
this is logs/text. Disabled cleanly when LOKI_PUSH_URL is unset.

LABELS (kept low-cardinality for Loki): job, agent, kind, status.
Everything high-cardinality (call_id, phone, text) lives in the JSON log line —
query it in Grafana with LogQL `| json`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

import aiohttp

logger = logging.getLogger("vta-agent.transcripts")

_TRUTHY = {"1", "true", "yes", "on"}

LOKI_PUSH_URL = os.getenv("LOKI_PUSH_URL", "").strip()
# Enabled by default whenever a push URL is configured.
LOKI_ENABLED = (
    os.getenv("LOKI_ENABLED", "true").strip().lower() in _TRUTHY and bool(LOKI_PUSH_URL)
)
LOKI_USERNAME = os.getenv("LOKI_USERNAME", "").strip()
LOKI_PASSWORD = os.getenv("LOKI_PASSWORD", "").strip()
LOKI_TENANT = os.getenv("LOKI_TENANT", "").strip()  # X-Scope-OrgID (multi-tenant)
LOKI_TIMEOUT = float(os.getenv("LOKI_TIMEOUT", "5"))

AGENT_LABEL = os.getenv("AGENT_NAME", "vta-emma")
LOKI_JOB = os.getenv("LOKI_JOB", AGENT_LABEL)


def _auth_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if LOKI_USERNAME:
        import base64

        token = base64.b64encode(f"{LOKI_USERNAME}:{LOKI_PASSWORD}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    elif LOKI_PASSWORD:  # bearer-style token only
        headers["Authorization"] = f"Bearer {LOKI_PASSWORD}"
    if LOKI_TENANT:
        headers["X-Scope-OrgID"] = LOKI_TENANT
    return headers


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _item_text(item: Any) -> str:
    """Best-effort extraction of an assistant/user turn's text."""
    txt = getattr(item, "text_content", None)
    if isinstance(txt, str):
        return txt
    content = getattr(item, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = [c for c in content if isinstance(c, str)]
        if parts:
            return " ".join(parts)
    return ""


def _item_metric(item: Any, key: str) -> float | None:
    metrics = getattr(item, "metrics", None)
    if not metrics:
        return None
    get = metrics.get if hasattr(metrics, "get") else (lambda k, d=None: getattr(metrics, k, d))
    val = get(key)
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


class TranscriptCollector:
    """Accumulates conversation turns for one call and flushes to Loki on end."""

    def __init__(
        self,
        session: Any,
        *,
        call_id: str,
        get_meta: Callable[[], dict] | None = None,
        models: dict | None = None,
    ):
        self.session = session
        self.call_id = call_id or ""
        self._get_meta = get_meta or (lambda: {})
        self.models = models or {}
        self.started_at = time.time()
        self.started_mono = time.monotonic()
        # Fallback turn buffer (used if session.history is unavailable at flush).
        self._turns: list[dict] = []
        self._flushed = False

    # -- live capture --------------------------------------------------------
    def on_conversation_item(self, ev: Any) -> None:
        try:
            item = getattr(ev, "item", None)
            if item is None:
                return
            role = getattr(item, "role", None)
            if role not in ("user", "assistant"):
                return
            text = _item_text(item)
            if not text:
                return
            created = getattr(item, "created_at", None)
            ts = float(created) if created else time.time()
            turn = {"ts": ts, "role": role, "text": text}
            e2e = _item_metric(item, "e2e_latency")
            if e2e is not None:
                turn["e2e_latency"] = round(e2e, 3)
            self._turns.append(turn)
        except Exception as e:  # never break the call on a telemetry bug
            logger.debug("on_conversation_item error: %s", e)

    # -- assemble the authoritative transcript at end of call ----------------
    def _collect_turns(self) -> list[dict]:
        """Prefer the session's final history; fall back to the live buffer."""
        turns: list[dict] = []
        try:
            history = getattr(self.session, "history", None)
            items = getattr(history, "items", None) if history is not None else None
            if items:
                for item in items:
                    role = getattr(item, "role", None)
                    if role not in ("user", "assistant"):
                        continue
                    text = _item_text(item)
                    if not text:
                        continue
                    created = getattr(item, "created_at", None)
                    ts = float(created) if created else time.time()
                    turn = {"ts": ts, "role": role, "text": text}
                    e2e = _item_metric(item, "e2e_latency")
                    if e2e is not None:
                        turn["e2e_latency"] = round(e2e, 3)
                    turns.append(turn)
        except Exception as e:
            logger.debug("history read failed: %s", e)
        if not turns:
            turns = list(self._turns)
        turns.sort(key=lambda t: t["ts"])
        return turns

    def _status(self) -> str:
        obs = getattr(self.session, "_vta_obs", None)
        return getattr(obs, "status", "unknown") if obs is not None else "unknown"

    def build_payload(self) -> dict:
        meta = {}
        try:
            meta = self._get_meta() or {}
        except Exception:
            meta = {}
        turns = self._collect_turns()
        ended_at = time.time()
        status = self._status()

        latencies = [t["e2e_latency"] for t in turns if "e2e_latency" in t]
        user_turns = sum(1 for t in turns if t["role"] == "user")
        agent_turns = sum(1 for t in turns if t["role"] == "assistant")

        summary = {
            "call_id": self.call_id,
            "phone": meta.get("phone", ""),
            "full_name": meta.get("full_name", ""),
            "status": status,
            "started_at": _iso(self.started_at),
            "ended_at": _iso(ended_at),
            "duration_s": round(ended_at - self.started_at, 2),
            "user_turns": user_turns,
            "agent_turns": agent_turns,
            "total_turns": len(turns),
            "models": self.models,
            "transcript": turns,
        }
        if latencies:
            ordered = sorted(latencies)
            summary["e2e_latency_p50"] = ordered[len(ordered) // 2]
            summary["e2e_latency_avg"] = round(sum(latencies) / len(latencies), 3)
            summary["e2e_latency_max"] = max(latencies)

        base_labels = {"job": LOKI_JOB, "agent": AGENT_LABEL, "status": status}

        # Per-turn transcript lines, each stamped at the turn's own time.
        transcript_values = []
        for t in turns:
            line = {
                "call_id": self.call_id,
                "phone": meta.get("phone", ""),
                "role": t["role"],
                "text": t["text"],
            }
            if "e2e_latency" in t:
                line["e2e_latency"] = t["e2e_latency"]
            transcript_values.append([str(int(t["ts"] * 1e9)), json.dumps(line, ensure_ascii=False)])

        summary_line = json.dumps(summary, ensure_ascii=False)
        streams = [
            {"stream": {**base_labels, "kind": "call_summary"},
             "values": [[str(int(ended_at * 1e9)), summary_line]]},
        ]
        if transcript_values:
            streams.insert(
                0,
                {"stream": {**base_labels, "kind": "transcript"}, "values": transcript_values},
            )
        return {"streams": streams}

    # -- flush ---------------------------------------------------------------
    async def flush(self) -> None:
        if self._flushed:
            return
        self._flushed = True
        if not LOKI_ENABLED:
            return
        try:
            payload = self.build_payload()
        except Exception as e:
            logger.error("transcript build failed: %s", e)
            return
        try:
            timeout = aiohttp.ClientTimeout(total=LOKI_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.post(
                    LOKI_PUSH_URL, data=json.dumps(payload), headers=_auth_headers()
                ) as resp:
                    body = "" if resp.status < 300 else (await resp.text())[:200]
                    logger.info(
                        "[LOKI] pushed transcript for call=%s status=%s -> HTTP %s %s",
                        self.call_id, self._status(), resp.status, body,
                    )
        except Exception as e:  # never block shutdown on telemetry
            logger.warning("[LOKI] transcript push failed for call=%s: %s", self.call_id, e)


def instrument_transcripts(
    session: Any,
    ctx: Any,
    *,
    call_id: str,
    get_meta: Callable[[], dict] | None = None,
    models: dict | None = None,
) -> TranscriptCollector:
    """Attach transcript capture to one AgentSession.

    Call right after `await session.start(...)`. Registers a conversation
    listener and a shutdown callback that pushes the transcript + call summary
    to Loki when the call ends (agent hangup OR customer drop).
    """
    collector = TranscriptCollector(session, call_id=call_id, get_meta=get_meta, models=models)
    if not LOKI_ENABLED:
        logger.info("[LOKI] disabled (LOKI_PUSH_URL unset) — transcript capture is a no-op")
        return collector

    try:
        session.on("conversation_item_added", collector.on_conversation_item)
    except Exception as e:
        logger.debug("could not subscribe conversation_item_added: %s", e)

    async def _flush_on_shutdown() -> None:
        await collector.flush()

    try:
        ctx.add_shutdown_callback(_flush_on_shutdown)
    except Exception as e:
        logger.debug("could not register transcript shutdown flush: %s", e)

    logger.info("[LOKI] transcript capture armed -> %s", LOKI_PUSH_URL)
    return collector
