"""
Unit tests for source-aware AgentCore Web Search discovery.

The EventSource searches each configured source (meetup.com, lu.ma) INDEPENDENTLY —
one domain-scoped Web Search call per source — so a single source cannot crowd the
other out of the result set. Candidates from all sources flow through the existing
pipeline (evidence extraction -> topic gate -> date/Austin validation -> normalization
-> cross-source deduplication -> deterministic ranking) and the top N (max 10) unique
relevant events are returned. The cap is a maximum, never a quota.

These tests exercise that behavior by mocking the per-source Web Search call
(``_invoke_web_search_for_source``) so no network/AWS/Gateway call occurs. Query
construction and the domain filter are asserted separately.
"""

from src.tools.agentcore_web_search_event_source import AgentCoreWebSearchEventSource


WINDOW = ("2026-09-15", "2026-12-14")


def _result(title, url, date="2026-10-07", extra_topic_text=""):
    """A minimal real-shaped Web Search result carrying on-topic evidence."""
    text = f"Austin, Texas on {date}T18:30:00Z. {title}. {extra_topic_text}".strip()
    return {"title": title, "url": url, "text": text}


def _source_with_per_source(meetup_results, luma_results):
    """Return a source whose per-source Web Search call is mocked per domain."""
    source = AgentCoreWebSearchEventSource()

    def per_source(topics, start, end, src):
        if src == "meetup.com":
            return list(meetup_results)
        if src == "lu.ma":
            return list(luma_results)
        return []

    source._invoke_web_search_for_source = per_source  # type: ignore[assignment]
    return source


# --------------------------------------------------------------------------- #
# Query strategy: each source searched independently and domain-scoped
# --------------------------------------------------------------------------- #
def test_sources_are_searched_independently_one_call_each():
    """One domain-scoped call is made per source, each with a source-specific query."""
    source = AgentCoreWebSearchEventSource()
    calls = []

    def per_source(topics, start, end, src):
        calls.append(src)
        return []

    source._invoke_web_search_for_source = per_source  # type: ignore[assignment]
    source(["AWS"], *WINDOW)

    # Exactly the configured sources, each queried once.
    assert calls == ["meetup.com", "lu.ma"]


def test_build_arguments_scopes_domain_filter_and_query_per_source():
    """Per-source arguments scope the domain filter to a single source."""
    source = AgentCoreWebSearchEventSource()

    meetup_args = source._build_arguments(["AWS"], *WINDOW, source="meetup.com")
    luma_args = source._build_arguments(["AWS"], *WINDOW, source="lu.ma")

    assert meetup_args["filters"]["domainFilter"] == {"include": ["meetup.com"]}
    assert luma_args["filters"]["domainFilter"] == {"include": ["lu.ma"]}
    # Source-specific query text so one source doesn't crowd the other out.
    assert "site:meetup.com" in meetup_args["query"]
    assert "site:lu.ma" in luma_args["query"]
    # More candidates than the final cap are requested per source.
    assert meetup_args["maxResults"] >= AgentCoreWebSearchEventSource._MAX_FINAL_RESULTS

    # Backward-compatible combined filter when no source is given.
    combined = source._build_arguments(["AWS"], *WINDOW)
    assert combined["filters"]["domainFilter"] == {"include": ["meetup.com", "lu.ma"]}


# --------------------------------------------------------------------------- #
# Meetup-specific and Luma-specific discovery
# --------------------------------------------------------------------------- #
def test_meetup_specific_discovery_contributes_results():
    source = _source_with_per_source(
        meetup_results=[_result("AWS Cloud Deep Dive", "https://www.meetup.com/a/1/")],
        luma_results=[],
    )
    out = source(["AWS"], *WINDOW)
    assert [e.source_platform for e in out] == ["Meetup"]
    assert out[0].event_url == "https://www.meetup.com/a/1/"


def test_luma_specific_discovery_contributes_results():
    source = _source_with_per_source(
        meetup_results=[],
        luma_results=[_result("AWS Bedrock Workshop", "https://lu.ma/austin-bedrock")],
    )
    out = source(["AWS"], *WINDOW)
    assert [e.source_platform for e in out] == ["Luma"]
    assert out[0].event_url == "https://lu.ma/austin-bedrock"


