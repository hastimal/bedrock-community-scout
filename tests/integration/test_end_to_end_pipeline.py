"""
End-to-end integration test for the Community Scout discovery pipeline.

Task 15.1: Write end-to-end integration test with mocked tool responses.
Validates: Requirement 10.4 — the Bedrock_Agent completes the full discovery
workflow (tool invocation, normalization, deduplication, ranking, and response
generation) within a single agentic session.

This test wires together the real production components exactly as the Scout
Orchestrator does:

    AgentCoreWebSearchEventSource (mocked Web Search Tool)
        -> aggregate()
        -> deduplicate()
        -> rank()
        -> format_response()

The only thing that is mocked is the Web Search Tool MCP call itself: we patch
``AgentCoreWebSearchEventSource._invoke_web_search`` so no live network or AWS
call is made. Everything downstream (snippet extraction, normalization,
aggregation, deduplication, ranking, and response rendering) runs the real code.

The scenario submits a known query and feeds in fixed meetup.com and lu.ma
event snippets. It then asserts that:

1. The ranked output contains the correct events (the ones inside the requested
   time window that match the query topics).
2. The events appear in the expected order (relevance score descending, then
   start date ascending on ties).
3. Every ``event_url`` is preserved byte-for-byte from the source snippet —
   never truncated, re-encoded, or otherwise altered.
"""

from datetime import datetime, timedelta, timezone

from src.models.events import QueryContext
from src.pipeline.aggregator import aggregate
from src.pipeline.deduplicator import deduplicate
from src.pipeline.ranker import rank
from src.pipeline.response_generator import format_response
from src.tools.agentcore_web_search_event_source import AgentCoreWebSearchEventSource


# --------------------------------------------------------------------------- #
# Fixed, known query
# --------------------------------------------------------------------------- #
# Two topics. The event snippets below are crafted so that one event matches
# both topics (highest topic_score), some match a single topic, and one matches
# neither — giving a deterministic, non-trivial ranking order.
TOPICS = ["Agentic AI", "AWS"]


def _relative_date(days_from_today: int) -> str:
    """Return a YYYY-MM-DD date string offset from today (UTC).

    Event dates are anchored relative to "today" so the recency component of the
    ranker (which depends on days-until-event) is stable regardless of when the
    test runs, while the events stay comfortably inside the requested window.
    """
    day = datetime.now(timezone.utc).date() + timedelta(days=days_from_today)
    return day.isoformat()


def _iso_datetime(date_str: str, time_suffix: str = "T18:00:00Z") -> str:
    """Compose an ISO 8601 UTC datetime string used inside a snippet."""
    return f"{date_str}{time_suffix}"


