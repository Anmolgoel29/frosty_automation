# linkedin/tracing.py
"""Optional OpenTelemetry tracing of every LLM call, exported to Arize Phoenix.

Off by default — set ``PHOENIX_COLLECTOR_ENDPOINT`` to enable (e.g.
``http://localhost:6006``, Phoenix's default HTTP port; the ``/v1/traces``
collector path is appended automatically if missing). Once enabled, every
LLM call in the process is traced: ``Agent.instrument_all()`` sets pydantic-ai's
process-wide instrumentation default, which every ``Agent(...)`` call site in
``linkedin/`` picks up automatically (none of them pass ``instrument=``
explicitly) — the qualifier, search-keyword generator, fact
extraction/reconciliation, and the follow-up messaging agent, across both the
"chat" and "task" model roles.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def setup_tracing() -> None:
    """Wire pydantic-ai's built-in OpenTelemetry instrumentation to Phoenix.

    No-op when ``PHOENIX_COLLECTOR_ENDPOINT`` is unset, importing neither
    ``phoenix`` nor ``opentelemetry`` in that case — tracing costs nothing
    when it isn't configured. Call once at process start, before any
    ``Agent`` is constructed (``rundaemon`` and every CLI debug script via
    ``linkedin/browser/registry.py:cli_parser`` both do this right after
    ``configure_logging``).
    """
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
    logger.info("LLM tracing enabled — exporting to %s (project=%s)", endpoint, project_name)
