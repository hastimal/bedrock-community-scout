"""
Property-based tests for end-to-end Event_URL immutability through the pipeline.

Property 5: CommonEvent URL Immutability
Validates: Requirements 5.4, 8.5, 9.2

# Feature: community-scout-austin, Property 5: event_url is unchanged through the pipeline

This test suite validates that when a list of CommonEvent records is run through
deduplicate() and then rank(), every event_url in the output is byte-for-byte
identical to one of the original event_url values.

Deduplication may drop records (duplicates are removed), and ranking reorders
records, but neither step may truncate, re-encode, or otherwise modify the
event_url of any record that survives. Concretely, the multiset of output URLs
must be a sub-multiset of the input URLs, and each output URL must exactly (byte
for byte) match the original URL of the CommonEvent instance it came from.
"""

from collections import Counter

from hypothesis import given, settings, strategies as st

from src.models.events import CommonEvent, QueryContext, RankedEvent
from src.pipeline.deduplicator import deduplicate
from src.pipeline.ranker import rank


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
    """Generate non-empty strings that satisfy CommonEvent required-field invariants."""
    return draw(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_.,':;",
            min_size=1,
            max_size=max_size,
        ).filter(lambda s: s.strip() != "")
    )


@st.composite
def varied_event_url(draw) -> str:
    """
    Generate varied, well-formed event_url strings.

    Includes query strings, fragments, mixed case, and tracking parameters so we
    exercise URLs that a naive normalization step might be tempted to rewrite.
    """
    scheme = draw(st.sampled_from(["https", "http"]))
    domain = draw(st.sampled_from(["meetup.com", "lu.ma", "example.com", "events.example.org"]))
    path = draw(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/",
            min_size=1,
            max_size=80,
        )
    )
    # Optional query string.
    query = draw(
        st.one_of(
            st.none(),
            st.text(
                alphabet="abcdefghijklmnopqrstuvwxyz0123456789=&%-_",
                min_size=1,
                max_size=60,
            ),
        )
    )
    # Optional fragment.
    fragment = draw(
        st.one_of(
            st.none(),
            st.text(
                alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_",
                min_size=1,
                max_size=30,
            ),
        )
    )
    url = f"{scheme}://{domain}/{path}"
    if query:
        url += f"?{query}"
    if fragment:
        url += f"#{fragment}"
    return url


@st.composite
def common_event(draw) -> CommonEvent:
    """Generate a valid CommonEvent record satisfying all model invariants."""
    return CommonEvent(
        title=draw(valid_non_empty_string(max_size=200)),
        start_datetime=draw(valid_iso8601_utc_datetime()),
        city=draw(valid_non_empty_string(max_size=100)),
        source_platform=draw(st.sampled_from(["Meetup", "Luma", "WebSearch"])),
        event_url=draw(varied_event_url()),
        description=draw(st.one_of(st.none(), valid_non_empty_string(max_size=500))),
        end_datetime=draw(st.one_of(st.none(), valid_iso8601_utc_datetime())),
        location_name=draw(st.one_of(st.none(), valid_non_empty_string(max_size=100))),
        match_confidence=draw(st.floats(min_value=0.0, max_value=1.0)),
        source_invocation_order=draw(st.integers(min_value=0, max_value=20)),
    )


@st.composite
def query_context(draw) -> QueryContext:
    """Generate a valid QueryContext to drive rank()."""
    topics = draw(
        st.lists(valid_non_empty_string(max_size=50), min_size=1, max_size=5)
    )
    return QueryContext(
        topics=topics,
        location="Austin, Texas",
        start_date="2000-01-01",
        end_date="2100-12-31",
    )


@given(events=st.lists(common_event(), min_size=0, max_size=25), context=query_context())
@settings(max_examples=100)
def test_event_url_immutable_through_deduplicate_then_rank(
    events: list, context: QueryContext
):
    """
    Property 5: Running CommonEvent records through deduplicate() then rank()
    never changes any surviving event_url byte-for-byte.

    Validates: Requirements 5.4, 8.5, 9.2
    """
    # Capture the original URLs as a multiset before the pipeline runs.
    original_urls = Counter(event.event_url for event in events)

    # Run the pipeline: deduplicate then rank.
    deduped = deduplicate(events)

    # Deduplication must not modify or introduce URLs: its output URLs must be a
    # sub-multiset of the originals.
    deduped_urls = Counter(event.event_url for event in deduped)
    for url, count in deduped_urls.items():
        assert url in original_urls, (
            f"deduplicate() produced an event_url not present in the input: {url!r}"
        )
        assert count <= original_urls[url], (
            f"deduplicate() produced more copies of {url!r} than existed in the input"
        )

    ranked = rank(deduped, context)

    # rank() must return RankedEvent objects; the URL lives at .event.event_url.
    assert all(isinstance(r, RankedEvent) for r in ranked), (
        "rank() must return RankedEvent objects"
    )

    # ranking must not modify, add, or drop URLs relative to the deduped set.
    ranked_urls = Counter(r.event.event_url for r in ranked)
    assert ranked_urls == deduped_urls, (
        "rank() changed the multiset of event_url values"
    )

    # Every URL in the final output must be byte-for-byte identical to an
    # original URL, and must not appear more often than in the input.
    for url, count in ranked_urls.items():
        assert url in original_urls, (
            f"Pipeline output contains an event_url not present in the input: {url!r}"
        )
        assert count <= original_urls[url], (
            f"Pipeline output contains more copies of {url!r} than existed in the input"
        )

    # Confirm the ranked events are the exact same CommonEvent instances that
    # entered ranking (their event_url attribute is untouched, not merely equal).
    deduped_ids = {id(event) for event in deduped}
    for r in ranked:
        assert id(r.event) in deduped_ids, (
            "rank() wrapped a CommonEvent instance that did not come from its input"
        )