def _build_mocked_web_search_results():
    """Build the fixed set of mocked Web Search Tool result dicts.

    These mimic the meetup.com and lu.ma snippets the AgentCore Web Search Tool
    would return. Each result carries a ``url``, ``title``, and ``snippet``; the
    source's real extractor parses the event date and city ("Austin") out of the
    snippet text. URLs deliberately include query strings and mixed casing so the
    byte-for-byte preservation assertion is meaningful.

    Returns:
        A tuple ``(raw_results, expected_order)`` where ``expected_order`` is the
        list of ``event_url`` values in the exact order the ranker should emit
        them.
    """
    # In-window event dates (the requested window spans today .. today+60).
    date_both = _relative_date(10)   # matches BOTH topics -> highest topic score
    date_ai = _relative_date(20)     # matches ONE topic (Agentic AI)
    date_aws = _relative_date(40)    # matches ONE topic (AWS), later start date
    date_none = _relative_date(5)    # matches NO topic -> score 0

    # Out-of-window event (well past the requested window) — must be dropped by
    # the source's time-window post-filter.
    date_out = _relative_date(120)

    # Byte-sensitive URLs: query strings, fragments, mixed case.
    url_both = "https://www.meetup.com/austin-ai/events/Agentic-AI-AWS-301/?utm_source=Scout&ref=Home#details"
    url_ai = "https://lu.ma/austin-agentic-ai-workshop?tk=AbC123"
    url_aws = "https://www.meetup.com/austin-cloud/events/AWS-Deep-Dive-88/"
    url_none = "https://lu.ma/austin-board-game-night"
    url_out = "https://www.meetup.com/austin-ai/events/future-agentic-ai-999/"

    raw_results = [
        {
            "title": "Agentic AI on AWS: Building Autonomous Agents",
            "url": url_both,
            "snippet": (
                f"Join us in Austin, Texas on {_iso_datetime(date_both)} for a hands-on "
                f"session on Agentic AI and AWS. We'll build autonomous agents together."
            ),
        },
        {
            "title": "Agentic AI Workshop",
            "url": url_ai,
            "snippet": (
                f"An Austin community workshop on {_iso_datetime(date_ai)} exploring "
                f"Agentic AI patterns and multi-agent orchestration."
            ),
        },
        {
            "title": "AWS Cloud Deep Dive",
            "url": url_aws,
            "snippet": (
                f"Austin, Texas meetup on {_iso_datetime(date_aws)} covering AWS cloud "
                f"architecture, cost optimization, and serverless."
            ),
        },
        {
            "title": "Austin Board Game Night",
            "url": url_none,
            "snippet": (
                f"A relaxed evening in Austin on {_iso_datetime(date_none)} for board "
                f"games and snacks. No laptops required."
            ),
        },
        {
            "title": "Future of Agentic AI (Save the Date)",
            "url": url_out,
            "snippet": (
                f"A far-future gathering in Austin on {_iso_datetime(date_out)} about "
                f"Agentic AI and AWS. Outside the requested window."
            ),
        },
    ]

    # Expected ranked order (relevance desc, then start date asc on ties):
    #   1. url_both  — matches both topics (topic_score = 80).
    #   2. url_ai    — matches one topic (Agentic AI), starts day+20.
    #   3. url_aws   — matches one topic (AWS), starts day+40 (later than url_ai;
    #                  url_ai also has a higher recency score since it is sooner).
    #   4. url_none  — matches no topic (score 0).
    # url_out is dropped entirely by the time-window post-filter.
    expected_order = [url_both, url_ai, url_aws, url_none]
    return raw_results, expected_order


def _make_source_with_mocked_web_search(raw_results):
    """Return an AgentCoreWebSearchEventSource whose Web Search call is mocked.

    Patches ``_invoke_web_search`` on the instance so the real network/AWS call is
    never made. The mock still runs the real ``_build_arguments`` so the domain
    filter and embedded time window are exercised, and returns the fixed raw
    results for the source's real extraction/normalization/filtering code.
    """
    source = AgentCoreWebSearchEventSource()
    captured = {}

    def fake_invoke(topics_arg, start_arg, end_arg):
        captured["arguments"] = source._build_arguments(topics_arg, start_arg, end_arg)
        return raw_results

    source._invoke_web_search = fake_invoke  # type: ignore[assignment]
    return source, captured


