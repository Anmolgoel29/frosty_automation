# linkedin/tracing.py
"""Optional OpenTelemetry tracing of every LLM call, exported to Arize Phoenix.

Off by default — set ``PHOENIX_COLLECTOR_ENDPOINT`` to enable (e.g.
``http://localhost:6006/v1/traces`` for a local Phoenix instance over HTTP, or
``http://phoenix:4317`` for its gRPC port). Once enabled, every LLM call in
the process is traced: ``Agent.instrument_all()`` sets pydantic-ai's
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
    # auto_instrument=False: we want exactly pydantic-ai's own instrumentation
    # (full request/response messages, tokens, cost per call), not every
    # OpenInference instrumentor that happens to be installed alongside it —
    # that would double-trace the same underlying openai/anthropic HTTP call.
    register(
        endpoint=endpoint, project_name=project_name,
        batch=True, auto_instrument=False, verbose=False,
    )
    Agent.instrument_all(True)
    logger.info("LLM tracing enabled — exporting to %s (project=%s)", endpoint, project_name)
