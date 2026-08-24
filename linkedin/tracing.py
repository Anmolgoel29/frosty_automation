# linkedin/tracing.py
"""Optional OpenTelemetry tracing of every LLM call, exported to Arize Phoenix.

Off by default — set ``PHOENIX_COLLECTOR_ENDPOINT`` to enable (e.g.
``http://localhost:6006``, Phoenix's default HTTP port; the ``/v1/traces``
collector path is appended automatically if missing). Once enabled, every
LLM call in the process is traced: ``Agent.instrument_all()`` sets pydantic-ai's
process-wide instrumentation default, which every ``Agent(...)`` call site in
``linkedin/`` picks up automatically (none of them pass ``instrument=``
explicitly) — both qualification-cascade stages, search-keyword generator,
fact extraction/reconciliation, and the follow-up messaging agent, across
both the "expensive" and "cheap" model roles.

**Task-context tagging.** By itself, pydantic-ai's instrumentation only
knows about the prompt/response of one model call — a Phoenix trace for,
say, ``qualify_with_llm`` has no way to say *which lead*, *which account*,
or *which queued Task* produced it. ``task_span()`` closes that gap: the
daemon opens exactly one of these per ``Task`` execution
(``daemon.py:_run_task``), tagged with ``task_id``/``task_type``/the
executing account/the campaign/the lead (when the task payload names one).
Every LLM call made anywhere during that task — however many function
calls deep, and even though pydantic-ai's own per-model-call span is
created on the dedicated ``llm-runner`` thread via
``linkedin/llm.py:run_agent_sync``, not the task's own thread — nests as a
descendant of it, because ``run_agent_sync`` bridges the active
OpenTelemetry context across that thread hop. The net effect: one Phoenix
trace per Task row, filterable/searchable by the tagged metadata, and
groupable into one Phoenix "session" per (lead, campaign) pair via
``session_id_for()`` so a lead's whole lifecycle — qualification, connect,
every follow-up — is one browsable session instead of scattered traces
with no run-to-run link. ``tag_current_span()`` backfills context onto an
already-open span for the one case a task's target lead isn't known until
partway through it: a ``connect`` task's payload only carries
``campaign_id`` — the lead it ends up qualifying/connecting is picked
inside ``run_qualification`` — so ``pipeline/qualify.py`` calls it once the
candidate is resolved.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os

logger = logging.getLogger(__name__)

# Flipped on by setup_tracing() once OTel export is actually configured.
# task_span()/tag_current_span() no-op while this is False, so tagging
# calls sprinkled through the pipeline cost nothing when tracing is off.
_ENABLED = False

# Accumulates the metadata dict for whichever task_span() is currently
# open, so tag_current_span() can merge new fields into it and re-set the
# span's single `metadata` JSON attribute from the *whole* dict — OTel spans
# don't support incrementally patching one JSON-string attribute, so a naive
# overwrite would clobber whatever task_span() set at open time.
_current_metadata: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_current_metadata", default=None,
)


def setup_tracing() -> None:
    """Wire pydantic-ai's built-in OpenTelemetry instrumentation to Phoenix.

    No-op when ``PHOENIX_COLLECTOR_ENDPOINT`` is unset, importing neither
    ``phoenix`` nor ``opentelemetry`` in that case — tracing costs nothing
    when it isn't configured. Call once at process start, before any
    ``Agent`` is constructed (``rundaemon`` and every CLI debug script via
    ``linkedin/browser/registry.py:cli_parser`` both do this right after
    ``configure_logging``).
    """
    global _ENABLED

    endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT")
    if not endpoint:
        logger.debug("PHOENIX_COLLECTOR_ENDPOINT not set — LLM tracing disabled")
        return

    from phoenix.otel import register
    from pydantic_ai.agent import Agent

    project_name = os.environ.get("PHOENIX_PROJECT_NAME", "openoutreach")

    # phoenix.otel only appends the OTLP HTTP collector path (/v1/traces) when
    # it resolves PHOENIX_COLLECTOR_ENDPOINT itself — i.e. when `endpoint=` is
    # left None. We pass it explicitly (so enabling tracing stays gated on
    # this function's own presence check), which skips that normalization: an
    # endpoint like "http://host:6006" would be posted to as-is, landing on
    # the UI route instead of the collector and failing with
    # "405 Method Not Allowed". Normalize it ourselves instead.
    endpoint = endpoint.rstrip("/")
    if not endpoint.endswith("/v1/traces"):
        endpoint = f"{endpoint}/v1/traces"

    # auto_instrument=False: we want exactly pydantic-ai's own instrumentation
    # (full request/response messages, tokens, cost per call), not every
    # OpenInference instrumentor that happens to be installed alongside it —
    # that would double-trace the same underlying openai/anthropic HTTP call.
    register(
        endpoint=endpoint, project_name=project_name, protocol="http/protobuf",
        batch=True, auto_instrument=False, verbose=False,
    )
    Agent.instrument_all(True)
    _ENABLED = True
    logger.info("LLM tracing enabled — exporting to %s (project=%s)", endpoint, project_name)


def session_id_for(*, campaign_id: int | None, public_id: str | None = None) -> str:
    """Deterministic Phoenix session id grouping every trace for one (lead, campaign) pair.

    Built from plain ids/identifiers, not a Deal pk — a Deal doesn't exist
    yet during qualification, and the same id must be derivable at every
    stage of the lead's lifecycle for the grouping to actually group
    anything. Returns "" (no session tag) when either half is missing, e.g.
    a connect task before a candidate has been picked.
    """
    if not campaign_id or not public_id:
        return ""
    return f"campaign-{campaign_id}:{public_id}"


@contextlib.contextmanager
def task_span(name: str, *, session_id: str = "", **metadata):
    """Open a span for one top-level LLM-bearing run (one daemon Task execution).

    No-op (yields None, opens nothing) when tracing isn't enabled. Keyword
    arguments become the span's `metadata` (searchable in the Phoenix UI,
    e.g. `task_id=`, `task_type=`, `linkedin_profile=`, `campaign=`,
    `lead_public_identifier=`) — falsy values (None/"") are dropped rather
    than tagged. See the module docstring for how nested LLM calls end up
    attached to this span despite running on a different thread.
    """
    if not _ENABLED:
        yield None
        return

    from openinference.instrumentation import get_metadata_attributes, get_session_attributes
    from opentelemetry import trace as otel_trace

    clean = {k: v for k, v in metadata.items() if v not in (None, "")}
    token = _current_metadata.set(clean)

    attributes: dict = {}
    if session_id:
        attributes.update(get_session_attributes(session_id=session_id))
    if clean:
        attributes.update(get_metadata_attributes(metadata=clean))

    tracer = otel_trace.get_tracer(__name__)
    try:
        with tracer.start_as_current_span(name, attributes=attributes) as span:
            yield span
    finally:
        _current_metadata.reset(token)


def tag_current_span(*, session_id: str = "", **metadata) -> None:
    """Backfill session id / metadata onto the currently-open `task_span`.

    For context that isn't known until partway through a task — a connect
    task's payload only carries `campaign_id`; the lead it ends up
    qualifying is picked inside `run_qualification`. No-op when tracing is
    disabled or nothing is currently open. Merges into (rather than
    replacing) whatever `task_span()` already tagged, since a span's
    `metadata` attribute is one JSON string, not independently-patchable
    fields.
    """
    if not _ENABLED:
        return

    from openinference.instrumentation import get_metadata_attributes, get_session_attributes
    from opentelemetry import trace as otel_trace

    span = otel_trace.get_current_span()
    if not span.is_recording():
        return

    current = _current_metadata.get()
    if current is None:
        current = {}
        _current_metadata.set(current)
    clean = {k: v for k, v in metadata.items() if v not in (None, "")}
    current.update(clean)

    attributes: dict = {}
    if session_id:
        attributes.update(get_session_attributes(session_id=session_id))
    if current:
        attributes.update(get_metadata_attributes(metadata=current))
    span.set_attributes(attributes)
