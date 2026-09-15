"""
Property-based tests for time-window filtering in the AgentCoreWebSearchEventSource.

Property 9: Time Window Filtering
Validates: Requirements 3.2, 3.5

# Feature: community-scout-austin, Property 9: all output events are within the requested time window

This test suite validates that for any tool invocation parameters (topics,
start_date, end_date) and any mocked Web Search Tool response containing events
with varied start dates, the AgentCoreWebSearchEventSource:

1. Embeds both ``start_date`` and ``end_date`` in the Web Search query text
   (Requirement 3.2).
2. Deterministically post-filters the extracted events using their *verified event
   start date* (parsed from the snippet), never the page's ``publishedDate``
   (Requirements 3.2, 3.5).
3. Returns only events whose ``start_datetime`` date falls within
   ``[start_date, end_date]`` (Requirement 3.5) — every in-window event is kept
   and every out-of-window event is dropped.

The Web Search Tool is mocked by patching
``AgentCoreWebSearchEventSource._invoke_web_search`` so no real network call
occurs. The patch still exercises the real ``_build_arguments`` query
construction so the embedded time window can be asserted.
"""

from datetime import date, timedelta

from hypothesis import given, settings, strategies as st

from src.models.events import CommonEvent
from src.tools.agentcore_web_search_event_source import AgentCoreWebSearchEventSource


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #
@st.composite
def valid_topics(draw) -> list:
    """Generate a valid list of 1–10 topic strings (each 1–200 chars)."""
    return draw(
        st.lists(
            st.text(
                alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ",
                min_size=1,
                max_size=40,
            ).filter(lambda s: s.strip() != ""),
            min_size=1,
            max_size=10,
        )
    )


@st.composite
def time_window(draw):
    """
    Generate an inclusive time window as (start_date, end_date) ISO date strings.

    The window is anchored on an arbitrary base date and spans 0–120 days, so we
    exercise single-day windows through multi-month windows.
    """
    base = draw(
        st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 1, 1))
    )
    span = draw(st.integers(min_value=0, max_value=120))
    start = base
    end = base + timedelta(days=span)
    return start.isoformat(), end.isoformat()


def _iso_datetime(d: date) -> str:
    """Render a date as an ISO 8601 datetime string parseable from a snippet."""
    return f"{d.isoformat()}T18:00:00Z"