def test_end_to_end_pipeline_ranks_events_in_expected_order_with_unmodified_urls():
    """Full pipeline: mocked Web Search -> aggregate -> dedup -> rank -> format.

    Asserts the ranked output contains the correct events in the expected order
    and that every source URL is preserved byte-for-byte.

    Validates: Requirement 10.4
    """
    raw_results, expected_order = _build_mocked_web_search_results()
    original_urls = [r["url"] for r in raw_results]

    start_date = _relative_date(0)
    end_date = _relative_date(60)

    source, captured = _make_source_with_mocked_web_search(raw_results)

    # --- Tool invocation (mocked Web Search Tool) --------------------------- #
    tool_result = source(TOPICS, start_date, end_date)
    assert isinstance(tool_result, list), (
        f"expected list[CommonEvent] from the event source, got {type(tool_result)}"
    )

    # The domain filter and time window must be embedded in the Web Search call.
    args = captured["arguments"]
    assert args["filters"]["domainFilter"] == {"include": ["meetup.com", "lu.ma"]}
    assert start_date in args["query"]
    assert end_date in args["query"]

    # --- Aggregate -> deduplicate ------------------------------------------ #
    events, errors = aggregate([tool_result])
    assert errors == [], "no tool failures expected in this scenario"

    deduplicated = deduplicate(events)

    # The out-of-window event is filtered by the source; four events remain and
    # there are no duplicates in this scenario.
    assert len(deduplicated) == len(expected_order), (
        f"expected {len(expected_order)} events after dedup, got {len(deduplicated)}"
    )

    # --- Rank --------------------------------------------------------------- #
    context = QueryContext(
        topics=TOPICS,
        location="Austin, Texas",
        start_date=start_date,
        end_date=end_date,
    )
    ranked = rank(deduplicated, context)

    # (Req 10.4) Correct events in the expected order.
    actual_order = [r.event.event_url for r in ranked]
    assert actual_order == expected_order, (
        "ranked output is not in the expected order.\n"
        f"expected: {expected_order}\n"
        f"actual:   {actual_order}"
    )

    # Relevance scores must be strictly non-increasing down the ranked list.
    scores = [r.relevance_score for r in ranked]
    assert scores == sorted(scores, reverse=True), (
        f"ranked scores are not sorted descending: {scores}"
    )
    # The two-topic match must outscore every single-topic match, which in turn
    # must outscore the no-match event. The no-match event ranks last even though
    # it has the nearest start date, because it earns zero topic score (its score
    # is purely the recency component) while the topic-matching events do not.
    assert scores[0] > scores[1], "two-topic event should outscore single-topic events"
    last_ranked = ranked[-1]
    assert last_ranked.event.event_url == expected_order[-1]
    # Its full score comes from recency alone (topic_score == 0), so it stays below
    # every event that matched at least one topic (each earning >= 40 topic points).
    assert last_ranked.relevance_score < 40.0, (
        "the no-topic-match event should rank below topic-matching events"
    )

    # --- Format response ---------------------------------------------------- #
    response = format_response(ranked, context, errors)

    # Header reports the post-deduplication count.
    assert "Found 4 events in Austin, Texas:" in response

    # (Req 10.4 / URL immutability) Every source URL appears byte-for-byte in the
    # rendered response, as a markdown hyperlink, and the dropped out-of-window
    # URL never appears.
    for url in expected_order:
        assert url in response, f"event_url missing or altered in response: {url!r}"
    dropped_url = next(u for u in original_urls if u not in expected_order)
    assert dropped_url not in response, (
        "out-of-window event leaked into the response"
    )

    # The two matching events render their exact URLs inside markdown links.
    assert f"]({expected_order[0]})" in response
    assert f"]({expected_order[1]})" in response


def test_end_to_end_pipeline_preserves_event_urls_byte_for_byte():
    """The event_url survives the whole pipeline unchanged, byte for byte.

    Captures the URLs coming out of the event source and confirms the exact same
    strings appear (unmodified) after deduplicate -> rank -> format_response.

    Validates: Requirement 10.4 (URL immutability across the single session)
    """
    raw_results, expected_order = _build_mocked_web_search_results()

    start_date = _relative_date(0)
    end_date = _relative_date(60)

    source, _ = _make_source_with_mocked_web_search(raw_results)
    tool_result = source(TOPICS, start_date, end_date)

    # URLs as produced by the event source (post normalization/filtering).
    source_urls = [event.event_url for event in tool_result]

    events, errors = aggregate([tool_result])
    deduplicated = deduplicate(events)
    context = QueryContext(
        topics=TOPICS,
        location="Austin, Texas",
        start_date=start_date,
        end_date=end_date,
    )
    ranked = rank(deduplicated, context)

    ranked_urls = [r.event.event_url for r in ranked]

    # Same set of URLs, byte-for-byte, before and after dedup/rank.
    assert sorted(ranked_urls) == sorted(source_urls), (
        "dedup/rank changed the set of event_url values"
    )

    # Each URL is byte-identical to the corresponding source snippet URL.
    response = format_response(ranked, context, errors)
    for url in source_urls:
        assert url in response, f"event_url was altered before rendering: {url!r}"
