"""
observability.py — Prometheus metrics for the VTA Emma LiveKit voice agent.

WHY THIS EXISTS
---------------
`vta-emma` runs as a LiveKit Cloud **managed** agent. Managed agents:
  - do not expose any inbound/public port (only an internal :8081 health
    check that LiveKit Cloud uses during rolling deploys), and
  - autoscale to **zero** when idle.

That breaks the classic "Prometheus scrapes agent-worker:8080/metrics" model
the blog posts assume. So this module supports **two exposition modes** behind
one env switch, while keeping a single `prometheus_client` registry:

  METRICS_MODE = "scrape"        -> start_http_server(METRICS_PORT) exposes
                                    /metrics for a local/self-hosted Prometheus
                                    (used by the docker-compose stack & dev).
  METRICS_MODE = "remote_write"  -> a background thread pushes the registry to
                                    a Prometheus remote-write endpoint
                                    (Grafana Cloud) every METRICS_PUSH_INTERVAL
                                    seconds + a flush on shutdown. Correct for
                                    the scale-to-zero managed deployment.
  METRICS_MODE = "both"          -> do both.
  METRICS_MODE = "off"           -> no-op (metrics still recorded, never sent).

The instrumentation API (counters/histograms/gauges) is identical in every
mode, so dashboards and alerts are portable between local and Grafana Cloud.

WHAT WE MEASURE
---------------
The agent uses a **realtime** model (xAI Grok Realtime), so there is no
separate STT/TTS to time. The meaningful signals are:
  * RealtimeModelMetrics  -> TTFT, response duration, tokens, tokens/sec
  * ChatMessage.metrics   -> per-turn end-to-end latency (the headline UX metric)
  * EOUMetrics            -> end-of-utterance (turn-detection) delay
  * Business dispositions -> verified / dnc / wrong_number / ... (call outcomes)
  * Reliability           -> errors, tool-call outcomes, forced call-ends

NOTE ON THE DEPRECATED EVENT
----------------------------
We subscribe to the session-level ``metrics_collected`` event. As of
livekit-agents 1.5.x this event is *deprecated* (but fully functional) — it is
the single aggregation point that re-emits RealtimeModelMetrics / EOUMetrics
for a realtime agent. The non-deprecated per-turn surface
(``conversation_item_added`` -> ``ChatMessage.metrics``) is used in parallel
for e2e latency. If the deprecated event is ever removed, only the
``_on_session_metrics`` handler needs to migrate; everything else is unaffected.
"""

from __future__ import annotations

import atexit
import logging
import os
import socket
import threading
import time
import uuid
from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)

logger = logging.getLogger("vta-agent.observability")

# ---------------------------------------------------------------------------
# Configuration (all via env; sane defaults)
# ---------------------------------------------------------------------------
_TRUTHY = {"1", "true", "yes", "on"}

METRICS_ENABLED = os.getenv("METRICS_ENABLED", "true").strip().lower() in _TRUTHY
# scrape | remote_write | both | off
METRICS_MODE = os.getenv("METRICS_MODE", "scrape").strip().lower()
METRICS_PORT = int(os.getenv("METRICS_PORT", "9091"))
METRICS_ADDR = os.getenv("METRICS_ADDR", "0.0.0.0")

# Grafana Cloud (or any Prometheus remote-write receiver)
METRICS_PUSH_URL = os.getenv("METRICS_PUSH_URL", "").strip()
METRICS_PUSH_USERNAME = os.getenv("METRICS_PUSH_USERNAME", "").strip()
METRICS_PUSH_PASSWORD = os.getenv("METRICS_PUSH_PASSWORD", "").strip()
METRICS_PUSH_INTERVAL = float(os.getenv("METRICS_PUSH_INTERVAL", "15"))

# Identity labels attached to every series (so multiple autoscaled replicas
# don't collide). PromQL's counter-reset handling makes per-replica counters
# safe to sum with `sum without(instance)(...)`.
METRICS_JOB = os.getenv("METRICS_JOB", os.getenv("AGENT_NAME", "vta-emma"))
METRICS_INSTANCE = os.getenv(
    "METRICS_INSTANCE",
    f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}",
)

AGENT_LABEL = os.getenv("AGENT_NAME", "vta-emma")

# Voice-appropriate latency buckets (seconds). Sub-second resolution where it
# matters for conversational feel, with a long tail to catch stalls.
_LATENCY_BUCKETS = (
    0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0,
)
_DURATION_BUCKETS = (0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 34.0)
_CALL_BUCKETS = (5, 15, 30, 45, 60, 90, 120, 180, 240, 300, 450, 600)