@st.composite
def event_dates_around_window(draw, start_date: str, end_date: str):
    """
    Generate a list of candidate event dates, a deliberate mix of in-window and
    out-of-window dates.

    Returns a list of ``date`` objects. Some dates land strictly before the
    window, some strictly after, and some inside (inclusive of the boundaries).
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)

    # In-window dates: uniformly between start and end (inclusive).
    window_days = (end - start).days
    in_window = draw(
        st.lists(
            st.integers(min_value=0, max_value=window_days).map(
                lambda offset: start + timedelta(days=offset)
            ),
            min_size=0,
            max_size=6,
        )
    )

    # Before-window dates: 1–500 days before start.
    before = draw(
        st.lists(
            st.integers(min_value=1, max_value=500).map(
                lambda offset: start - timedelta(days=offset)
            ),
            min_size=0,
            max_size=4,
        )
    )

    # After-window dates: 1–500 days after end.
    after = draw(
        st.lists(
            st.integers(min_value=1, max_value=500).map(
                lambda offset: end + timedelta(days=offset)
            ),
            min_size=0,
            max_size=4,
        )
    )

    dates = in_window + before + after
    dates = draw(st.permutations(dates))
    return list(dates), set(in_window)


@st.composite
def scenario(draw):
    """
    Generate a full test scenario: topics, a time window, and mocked Web Search
    Tool raw results whose snippets carry varied verified event start dates.

    To prove filtering uses the verified event start date and NOT ``publishedDate``,
    each result is given a ``publishedDate`` on the *opposite* side of the window
    from its actual event date: in-window events get an out-of-window publishedDate,
    and out-of-window events get an in-window publishedDate.
    """
    topics = draw(valid_topics())
    start_date, end_date = draw(time_window())
    dates, in_window_set = draw(
        event_dates_around_window(start_date=start_date, end_date=end_date)
    )

    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    mid = start + timedelta(days=(end - start).days // 2)  # a guaranteed in-window date

    raw_results = []
    expected_in_window_datetimes = []  # ISO 8601 UTC datetimes we expect to survive
    for i, event_date in enumerate(dates):
        domain = draw(st.sampled_from(["meetup.com", "lu.ma"]))
        url = f"https://{domain}/event-{i}"
        # Snippet contains the verified event start date plus the city "Austin"
        # (required by the source's extraction), and no other date expression so
        # end-datetime extraction does not pick up a second date.
        snippet = (
            f"Join us in Austin, Texas on {_iso_datetime(event_date)} "
            f"for a great community meetup."
        )

        # publishedDate deliberately on the opposite side of the window.
        if event_date in in_window_set:
            published = (start - timedelta(days=30)).isoformat()  # out of window
            expected_in_window_datetimes.append(_iso_datetime(event_date))
        else:
            published = _iso_datetime(mid)[:10]  # in window

        raw_results.append(
            {
                "title": f"Austin Tech Event {i}",
                "url": url,
                "snippet": snippet,
                "publishedDate": published,
            }
        )

    raw_results = draw(st.permutations(raw_results))
    return {
        "topics": topics,
        "start_date": start_date,
        "end_date": end_date,
        "raw_results": list(raw_results),
        "expected_in_window_datetimes": expected_in_window_datetimes,
    }


# --------------------------------------------------------------------------- #
# Property test
# --------------------------------------------------------------------------- #
@given(scenario())
@settings(max_examples=150)
def test_time_window_filtering(data):
    """
    Property 9: For any tool invocation and any mocked Web Search response with
    varied event dates, every CommonEvent in the filtered output has its
    ``start_datetime`` date within ``[start_date, end_date]``, the time window is
    embedded in the query text, and filtering uses the verified event start date
    rather than the page publication date.

    Validates: Requirements 3.2, 3.5
    """
    topics = data["topics"]
    start_date = data["start_date"]
    end_date = data["end_date"]
    raw_results = data["raw_results"]
    expected_in_window_datetimes = data["expected_in_window_datetimes"]

    source = AgentCoreWebSearchEventSource()

    # Capture the query arguments the source builds, then return the mocked raw
    # results — no real network call occurs.
    captured = {}

    def fake_invoke(topics_arg, start_arg, end_arg):
        # Exercise the real argument/query construction so we can assert the
        # embedded time window (Requirement 3.2).
        captured["arguments"] = source._build_arguments(topics_arg, start_arg, end_arg)
        return raw_results

    source._invoke_web_search = fake_invoke  # type: ignore[assignment]

    result = source(topics, start_date, end_date)

    # The mock never errors, so the result must be a concrete list of events.
    assert isinstance(result, list), f"expected list[CommonEvent], got {type(result)}"
    assert all(isinstance(e, CommonEvent) for e in result)

    # (Req 3.2) The time window is embedded in the query TEXT.
    query_text = captured["arguments"]["query"]
    assert start_date in query_text, (
        f"start_date {start_date!r} not embedded in query text: {query_text!r}"
    )
    assert end_date in query_text, (
        f"end_date {end_date!r} not embedded in query text: {query_text!r}"
    )

    # (Req 3.5) Every returned event's start_datetime date is within [start, end].
    for event in result:
        event_date = event.start_datetime[:10]
        assert start_date <= event_date <= end_date, (
            f"event start date {event_date} outside window "
            f"[{start_date}, {end_date}] for {event.event_url}"
        )

    # (Req 3.2 / 3.5) Filtering is complete AND uses the verified event start date,
    # not publishedDate: every generated in-window event survives (its publishedDate
    # was set out-of-window), and no out-of-window event leaks through (its
    # publishedDate was set in-window).
    returned_start_datetimes = sorted(e.start_datetime for e in result)
    assert returned_start_datetimes == sorted(expected_in_window_datetimes), (
        "post-filtered events do not match the set of events whose verified start "
        "date falls within the window; filtering may be using publishedDate or is "
        "not deterministic"
    )
