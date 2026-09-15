"""
Unit tests for the deterministic topic-relevance gate in AgentCoreWebSearchEventSource.

The gate runs BEFORE an extracted event is accepted as a CommonEvent. It requires the
event's OWN evidence (title + extracted description) to contain a credible keyword
signal for at least one REQUESTED topic. This removes false positives from generic
Austin Meetup discovery pages (e.g. "Hong Kong Mahjong",
"Religious Studies: Demon Hunters") that were returned only because the search term
appeared elsewhere on the page. It prefers precision over recall and never substitutes
unrelated events.

These tests exercise the gate directly (``_event_matches_requested_topics``) and, at a
higher level, through the event source's ``__call__`` extraction path with a mocked
Web Search response.
"""

from src.models.events import CommonEvent, ToolError
from src.tools.agentcore_web_search_event_source import AgentCoreWebSearchEventSource


def _event(title, description=None, url="https://www.meetup.com/austin/events/1/"):
    return CommonEvent(
        title=title,
        start_datetime="2026-10-07T18:30:00Z",
        city="Austin, Texas",
        source_platform="Meetup",
        event_url=url,
        description=description,
    )


def _gate(title, topics, description=None):
    source = AgentCoreWebSearchEventSource()
    return source._event_matches_requested_topics(_event(title, description), topics)


# --------------------------------------------------------------------------- #
# Rejections — the reported false positives
# --------------------------------------------------------------------------- #
def test_aws_rejects_religious_studies_demon_hunters():
    assert _gate("Religious Studies: Demon Hunters", ["AWS"]) is False


def test_aws_rejects_hong_kong_mahjong():
    assert _gate("Hong Kong Mahjong", ["AWS"]) is False


def test_kubernetes_rejects_unrelated_generic_austin_events():
    for title in [
        "Hong Kong Mahjong",
        "Austin Board Game Night",
        "Religious Studies: Demon Hunters",
        "Downtown Austin Walking Tour",
    ]:
        assert _gate(title, ["Kubernetes"]) is False, title


def test_topic_term_only_in_metadata_or_sibling_is_not_evidence():
    # The event's OWN title/description has no AWS signal; a sibling event or page
    # metadata mentioning AWS must NOT make this event pass.
    assert _gate("Hong Kong Mahjong", ["AWS"], description="A fun evening of tiles.") is False


# --------------------------------------------------------------------------- #
# Acceptances — AWS
# --------------------------------------------------------------------------- #
def test_aws_accepts_expected_signals():
    assert _gate("AWS Cloud Deep Dive", ["AWS"]) is True
    assert _gate("Intro to Amazon Web Services", ["AWS"]) is True
    assert _gate("Building with Bedrock", ["AWS"]) is True
    assert _gate("Hands-on AgentCore Workshop", ["AWS"]) is True


# --------------------------------------------------------------------------- #
# Acceptances — Kubernetes
# --------------------------------------------------------------------------- #
def test_kubernetes_accepts_expected_signals():
    assert _gate("Kubernetes 101", ["Kubernetes"]) is True
    assert _gate("KubeCon Recap Meetup", ["Kubernetes"]) is True
    assert _gate("KubeVirt Deep Dive", ["Kubernetes"]) is True


# --------------------------------------------------------------------------- #
# Acceptances — Agentic AI
# --------------------------------------------------------------------------- #
def test_agentic_ai_accepts_expected_signals():
    assert _gate("Agentic AI Patterns", ["Agentic AI"]) is True
    assert _gate("Building AI Agents", ["Agentic AI"]) is True
    assert _gate("Multi-Agent Systems Workshop", ["Agentic AI"]) is True


# --------------------------------------------------------------------------- #
# Word-boundary safety (short acronyms must not match inside unrelated words)
# --------------------------------------------------------------------------- #
def test_word_boundary_prevents_substring_false_positives():
    # "aws" inside "flaws"/"Lawson" must not match AWS.
    assert _gate("Design flaws in distributed systems", ["AWS"]) is False
    assert _gate("A talk by Lawson", ["AWS"]) is False


# --------------------------------------------------------------------------- #
# Multi-topic query behavior
# --------------------------------------------------------------------------- #
def test_multi_topic_query_accepts_event_matching_any_one_topic():
    topics = ["Agentic AI", "AWS", "Kubernetes"]
    assert _gate("Kubernetes 101", topics) is True
    assert _gate("AWS Cloud Deep Dive", topics) is True
    assert _gate("Agentic AI Patterns", topics) is True


def test_multi_topic_query_rejects_event_matching_no_topic():
    topics = ["Agentic AI", "AWS", "Kubernetes"]
    assert _gate("Hong Kong Mahjong", topics) is False
    assert _gate("Religious Studies: Demon Hunters", topics) is False


def test_description_evidence_can_satisfy_the_gate():
    # Topic signal in the extracted description (not just the title) is accepted.
    assert _gate(
        "Community Tech Night",
        ["Kubernetes"],
        description="This month we cover Kubernetes and Helm on EKS.",
    ) is True


# --------------------------------------------------------------------------- #
# End-to-end through the extraction path (mocked Web Search)
# --------------------------------------------------------------------------- #
def _source_with_mocked_results(raw_results):
    source = AgentCoreWebSearchEventSource()
    source._invoke_web_search = lambda t, s, e: raw_results  # type: ignore[assignment]
    return source


def test_extraction_path_filters_offtopic_false_positives():
    """A discovery-page result set: only the on-topic event survives the gate."""
    raw_results = [
        {
            "title": "Hong Kong Mahjong",
            "url": "https://www.meetup.com/austin-mahjong/events/11/",
            "text": "Join us in Austin, Texas on 2026-10-07T18:30:00Z for mahjong.",
        },
        {
            "title": "Religious Studies: Demon Hunters",
            "url": "https://www.meetup.com/austin-rel/events/12/",
            "text": "Austin, Texas on 2026-10-08T18:30:00Z — a discussion group.",
        },
        {
            "title": "AWS Cloud Deep Dive",
            "url": "https://www.meetup.com/austin-cloud/events/13/",
            "text": "Austin, Texas on 2026-10-09T18:30:00Z covering AWS and Bedrock.",
        },
    ]
    source = _source_with_mocked_results(raw_results)

    result = source(["AWS"], "2026-09-15", "2026-12-14")

    assert isinstance(result, list)
    titles = [e.title for e in result]
    assert titles == ["AWS Cloud Deep Dive"]
    assert "Hong Kong Mahjong" not in titles
    assert "Religious Studies: Demon Hunters" not in titles


def test_extraction_path_returns_zero_when_no_relevant_event():
    """If nothing matches the requested topic, the source returns zero events."""
    raw_results = [
        {
            "title": "Hong Kong Mahjong",
            "url": "https://www.meetup.com/austin-mahjong/events/11/",
            "text": "Join us in Austin, Texas on 2026-10-07T18:30:00Z for mahjong.",
        },
        {
            "title": "Austin Board Game Night",
            "url": "https://www.meetup.com/austin-games/events/12/",
            "text": "Austin, Texas on 2026-10-08T18:30:00Z for board games.",
        },
    ]
    source = _source_with_mocked_results(raw_results)

    result = source(["Kubernetes"], "2026-09-15", "2026-12-14")

    assert result == []
