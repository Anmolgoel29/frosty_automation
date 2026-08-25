# tests/test_tracing.py
"""Tests for linkedin/tracing.py's per-decision agent_run span helper."""
from __future__ import annotations

import json

import pytest

from linkedin import tracing


class TestAgentRunDisabled:
    """Tracing is off by default (no PHOENIX_COLLECTOR_ENDPOINT) — both
    halves of the pair must be safe no-ops so callers never have to branch
    on whether tracing happens to be configured."""

    def test_agent_run_yields_none(self):
        with tracing.agent_run("qualify_cheap") as span:
            assert span is None

    def test_finish_agent_run_on_none_span_does_not_raise(self):
        tracing.finish_agent_run(None, output="disqualify=True — wrong seniority", model="x:y")


class TestAgentRunEnabled:
    """With tracing on, agent_run/finish_agent_run must actually produce a
    correctly-attributed span: AGENT kind, the decision as output, and the
    full metadata (model/lead/campaign/decision fields) as one JSON blob —
    exactly what pipeline/qualify.py relies on to make a lead's qualify
    decision independently browsable in Phoenix."""

    @pytest.fixture
    def exporter(self, monkeypatch):
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        provider = TracerProvider()
        mem_exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(mem_exporter))

        # otel_trace.set_tracer_provider() is a real "exactly once, process-wide"
        # guard (opentelemetry.util._once.Once), not just a plain attribute —
        # resetting _TRACER_PROVIDER alone still leaves it refusing to set a
        # second provider, so the gate itself has to be reset per test.
        monkeypatch.setattr(otel_trace, "_TRACER_PROVIDER", None, raising=False)
        otel_trace._TRACER_PROVIDER_SET_ONCE._done = False
        otel_trace.set_tracer_provider(provider)
        monkeypatch.setattr(tracing, "_ENABLED", True)

        yield mem_exporter

        mem_exporter.clear()

    def test_span_kind_and_output(self, exporter):
        with tracing.agent_run("qualify_cheap", session_id="campaign-1:alice") as span:
            tracing.finish_agent_run(
                span,
                output="disqualify=True — wrong seniority",
                model="anthropic:claude-haiku",
                lead_public_identifier="alice",
                campaign="Outreach",
                disqualify=True,
                reason="wrong seniority",
            )

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        finished = spans[0]

        assert finished.name == "qualify_cheap"
        assert finished.attributes["openinference.span.kind"] == "AGENT"
        assert finished.attributes["output.value"] == "disqualify=True — wrong seniority"
        assert finished.attributes["session.id"] == "campaign-1:alice"

        metadata = json.loads(finished.attributes["metadata"])
        assert metadata == {
            "model": "anthropic:claude-haiku",
            "lead_public_identifier": "alice",
            "campaign": "Outreach",
            "disqualify": True,
            "reason": "wrong seniority",
        }

    def test_falsy_metadata_fields_are_dropped(self, exporter):
        with tracing.agent_run("qualify_with_llm") as span:
            tracing.finish_agent_run(
                span, output="qualified=True", model="x:y", campaign="",
            )

        metadata = json.loads(exporter.get_finished_spans()[0].attributes["metadata"])
        assert "campaign" not in metadata
        assert metadata["model"] == "x:y"
