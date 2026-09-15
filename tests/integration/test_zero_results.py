"""
End-to-end integration test for the zero-results discovery path.

Task 15.4: Write integration test for zero results.
Validates: Requirements 8.4 and 9.3.

Requirement 8.4: When the combined results contain zero events after
deduplication, the Response Generator informs the user that no events were found
for the specified Topics and Time Window in Austin, Texas, and suggests
broadening the search criteria.

Requirement 9.3: When no Event_Source_Tool returns results, the Scout displays a
"no events found" message without generating any placeholder or fabricated
events.

This test drives the same production components the Scout Orchestrator wires
together, but forces the Web Search Tool to return an empty result set:

    AgentCoreWebSearchEventSource (Web Search returns [])
        -> aggregate()
        -> deduplicate()
        -> rank()
        -> format_response()

Only the Web Search Tool MCP call is mocked: ``_invoke_web_search`` is patched to
return an empty list, exactly as the real source would when the domain-filtered
query matches nothing. Everything downstream runs the real code, so the test
exercises the actual empty-result path end to end.

The scenario asserts that:

1. The event source returns an empty ``list[CommonEvent]`` (never a ToolError,
   never a fabricated event) when the Web Search Tool yields no results.
2. Aggregation, deduplication, and ranking all yield nothing.
3. The rendered response is the "no events found" message that names the
   requested topics, Austin, Texas, and the requested time window, suggests
   broadening the search, and reports a count of 0 (Requirements 8.4, 9.3).
4. No event-list content (URLs, "Source:", etc.) leaks into the response — no
   placeholder or fabricated events are manufactured.
"""

from datetime import datetime, timedelta, timezone

from src.models.events import CommonEvent, QueryContext, ToolError
from src.pipeline.aggregator import aggregate
from src.pipeline.deduplicator import deduplicate
from src.pipeline.ranker import rank
from src.pipeline.response_generator import format_response
from src.tools.agentcore_web_search_event_source import AgentCoreWebSearchEventSource


TOPICS = ["Agentic AI", "AWS"]


def _relative_date(days_from_today: int) -> str:
    """Return a YYYY-MM-DD date string offset from today (UTC)."""
    day = datetime.now(timezone.utc).date() + timedelta(days=days_from_today)
    return day.isoformat()


def _make_source_with_empty_web_search():
    """Return an AgentCoreWebSearchEventSource whose Web Search call yields [].

    Patches ``_invoke_web_search`` on the instance so no real network/AWS call is
    made and the source's real empty-result handling runs. The mock still runs the
    real ``_build_arguments`` so the domain filter and embedded time window are
    exercised, and returns an empty list (the Web Search Tool matched nothing).
    """
    source = AgentCoreWebSearchEventSource()
    captured = {}

    def fake_invoke(topics_arg, start_arg, end_arg):
        captured["arguments"] = source._build_arguments(topics_arg, start_arg, end_arg)
        return []

    source._invoke_web_search = fake_invoke  # type: ignore[assignment]
    return source, captured