# Dedicated registry — keeps our series isolated and easy to serialize for
# remote-write. Default process/GC collectors are registered onto it too.
REGISTRY = CollectorRegistry(auto_describe=True)

try:  # process_* and python_gc_* worker-health metrics (CPU, RSS, fds, GC)
    from prometheus_client import (
        GC_COLLECTOR,
        PLATFORM_COLLECTOR,
        PROCESS_COLLECTOR,
    )

    REGISTRY.register(PROCESS_COLLECTOR)
    REGISTRY.register(PLATFORM_COLLECTOR)
    REGISTRY.register(GC_COLLECTOR)
except Exception:  # pragma: no cover - best effort
    pass

# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------
# Session lifecycle ---------------------------------------------------------
SESSIONS_STARTED = Counter(
    "livekit_sessions_started_total",
    "Voice sessions (calls) started by the agent.",
    ["agent"],
    registry=REGISTRY,
)
SESSIONS_ENDED = Counter(
    "livekit_sessions_ended_total",
    "Voice sessions ended, labelled by terminal disposition/status.",
    ["agent", "status"],
    registry=REGISTRY,
)
ACTIVE_SESSIONS = Gauge(
    "livekit_active_sessions",
    "Currently active voice sessions on this worker.",
    ["agent"],
    registry=REGISTRY,
    multiprocess_mode="livesum",
)
SESSION_DURATION = Histogram(
    "livekit_session_duration_seconds",
    "Total wall-clock duration of a call, labelled by disposition.",
    ["agent", "status"],
    buckets=_CALL_BUCKETS,
    registry=REGISTRY,
)

