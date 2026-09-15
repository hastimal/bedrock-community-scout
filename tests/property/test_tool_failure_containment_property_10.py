"""
Property-based tests for tool failure containment in the aggregator.

Property 10: Tool Failure Containment
Validates: Requirements 2.5, 11.5

# Feature: community-scout-austin, Property 10: failed tools are contained without aborting the session

This test suite validates that for any set of N event-source tool invocations
where k tools fail (0 < k < N), the aggregate() function:

1. Includes all events from the (N - k) successful tools in the returned events
   list (same count, and every originating event present).
2. Records exactly k error entries -- one per failed tool.
3. Does not raise an exception (the session is not aborted).

Successful tools contribute a ``list[CommonEvent]`` to ``tool_results``; failed
tools contribute a single ``ToolError``.
"""

from collections import Counter

from hypothesis import given, settings, strategies as st

from src.models.events import CommonEvent, ToolError
from src.pipeline.aggregator import aggregate


@st.composite
def valid_iso8601_utc_datetime(draw) -> str:
    """Generate valid ISO 8601 UTC datetime strings (YYYY-MM-DDTHH:MM:SSZ)."""
    year = draw(st.integers(min_value=2000, max_value=2100))
    month = draw(st.integers(min_value=1, max_value=12))
    day = draw(st.integers(min_value=1, max_value=28))  # Avoid month-length edge cases.
    hour = draw(st.integers(min_value=0, max_value=23))
    minute = draw(st.integers(min_value=0, max_value=59))
    second = draw(st.integers(min_value=0, max_value=59))
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"


@st.composite
def valid_non_empty_string(draw, max_size: int = 200) -> str:
    """Generate non-empty strings that satisfy required-field invariants."""
    return draw(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_.,':;",
            min_size=1,
            max_size=max_size,
        ).filter(lambda s: s.strip() != "")
    )


@st.composite
def common_event(draw) -> CommonEvent:
    """Generate a valid CommonEvent record satisfying all model invariants."""
    return CommonEvent(
        title=draw(valid_non_empty_string(max_size=200)),
        start_datetime=draw(valid_iso8601_utc_datetime()),
        city=draw(valid_non_empty_string(max_size=100)),
        source_platform=draw(st.sampled_from(["Meetup", "Luma", "WebSearch"])),
        event_url=draw(valid_non_empty_string(max_size=200)),
        description=draw(st.one_of(st.none(), valid_non_empty_string(max_size=500))),
        end_datetime=draw(st.one_of(st.none(), valid_iso8601_utc_datetime())),
        location_name=draw(st.one_of(st.none(), valid_non_empty_string(max_size=100))),
        match_confidence=draw(st.floats(min_value=0.0, max_value=1.0)),
        source_invocation_order=draw(st.integers(min_value=0, max_value=20)),
    )


@st.composite
def tool_error(draw) -> ToolError:
    """Generate a valid ToolError satisfying its non-empty-string invariants."""
    return ToolError(
        tool_name=draw(valid_non_empty_string(max_size=100)),
        reason=draw(valid_non_empty_string(max_size=200)),
    )


@st.composite
def mixed_tool_results(draw):
    """
    Generate a mix of N tool results where k fail (0 < k < N, N >= 2).

    Returns a tuple ``(tool_results, success_lists, error_list)`` where:
    - ``tool_results`` is the shuffled list of ``list[CommonEvent] | ToolError``
      passed to aggregate().
    - ``success_lists`` is the list of the successful tools' event lists.
    - ``error_list`` is the list of ToolError entries.
    """
    n = draw(st.integers(min_value=2, max_value=10))
    # k failures with 0 < k < N.
    k = draw(st.integers(min_value=1, max_value=n - 1))

    # Successful tools: each contributes a (possibly empty) list of CommonEvents.
    success_lists = [
        draw(st.lists(common_event(), min_size=0, max_size=5))
        for _ in range(n - k)
    ]
    # Failed tools: each contributes a single ToolError.
    error_list = [draw(tool_error()) for _ in range(k)]

    # Combine and shuffle to exercise arbitrary interleavings of success/failure.
    tool_results: list = list(success_lists) + list(error_list)
    tool_results = draw(st.permutations(tool_results))

    return list(tool_results), success_lists, error_list, n, k


@given(mixed_tool_results())
@settings(max_examples=200)
def test_tool_failure_containment(data):
    """
    Property 10: For N tool invocations where k fail (0 < k < N), aggregate()
    returns all events from the (N - k) successful tools and exactly k error
    entries, without aborting the session.

    Validates: Requirements 2.5, 11.5
    """
    tool_results, success_lists, error_list, n, k = data

    # 3. No exception is raised (session is not aborted).
    events, errors = aggregate(tool_results)

    # 1. Returned events are exactly the union of all successful tools' events.
    expected_events = [event for lst in success_lists for event in lst]
    assert len(events) == len(expected_events), (
        f"aggregate() returned {len(events)} events; expected {len(expected_events)} "
        f"(union of {n - k} successful tools)"
    )
    # Every originating event must be present. Compare by object identity so that
    # even equal-but-distinct events are all accounted for.
    expected_ids = Counter(id(event) for event in expected_events)
    actual_ids = Counter(id(event) for event in events)
    assert actual_ids == expected_ids, (
        "aggregate() events are not exactly the union of the successful tools' events"
    )

    # 2. Exactly k error entries -- one per failed tool.
    assert len(errors) == k, f"aggregate() returned {len(errors)} errors; expected k={k}"
    expected_error_ids = Counter(id(err) for err in error_list)
    actual_error_ids = Counter(id(err) for err in errors)
    assert actual_error_ids == expected_error_ids, (
        "aggregate() errors are not exactly the k failed tools' ToolError entries"
    )

    # Every returned error is a ToolError and every returned event is a CommonEvent.
    assert all(isinstance(err, ToolError) for err in errors)
    assert all(isinstance(event, CommonEvent) for event in events)
