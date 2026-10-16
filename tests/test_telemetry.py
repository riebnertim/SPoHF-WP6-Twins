"""Tests for wp6_data.shared.telemetry and the sync trace tree.

These assert the two behaviours this repo owns: (1) tracing stays a no-op unless
explicitly enabled, and (2) a sync run produces a connected ``sync.run`` →
``sync.endpoint`` span tree (the parent under which httpx/psycopg spans nest in
production). Library auto-instrumentation itself is OpenTelemetry's to test.
"""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from wp6_data.shared import telemetry
from wp6_data.shared.observability import _add_trace_context
from wp6_data.sync.feeds import YOOKR
from wp6_data.sync.orchestrator import SyncOrchestrator

# One global provider for the process (set_tracer_provider honours only the first
# call). No other test module configures tracing, so this test owns it.
_EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_provider)


@pytest.fixture()
def spans():
    """Fresh span buffer per test; returns the finished spans after the test body."""
    _EXPORTER.clear()
    yield _EXPORTER


# --- enable/disable gating ---


def test_setup_is_noop_without_endpoint(monkeypatch):
    """No OTLP endpoint → tracing disabled, and the FastAPI hook stays inert."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    telemetry.setup_telemetry(default_service_name="wp6-test")
    assert telemetry.tracing_enabled() is False
    # Must not raise or attempt to instrument when disabled.
    assert telemetry.instrument_fastapi(MagicMock()) is None


# --- FastAPI noise reduction ---


def test_fastapi_excludes_health_and_asgi_noise(spans, monkeypatch):
    """Probe URLs produce no trace; kept routes have no http send/receive child spans."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(telemetry, "_initialised", True)

    app = FastAPI()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/ping")
    def ping():
        return {"pong": True}

    telemetry.instrument_fastapi(app)
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/ping").status_code == 200

    names = [s.name for s in spans.get_finished_spans()]
    assert not any("health" in n for n in names)  # excluded entirely
    assert len([n for n in names if "ping" in n.lower()]) == 1  # one span, no children
    assert not any("send" in n or "receive" in n for n in names)


# --- sync span tree ---


@pytest.mark.asyncio()
async def test_sync_emits_connected_span_tree(spans, mock_settings):
    """run() wraps the cycle in sync.run, with one sync.endpoint child per endpoint."""
    orch = SyncOrchestrator(mock_settings)

    with (
        patch("wp6_data.sync.orchestrator.init_pool", new_callable=AsyncMock),
        patch(
            "wp6_data.sync.orchestrator.feed_for",
            return_value=replace(YOOKR, ensure_schema=AsyncMock()),
        ),
        patch("wp6_data.sync.orchestrator.refresh_sensor_summary_recent", new_callable=AsyncMock),
        patch("wp6_data.sync.orchestrator.close_pool", new_callable=AsyncMock),
        patch.object(orch, "_sync_endpoint", new_callable=AsyncMock, return_value=5),
    ):
        await orch.run()

    by_name = {s.name: s for s in spans.get_finished_spans()}
    assert set(by_name) == {"sync.run", "sync.endpoint"}

    run_span = by_name["sync.run"]
    ep_span = by_name["sync.endpoint"]

    # The endpoint span is a child of the run span — the linkage that makes the
    # whole sync read as one trace rather than orphaned fragments.
    assert ep_span.parent.span_id == run_span.context.span_id

    assert run_span.attributes["sync.mode"] == mock_settings.sync_mode
    assert run_span.attributes["sync.endpoint_count"] == len(mock_settings.endpoint_list)
    assert run_span.attributes["sync.total_records"] == 5
    assert ep_span.attributes["sync.endpoint"] == mock_settings.endpoint_list[0]
    assert ep_span.attributes["sync.records"] == 5


# --- log <-> trace correlation ---


def test_trace_context_added_within_span():
    """Inside an active span, log events gain 32-hex trace_id + 16-hex span_id."""
    with trace.get_tracer("t").start_as_current_span("x"):
        out = _add_trace_context(None, "info", {"event": "hi"})
    assert len(out["trace_id"]) == 32
    assert len(out["span_id"]) == 16


def test_trace_context_absent_without_span():
    """No active span → no trace fields (so plain CLI/test logs stay unchanged)."""
    out = _add_trace_context(None, "info", {"event": "hi"})
    assert "trace_id" not in out
    assert "span_id" not in out


@pytest.mark.asyncio()
async def test_endpoint_failure_recorded_on_span(spans, mock_settings):
    """A failing endpoint records its exception on the span but doesn't abort the run."""
    orch = SyncOrchestrator(mock_settings)

    with (
        patch("wp6_data.sync.orchestrator.init_pool", new_callable=AsyncMock),
        patch(
            "wp6_data.sync.orchestrator.feed_for",
            return_value=replace(YOOKR, ensure_schema=AsyncMock()),
        ),
        patch("wp6_data.sync.orchestrator.refresh_sensor_summary_recent", new_callable=AsyncMock),
        patch("wp6_data.sync.orchestrator.close_pool", new_callable=AsyncMock),
        patch.object(
            orch, "_sync_endpoint", new_callable=AsyncMock, side_effect=RuntimeError("boom")
        ),
    ):
        stats = await orch.run()

    ep_span = next(s for s in spans.get_finished_spans() if s.name == "sync.endpoint")
    assert any(e.name == "exception" for e in ep_span.events)
    assert stats["errors"]  # surfaced, not swallowed silently