def test_mixed_source_aggregation_contains_both_platforms():
    source = _source_with_per_source(
        meetup_results=[
            _result("AWS Cloud Deep Dive", "https://www.meetup.com/a/1/", "2026-10-07"),
            _result("Kubernetes 101", "https://www.meetup.com/a/2/", "2026-10-08"),
        ],
        luma_results=[
            _result("Agentic AI Night", "https://lu.ma/agentic", "2026-10-09"),
        ],
    )
    out = source(["Agentic AI", "AWS", "Kubernetes"], *WINDOW)
    platforms = {e.source_platform for e in out}
    assert platforms == {"Meetup", "Luma"}
    assert len(out) == 3


# --------------------------------------------------------------------------- #
# Cross-source deduplication
# --------------------------------------------------------------------------- #
def test_cross_source_deduplication_collapses_same_event():
    """The same event surfaced on both platforms dedups to one record."""
    source = _source_with_per_source(
        meetup_results=[
            _result("AWS Cloud Deep Dive", "https://www.meetup.com/a/1/", "2026-10-07")
        ],
        luma_results=[
            _result("AWS Cloud Deep Dive", "https://lu.ma/austin-aws", "2026-10-07")
        ],
    )
    out = source(["AWS"], *WINDOW)
    assert len(out) == 1
    # The earliest-invocation source (Meetup) is retained; URL preserved byte-for-byte.
    assert out[0].event_url == "https://www.meetup.com/a/1/"


# --------------------------------------------------------------------------- #
# Top-N cap behavior (max 10; not a quota)
# --------------------------------------------------------------------------- #
def test_returns_at_most_ten_events():
    meetup = [
        _result(f"AWS Meetup Event {i}", f"https://www.meetup.com/a/{i}/", "2026-10-07")
        for i in range(8)
    ]
    luma = [
        _result(f"AWS Luma Event {i}", f"https://lu.ma/austin-{i}", "2026-10-08")
        for i in range(8)
    ]
    source = _source_with_per_source(meetup, luma)
    out = source(["AWS"], *WINDOW)
    # 16 relevant candidates exist, but the cap limits the result to 10.
    assert len(out) == 10


def test_returns_fewer_than_ten_when_insufficient_relevant_evidence():
    source = _source_with_per_source(
        meetup_results=[
            _result("AWS Cloud Deep Dive", "https://www.meetup.com/a/1/", "2026-10-07"),
            # Off-topic events must NOT be used to pad toward 10.
            _result("Hong Kong Mahjong", "https://www.meetup.com/a/9/", "2026-10-07"),
            _result("Board Game Night", "https://www.meetup.com/a/8/", "2026-10-07"),
        ],
        luma_results=[
            _result("AWS Bedrock Workshop", "https://lu.ma/austin-bedrock", "2026-10-08"),
        ],
    )
    out = source(["AWS"], *WINDOW)
    # Only the two on-topic events are returned — never padded with unrelated events.
    assert len(out) == 2
    titles = {e.title for e in out}
    assert titles == {"AWS Cloud Deep Dive", "AWS Bedrock Workshop"}


def test_zero_relevant_events_returns_empty():
    source = _source_with_per_source(
        meetup_results=[_result("Hong Kong Mahjong", "https://www.meetup.com/a/9/")],
        luma_results=[_result("Salsa Dancing Social", "https://lu.ma/salsa")],
    )
    out = source(["Kubernetes"], *WINDOW)
    assert out == []


def test_urls_preserved_byte_for_byte_across_sources():
    m_url = "https://www.meetup.com/austin-ai/events/301/?ref=home&x=1#frag"
    l_url = "https://lu.ma/austin-agentic?tk=AbC123"
    source = _source_with_per_source(
        meetup_results=[_result("Agentic AI Patterns", m_url, "2026-10-07")],
        luma_results=[_result("AI Agents Deep Dive", l_url, "2026-10-08")],
    )
    out = source(["Agentic AI"], *WINDOW)
    urls = {e.event_url for e in out}
    assert m_url in urls
    assert l_url in urls
