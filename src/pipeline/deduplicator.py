"""
Event deduplication for the Community Scout pipeline.

This module provides a pure-Python deduplication step that detects duplicate
events across sources and retains a single, highest-confidence record per
logical event. It has no external dependencies.

Duplicate detection groups events by a normalized key derived from the event's
title, start date, and city:
- title and city are compared after stripping leading/trailing whitespace and
  lowercasing (case-insensitive comparison).
- start date uses only the date portion (``YYYY-MM-DD`` prefix) of the ISO 8601
  ``start_datetime`` field, ignoring the time component.

Within each group the record with the highest ``match_confidence`` is retained.
Ties are broken by the earliest tool invocation (lowest
``source_invocation_order``). Absent confidence is treated as 0 (the CommonEvent
model already defaults ``match_confidence`` to 0.0).

The retained records are returned unmodified — in particular ``event_url`` is
preserved byte-for-byte. Groups are emitted in the order in which they first
appear in the input, so the output ordering is deterministic.
"""

from src.models.events import CommonEvent


def deduplicate(events: list[CommonEvent]) -> list[CommonEvent]:
    """
    Remove duplicate events, retaining one record per unique event.

    Events are grouped by the normalized key
    ``(normalized_title, start_date, normalized_city)`` where ``title`` and
    ``city`` are stripped and lowercased, and ``start_date`` is the ``YYYY-MM-DD``
    prefix of ``start_datetime``. Within each group, the record with the highest
    ``match_confidence`` is retained; ties are broken by the lowest
    ``source_invocation_order`` (earliest-invoked tool).

    Retained records are returned unchanged (no fields are modified, and
    ``event_url`` is preserved byte-for-byte). Groups appear in the output in the
    order they first appear in the input, giving deterministic, stable ordering.

    Args:
        events: The list of CommonEvent records to deduplicate, potentially
            containing duplicates from multiple sources.

    Returns:
        A new list containing exactly one CommonEvent per unique group, ordered
        by first appearance of each group in the input.
    """
    # Maps the normalized group key to the index of its retained record in the
    # output list, so we can preserve first-appearance order while replacing a
    # retained record when a better candidate is found.
    group_index: dict[tuple[str, str, str], int] = {}
    retained: list[CommonEvent] = []

    for event in events:
        normalized_title = event.title.strip().lower()
        normalized_city = event.city.strip().lower()
        start_date = event.start_datetime[:10]  # YYYY-MM-DD prefix
        key = (normalized_title, start_date, normalized_city)

        if key not in group_index:
            group_index[key] = len(retained)
            retained.append(event)
            continue

        current = retained[group_index[key]]
        # Retain the higher-confidence record; on a tie keep the earliest-invoked
        # tool (lower source_invocation_order). The existing record is kept unless
        # the candidate strictly wins on this ordering.
        if _sort_key(event) < _sort_key(current):
            retained[group_index[key]] = event

    return retained


def _sort_key(event: CommonEvent) -> tuple[float, int]:
    """
    Return the ordering key for selecting a record within a duplicate group.

    Records are ranked by ``match_confidence`` descending, then
    ``source_invocation_order`` ascending. This is expressed as a tuple where
    smaller compares as "better": negated confidence puts the highest confidence
    first, and invocation order breaks ties toward the earliest-invoked tool.
    """
    return (-event.match_confidence, event.source_invocation_order)