# Latency (voice UX) --------------------------------------------------------
E2E_LATENCY = Histogram(
    "livekit_e2e_latency_seconds",
    "End-to-end response latency: user stopped speaking -> agent starts "
    "responding (from ChatMessage.metrics). The headline conversational metric.",
    ["agent"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
LLM_TTFT = Histogram(
    "livekit_llm_ttft_seconds",
    "Realtime model time-to-first-audio-token.",
    ["agent", "model"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
LLM_RESPONSE_DURATION = Histogram(
    "livekit_llm_response_duration_seconds",
    "Realtime model full-response generation time.",
    ["agent", "model"],
    buckets=_DURATION_BUCKETS,
    registry=REGISTRY,
)
EOU_DELAY = Histogram(
    "livekit_eou_delay_seconds",
    "End-of-utterance (turn-detection) delay.",
    ["agent"],
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)

# Tokens / cost -------------------------------------------------------------
LLM_INPUT_TOKENS = Counter(
    "livekit_llm_input_tokens_total",
    "Input tokens consumed by the realtime model.",
    ["agent", "model"],
    registry=REGISTRY,
)
LLM_OUTPUT_TOKENS = Counter(
    "livekit_llm_output_tokens_total",
    "Output tokens produced by the realtime model.",
    ["agent", "model"],
    registry=REGISTRY,
)
LLM_RESPONSES = Counter(
    "livekit_llm_responses_total",
    "Number of realtime model responses (one per assistant turn).",
    ["agent", "model"],
    registry=REGISTRY,
)

# Reliability ---------------------------------------------------------------
ERRORS = Counter(
    "livekit_errors_total",
    "Errors encountered by the agent, labelled by type.",
    ["agent", "type"],
    registry=REGISTRY,
)
TOOL_CALLS = Counter(
    "livekit_tool_calls_total",
    "Function/tool calls, labelled by tool name and outcome.",
    ["agent", "tool", "outcome"],
    registry=REGISTRY,
)
FORCED_ENDS = Counter(
    "livekit_forced_ends_total",
    "Calls force-ended by the agent (silence, watchdog, terminal-speech), "
    "labelled by reason.",
    ["agent", "reason"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Exposition (scrape server and/or remote-write pusher) — start once
# ---------------------------------------------------------------------------
_init_lock = threading.Lock()
_initialized = False
_pusher: "_RemoteWritePusher | None" = None


class _RemoteWritePusher:
    """Periodically serialize REGISTRY and push it to a Prometheus
    remote-write endpoint (e.g. Grafana Cloud). Pure-Python; no snappy/C deps.
    """

    def __init__(self, url: str, username: str, password: str, interval: float):
        from prometheus_remote_writer import RemoteWriter  # lazy import

        headers: dict[str, str] = {}
        # Grafana Cloud uses HTTP Basic auth (instance id : API token).
        if username:
            import base64

            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        elif password:  # bearer-style token only
            headers["Authorization"] = f"Bearer {password}"

        self._writer = RemoteWriter(url=url, headers=headers)
        self._interval = max(5.0, interval)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="metrics-remote-write", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        atexit.register(self.flush)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.flush()

    def flush(self) -> None:
        try:
            samples = _collect_samples()
            if samples:
                self._writer.send(samples)
        except Exception as e:  # never let telemetry crash the agent
            logger.warning("remote-write push failed: %s", e)


def _collect_samples() -> list[dict[str, Any]]:
    """Snapshot the registry into the remote-write payload shape:
    [{ 'metric': {'__name__': ..., <labels>}, 'values': [v], 'timestamps': [ms] }]
    """
    now_ms = int(time.time() * 1000)
    out: list[dict[str, Any]] = []
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            labels = dict(sample.labels)
            labels["__name__"] = sample.name
            labels.setdefault("job", METRICS_JOB)
            labels.setdefault("instance", METRICS_INSTANCE)
            out.append(
                {"metric": labels, "values": [sample.value], "timestamps": [now_ms]}
            )
    return out


def init_exposition() -> None:
    """Idempotently start metrics exposition based on METRICS_MODE.

    Safe to call from process start and/or per-session — only the first call
    has any effect.
    """
    global _initialized, _pusher
    if not METRICS_ENABLED or METRICS_MODE == "off":
        return
    with _init_lock:
        if _initialized:
            return
        _initialized = True

        if METRICS_MODE in ("scrape", "both"):
            try:
                start_http_server(METRICS_PORT, addr=METRICS_ADDR, registry=REGISTRY)
                logger.info(
                    "[METRICS] scrape endpoint on http://%s:%d/metrics",
                    METRICS_ADDR,
                    METRICS_PORT,
                )
            except Exception as e:
                logger.error("[METRICS] failed to start scrape server: %s", e)

        if METRICS_MODE in ("remote_write", "both"):
            if not METRICS_PUSH_URL:
                logger.error(
                    "[METRICS] METRICS_MODE=%s but METRICS_PUSH_URL is unset; "
                    "remote-write disabled",
                    METRICS_MODE,
                )
            else:
                try:
                    _pusher = _RemoteWritePusher(
                        METRICS_PUSH_URL,
                        METRICS_PUSH_USERNAME,
                        METRICS_PUSH_PASSWORD,
                        METRICS_PUSH_INTERVAL,
                    )
                    _pusher.start()
                    logger.info(
                        "[METRICS] remote-write -> %s every %.0fs (instance=%s)",
                        METRICS_PUSH_URL,
                        max(5.0, METRICS_PUSH_INTERVAL),
                        METRICS_INSTANCE,
                    )
                except Exception as e:
                    logger.error(
                        "[METRICS] failed to start remote-write pusher "
                        "(is 'prometheus-remote-writer' installed?): %s",
                        e,
                    )


def flush() -> None:
    """Best-effort immediate push (call on shutdown to avoid losing the tail)."""
    if _pusher is not None:
        _pusher.flush()


# ---------------------------------------------------------------------------
# Lightweight recording helpers (used directly by agent.py code paths)
# ---------------------------------------------------------------------------
def record_error(error_type: str) -> None:
    if METRICS_ENABLED:
        ERRORS.labels(AGENT_LABEL, error_type[:64]).inc()


def record_tool_call(tool: str, outcome: str) -> None:
    if METRICS_ENABLED:
        TOOL_CALLS.labels(AGENT_LABEL, tool, outcome).inc()


def record_forced_end(reason: str) -> None:
    if METRICS_ENABLED:
        FORCED_ENDS.labels(AGENT_LABEL, reason).inc()


def note_status(session: Any, status: str) -> None:
    """Stamp the terminal disposition on the session's observer so the
    shutdown finalizer can label session counters/duration correctly."""
    obs = getattr(session, "_vta_obs", None)
    if obs is not None:
        obs.status = status or obs.status


# ---------------------------------------------------------------------------
# Per-session instrumentation
# ---------------------------------------------------------------------------
class _SessionObserver:
    """Holds per-session state and the event handlers bound to one call.

    A worker process handles many concurrent jobs, so all per-session state
    lives here (keyed by the session object), never in module globals.
    """

    def __init__(self, session: Any, model: str):
        self.session = session
        self.model = model or "unknown"
        self.started_at = time.monotonic()
        self.status = "unknown"  # overwritten by note_status() on the end path
        self.finalized = False

    # -- metrics_collected (deprecated session event; see module docstring) --
    def on_session_metrics(self, ev: Any) -> None:
        try:
            m = getattr(ev, "metrics", ev)
            name = type(m).__name__

            if name == "RealtimeModelMetrics" or (
                hasattr(m, "ttft") and hasattr(m, "input_tokens")
            ):
                ttft = getattr(m, "ttft", None)
                if ttft is not None and ttft >= 0:  # realtime ttft can be -1
                    LLM_TTFT.labels(AGENT_LABEL, self.model).observe(ttft)
                dur = getattr(m, "duration", None)
                if dur is not None and dur >= 0:
                    LLM_RESPONSE_DURATION.labels(AGENT_LABEL, self.model).observe(dur)
                in_tok = int(getattr(m, "input_tokens", 0) or 0)
                out_tok = int(getattr(m, "output_tokens", 0) or 0)
                if in_tok:
                    LLM_INPUT_TOKENS.labels(AGENT_LABEL, self.model).inc(in_tok)
                if out_tok:
                    LLM_OUTPUT_TOKENS.labels(AGENT_LABEL, self.model).inc(out_tok)
                LLM_RESPONSES.labels(AGENT_LABEL, self.model).inc()

            elif name == "EOUMetrics" or hasattr(m, "end_of_utterance_delay"):
                eou = getattr(m, "end_of_utterance_delay", None)
                if eou is not None and eou >= 0:
                    EOU_DELAY.labels(AGENT_LABEL).observe(eou)
        except Exception as e:  # never break the call on a telemetry bug
            logger.debug("on_session_metrics error: %s", e)

    # -- conversation_item_added (non-deprecated; per-turn latency) ----------
    def on_conversation_item(self, ev: Any) -> None:
        try:
            item = getattr(ev, "item", None)
            if item is None:
                return
            metrics = getattr(item, "metrics", None)
            if not metrics:
                return
            get = metrics.get if hasattr(metrics, "get") else (
                lambda k, d=None: getattr(metrics, k, d)
            )
            if getattr(item, "role", None) == "assistant":
                e2e = get("e2e_latency")
                if e2e is not None and e2e >= 0:
                    E2E_LATENCY.labels(AGENT_LABEL).observe(e2e)
            elif getattr(item, "role", None) == "user":
                eou = get("end_of_turn_delay")
                if eou is not None and eou >= 0:
                    EOU_DELAY.labels(AGENT_LABEL).observe(eou)
        except Exception as e:
            logger.debug("on_conversation_item error: %s", e)

    def finalize(self) -> None:
        if self.finalized:
            return
        self.finalized = True
        duration = max(0.0, time.monotonic() - self.started_at)
        try:
            ACTIVE_SESSIONS.labels(AGENT_LABEL).dec()
            SESSIONS_ENDED.labels(AGENT_LABEL, self.status).inc()
            SESSION_DURATION.labels(AGENT_LABEL, self.status).observe(duration)
        except Exception as e:
            logger.debug("finalize error: %s", e)
        flush()  # best-effort tail push for the scale-to-zero case


def instrument_session(session: Any, ctx: Any, model: str = "") -> _SessionObserver:
    """Attach Prometheus instrumentation to one AgentSession.

    Call this right after `await session.start(...)` in the entrypoint. It:
      * ensures exposition is running,
      * registers metrics event handlers on the session,
      * increments started/active counters, and
      * registers a shutdown callback to finalize duration + disposition.

    Returns the observer (also stored as `session._vta_obs`).
    """
    init_exposition()
    obs = _SessionObserver(session, model)
    try:
        session._vta_obs = obs  # type: ignore[attr-defined]
    except Exception:
        pass

    if METRICS_ENABLED:
        try:
            SESSIONS_STARTED.labels(AGENT_LABEL).inc()
            ACTIVE_SESSIONS.labels(AGENT_LABEL).inc()
        except Exception:
            pass

        # Multiple listeners per event are supported; we don't disturb the
        # agent's existing conversation_item_added / user_state handlers.
        try:
            session.on("metrics_collected", obs.on_session_metrics)
        except Exception as e:
            logger.debug("could not subscribe metrics_collected: %s", e)
        try:
            session.on("conversation_item_added", obs.on_conversation_item)
        except Exception as e:
            logger.debug("could not subscribe conversation_item_added: %s", e)

        try:
            ctx.add_shutdown_callback(_make_finalizer(obs))
        except Exception as e:
            logger.debug("could not register shutdown finalizer: %s", e)

    return obs


def _make_finalizer(obs: _SessionObserver):
    async def _finalize() -> None:
        obs.finalize()

    return _finalize
