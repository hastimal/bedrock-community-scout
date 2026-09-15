"""
Integration test for AgentCore Web Search Tool timeout handling.

Task 15.2: Write integration test for AgentCore tool timeout.
Validates: Requirements 2.4 and 10.5.

Requirement 2.4: If an Event_Source_Tool does not respond within 10 seconds or
returns an error, the Scout SHALL record an error entry (tool name + failure
reason), exclude that tool's results, and continue processing the remaining
tools.

Requirement 10.5: If the AgentCore runtime returns an error for a tool
invocation, the Bedrock_Agent follows the error-handling behavior in
Requirement 2.4.

This test drives the same production components the Scout Orchestrator wires
together, but forces the Web Search Tool MCP call to time out:

    AgentCoreWebSearchEventSource (Web Search MCP call times out)
        -> aggregate()
        -> deduplicate()
        -> rank()
        -> format_response()

The timeout is injected at the real MCP boundary: the AgentCore Gateway Web
Search connector is replaced with a fake ``MCPClient`` whose ``call_tool_sync``
raises a ``TimeoutError`` (as the Strands MCP client would when the tool does
not respond within ``read_timeout_seconds``). Everything else runs the real
code so the test exercises the actual error-containment path in
``AgentCoreWebSearchEventSource.__call__``.

The scenario asserts that:

1. The event source surfaces the failure as a ``ToolError`` (tool name +
   reason) rather than raising an exception (Requirement 2.4 / 10.5).
2. No partial ``CommonEvent`` records accompany the failure — a ``ToolError``
   is never returned alongside events.
3. The pipeline continues: aggregation collects the error, ranking yields no
   events, and the rendered response reports the failed source in its footer
   without raising.
4. When a timing-out source is combined with a healthy source, the healthy
   source's events still flow through while the timeout is reported in the
   footer (Requirement 2.4 — "continue processing remaining tools").
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.models.events import CommonEvent, QueryContext, ToolError
from src.pipeline.aggregator import aggregate
from src.pipeline.deduplicator import deduplicate
from src.pipeline.ranker import rank
from src.pipeline.response_generator import format_response
from src.tools import agentcore_web_search_event_source as event_source_module
from src.tools.agentcore_web_search_event_source import AgentCoreWebSearchEventSource


TOPICS = ["Agentic AI", "AWS"]


def _relative_date(days_from_today: int) -> str:
    """Return a YYYY-MM-DD date string offset from today (UTC)."""
    day = datetime.now(timezone.utc).date() + timedelta(days=days_from_today)
    return day.isoformat()


# --------------------------------------------------------------------------- #
# Fakes that simulate a Web Search Tool timeout at the real MCP boundary
# --------------------------------------------------------------------------- #
class _TimingOutMCPClient:
    """A stand-in for the Strands ``MCPClient`` whose tool call always times out.

    Used as a context manager exactly like the real client. ``call_tool_sync``
    raises ``TimeoutError`` to mimic the Web Search Tool failing to respond
    within ``read_timeout_seconds`` (the 10-second budget in Requirement 2.4).
    """

    def __init__(self, transport_factory):
        # The real client is constructed with a transport factory callable; accept
        # and ignore it so construction matches the production call site.
        self._transport_factory = transport_factory

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def call_tool_sync(self, *args, **kwargs):
        # Emulate the MCP client aborting after read_timeout_seconds elapses.
        timeout = kwargs.get("read_timeout_seconds", "?")
        raise TimeoutError(
            f"Web Search Tool did not respond within {timeout} seconds"
        )


def _install_timing_out_mcp_client(monkeypatch):
    """Patch the module so the event source builds a timing-out MCP client.

    Replaces ``MCPClient`` on the event-source module with ``_TimingOutMCPClient``
    and stubs the transport factory so no real network/AWS connection is
    attempted. This forces the real ``_invoke_web_search`` code path to run and
    fail with a timeout, which ``__call__`` must convert into a ``ToolError``.
    """
    monkeypatch.setattr(
        event_source_module, "MCPClient", _TimingOutMCPClient, raising=False
    )

    # The transport factory is only called inside _TimingOutMCPClient's (unused)
    # factory, but stub it defensively so no live connector is created.
    def _fake_transport(self):
        return object()

    monkeypatch.setattr(
        AgentCoreWebSearchEventSource, "_create_mcp_transport", _fake_transport
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_agentcore_tool_timeout_returns_tool_error_without_partial_results(monkeypatch):
    """A Web Search Tool timeout yields a ToolError, no events, and no exception.

    The event source must catch the timeout raised by the MCP client and return
    a ``ToolError`` naming the tool and describing the failure — never a partial
    event list and never a raised exception.

    Validates: Requirements 2.4, 10.5
    """
    _install_timing_out_mcp_client(monkeypatch)

    source = AgentCoreWebSearchEventSource()
    start_date = _relative_date(0)
    end_date = _relative_date(60)

    # The call must not raise — the timeout is encoded in the return value.
    result = source(TOPICS, start_date, end_date)

    # (Req 2.4 / 10.5) The failure is surfaced as a ToolError, not a partial list.
    assert isinstance(result, ToolError), (
        f"expected a ToolError on timeout, got {type(result)}: {result!r}"
    )
    assert result.tool_name == "AgentCoreWebSearchEventSource"
    # The reason names the failure; it must be a non-empty, human-readable string.
    assert result.reason and isinstance(result.reason, str)
    assert "10" in result.reason or "timed out" in result.reason.lower() or (
        "timeout" in result.reason.lower()
    ), f"reason should describe the timeout, got: {result.reason!r}"


def test_agentcore_tool_timeout_flows_through_pipeline_into_error_footer(monkeypatch):
    """The timeout ToolError flows through the pipeline into the response footer.

    With the only source timing out, aggregation must yield zero events and one
    error, ranking yields nothing, and the rendered response reports the failed
    source in its footer — all without raising.

    Validates: Requirements 2.4, 10.5
    """
    _install_timing_out_mcp_client(monkeypatch)

    source = AgentCoreWebSearchEventSource()
    start_date = _relative_date(0)
    end_date = _relative_date(60)

    tool_result = source(TOPICS, start_date, end_date)
    assert isinstance(tool_result, ToolError)

    # --- Aggregate: the error is collected; no partial events leak through --- #
    events, errors = aggregate([tool_result])
    assert events == [], "no events should accompany a timed-out source"
    assert len(errors) == 1
    assert errors[0].tool_name == "AgentCoreWebSearchEventSource"

    deduplicated = deduplicate(events)
    assert deduplicated == []

    context = QueryContext(
        topics=TOPICS,
        location="Austin, Texas",
        start_date=start_date,
        end_date=end_date,
    )
    ranked = rank(deduplicated, context)
    assert ranked == []

    # --- Format: the failed source is reported in the footer, no exception --- #
    response = format_response(ranked, context, errors)
    assert "AgentCoreWebSearchEventSource" in response
    assert "could not be retrieved" in response
    # No events were found, so the no-events message is present.
    assert "Found 0 events in Austin, Texas." in response


def test_agentcore_tool_timeout_does_not_block_healthy_source(monkeypatch):
    """A timed-out source does not stop a healthy source's events (Req 2.4).

    Simulates two Event_Source_Tool invocations: one that times out (ToolError)
    and one healthy source returning a real event. The Scout must exclude the
    failed tool's results, keep the healthy event, and report the failure in the
    footer — "continue processing remaining tools".

    Validates: Requirements 2.4, 10.5
    """
    _install_timing_out_mcp_client(monkeypatch)

    source = AgentCoreWebSearchEventSource()
    start_date = _relative_date(0)
    end_date = _relative_date(60)

    timed_out = source(TOPICS, start_date, end_date)
    assert isinstance(timed_out, ToolError)

    # A healthy sibling source result: one real, in-window event.
    healthy_event = CommonEvent(
        title="Agentic AI on AWS Meetup",
        start_datetime=f"{_relative_date(15)}T18:00:00Z",
        city="Austin, Texas",
        source_platform="Meetup",
        event_url="https://www.meetup.com/austin-ai/events/healthy-123/",
        match_confidence=0.9,
        source_invocation_order=1,
    )

    # Aggregate the mixed results in invocation order: [timeout, healthy].
    events, errors = aggregate([timed_out, [healthy_event]])

    # The healthy event survives; the timeout is recorded as an error.
    assert events == [healthy_event]
    assert len(errors) == 1
    assert errors[0].tool_name == "AgentCoreWebSearchEventSource"

    deduplicated = deduplicate(events)
    context = QueryContext(
        topics=TOPICS,
        location="Austin, Texas",
        start_date=start_date,
        end_date=end_date,
    )
    ranked = rank(deduplicated, context)

    # The healthy event is ranked and rendered; the timeout appears in the footer.
    assert [r.event.event_url for r in ranked] == [healthy_event.event_url]

    response = format_response(ranked, context, errors)
    assert "Found 1 event in Austin, Texas:" in response
    assert healthy_event.event_url in response
    assert "AgentCoreWebSearchEventSource" in response
    assert "could not be retrieved" in response


def test_agentcore_tool_timeout_never_raises_from_entry_point(monkeypatch):
    """The tool entry point encodes the timeout in its return value, never raises.

    Explicitly asserts that invoking the event source under a timing-out MCP
    client does not propagate any exception to the caller.

    Validates: Requirements 2.4, 10.5
    """
    _install_timing_out_mcp_client(monkeypatch)

    source = AgentCoreWebSearchEventSource()

    try:
        result = source(TOPICS, _relative_date(0), _relative_date(30))
    except Exception as exc:  # noqa: BLE001 - the whole point is that this cannot happen
        pytest.fail(f"event source raised instead of returning a ToolError: {exc!r}")

    assert isinstance(result, ToolError)