def test_zero_results_renders_no_events_message_with_count_zero():
    """Empty Web Search results -> a "no events found" message reporting count 0.

    The full pipeline runs against a Web Search Tool that returns nothing. The
    rendered response must inform the user that no events were found for the
    requested topics in Austin, Texas within the requested time window, suggest
    broadening the search, and report a count of 0 — without fabricating any
    placeholder events.

    Validates: Requirements 8.4, 9.3
    """
    start_date = _relative_date(0)
    end_date = _relative_date(60)

    source, captured = _make_source_with_empty_web_search()

    # --- Tool invocation (mocked Web Search Tool returns []) ---------------- #
    tool_result = source(TOPICS, start_date, end_date)

    # (Req 9.3) An empty result set surfaces as an empty list of events — never a
    # ToolError and never a fabricated event.
    assert isinstance(tool_result, list), (
        f"expected list[CommonEvent] from the event source, got {type(tool_result)}"
    )
    assert tool_result == [], "no events should be produced from empty Web Search results"

    # The domain filter and time window were still embedded in the Web Search call.
    args = captured["arguments"]
    assert args["filters"]["domainFilter"] == {"include": ["meetup.com", "lu.ma"]}
    assert start_date in args["query"]
    assert end_date in args["query"]

    # --- Aggregate -> deduplicate -> rank ----------------------------------- #
    events, errors = aggregate([tool_result])
    assert events == [], "aggregation should yield no events"
    assert errors == [], "no tool failures occurred; the source simply found nothing"

    deduplicated = deduplicate(events)
    assert deduplicated == [], "deduplication of an empty set yields an empty set"

    context = QueryContext(
        topics=TOPICS,
        location="Austin, Texas",
        start_date=start_date,
        end_date=end_date,
    )
    ranked = rank(deduplicated, context)
    assert ranked == [], "ranking an empty set yields an empty set"

    # --- Format response ---------------------------------------------------- #
    response = format_response(ranked, context, errors)

    # (Req 8.4 / 9.3) The response reports a count of 0 and is the no-events message.
    assert "Found 0 events in Austin, Texas." in response

    # (Req 8.4) The message names the requested topics, Austin, Texas, and the
    # requested time window, and suggests broadening the search.
    for topic in TOPICS:
        assert topic in response, f"requested topic missing from message: {topic!r}"
    assert "Austin, Texas" in response
    assert start_date in response
    assert end_date in response
    assert "broadening" in response.lower(), "message should suggest broadening the search"

    # (Req 9.3) No placeholder or fabricated events: no event-list rendering leaks in.
    assert "http" not in response, "no event URL should appear in a zero-results response"
    assert "- Source:" not in response, "no event entry should be rendered"
    assert "Found 1 event" not in response


def test_zero_results_is_distinct_from_a_tool_failure():
    """A genuine zero-results case reports no failed sources (Req 9.3 vs 2.4).

    Finding nothing is not the same as a source failing. With an empty Web Search
    result and no ToolError, the response must be the plain "no events found"
    message with no failure footer.

    Validates: Requirements 8.4, 9.3
    """
    start_date = _relative_date(0)
    end_date = _relative_date(30)

    source, _ = _make_source_with_empty_web_search()
    tool_result = source(TOPICS, start_date, end_date)
    assert tool_result == []
    assert not isinstance(tool_result, ToolError)

    events, errors = aggregate([tool_result])
    deduplicated = deduplicate(events)
    context = QueryContext(
        topics=TOPICS,
        location="Austin, Texas",
        start_date=start_date,
        end_date=end_date,
    )
    ranked = rank(deduplicated, context)

    response = format_response(ranked, context, errors)

    assert "Found 0 events in Austin, Texas." in response
    # No source failed, so no failure footer is present.
    assert "could not be retrieved" not in response
    assert "---" not in response


def test_zero_results_across_multiple_empty_sources_reports_count_zero():
    """Multiple sources all returning nothing still yields a single count-0 message.

    Requirement 9.3 covers the case where *no* Event_Source_Tool returns results.
    Aggregating several empty results must still produce the no-events message with
    a count of 0 and no fabricated events.

    Validates: Requirements 8.4, 9.3
    """
    start_date = _relative_date(0)
    end_date = _relative_date(45)

    source, _ = _make_source_with_empty_web_search()
    first = source(TOPICS, start_date, end_date)
    second = source(TOPICS, start_date, end_date)
    assert first == [] and second == []

    # Two independent Event_Source_Tool invocations, both empty.
    events, errors = aggregate([first, second])
    assert events == []
    assert errors == []

    deduplicated = deduplicate(events)
    context = QueryContext(
        topics=TOPICS,
        location="Austin, Texas",
        start_date=start_date,
        end_date=end_date,
    )
    ranked = rank(deduplicated, context)
    assert ranked == []

    response = format_response(ranked, context, errors)
    assert "Found 0 events in Austin, Texas." in response
    for topic in TOPICS:
        assert topic in response
    assert "broadening" in response.lower()
    # No CommonEvent was ever constructed — nothing fabricated.
    assert not any(isinstance(e, CommonEvent) for e in events)
