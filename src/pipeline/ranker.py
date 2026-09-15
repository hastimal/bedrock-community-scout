"""
Event ranking for the Community Scout pipeline.

This module provides a pure-Python ranking step that assigns each event a
deterministic ``relevance_score`` and returns the events sorted by relevance.
It has no external dependencies and makes no LLM calls.

The relevance score is a deterministic weighted sum::

    relevance_score = topic_score + recency_score

    topic_score   = (matched_topics / total_topics) * 80
    recency_score = max(0, 20 * (1 - days_until_event / 365))  # 0 if > 365 days away

Where:
- ``matched_topics`` is the number of distinct query topics that appear as a
  case-insensitive substring in the event title *or* description.
- ``total_topics`` is the number of topics in ``context.topics``.
- ``days_until_event`` is the number of calendar days between today (current
  date, UTC) and the event start date (the ``YYYY-MM-DD`` portion of
  ``start_datetime``). Events in the past yield a non-negative day count of 0 so
  recency stays within [0, 20].
- Location is not scored in v0.1 because every accepted event is already in
  Austin, Texas.

The final ``relevance_score`` is clamped to [0, 100]. When no topics were
extracted (``context.topics`` is empty), every event receives a score of 0.

Events are ordered by ``relevance_score`` descending, with ties broken by the
earliest ``start_datetime`` (ascending), giving deterministic, stable output.
"""

from datetime import date, datetime, timezone

from src.models.events import CommonEvent, QueryContext, RankedEvent

# Weight ceilings for the two scoring components.
_TOPIC_WEIGHT = 80.0
_RECENCY_WEIGHT = 20.0
_RECENCY_HORIZON_DAYS = 365


def rank(events: list[CommonEvent], context: QueryContext) -> list[RankedEvent]:
    """
    Score and sort events by relevance to the query context.

    Each event's ``relevance_score`` is computed as ``topic_score +
    recency_score`` and clamped to [0, 100]:

    - ``topic_score`` = ``(matched_topics / total_topics) * 80`` where
      ``matched_topics`` is the count of distinct query topics that appear as a
      case-insensitive substring in the event title or description, and
      ``total_topics`` is ``len(context.topics)``.
    - ``recency_score`` = ``max(0, 20 * (1 - days_until_event / 365))``, which is
      0 for events more than 365 days away. ``days_until_event`` is the number of
      calendar days between today (UTC) and the event's start date; past events
      are treated as 0 days away.

    Location is not a scoring component in v0.1. When ``context.topics`` is empty,
    every event receives a ``relevance_score`` of 0.

    The returned list is sorted by ``relevance_score`` descending, with ties
    broken by ascending ``start_datetime``.

    Args:
        events: The list of CommonEvent records to rank.
        context: The interpreted query context providing the topics to match.

    Returns:
        A new list of RankedEvent records sorted by relevance (descending) and
        then by start datetime (ascending) on ties.
    """
    today = datetime.now(timezone.utc).date()
    total_topics = len(context.topics)
    # Pre-lowercase topics once for case-insensitive substring matching.
    lowered_topics = [topic.lower() for topic in context.topics]

    ranked: list[RankedEvent] = []
    for event in events:
        if total_topics == 0:
            relevance_score = 0.0
        else:
            topic_score = _topic_score(event, lowered_topics, total_topics)
            recency_score = _recency_score(event, today)
            relevance_score = _clamp(topic_score + recency_score, 0.0, 100.0)

        ranked.append(RankedEvent(event=event, relevance_score=relevance_score))

    # Sort by score descending, then by start_datetime ascending on ties. The
    # start_datetime strings are ISO 8601 UTC, so lexicographic ordering matches
    # chronological ordering.
    ranked.sort(key=lambda r: (-r.relevance_score, r.event.start_datetime))
    return ranked


def _topic_score(event: CommonEvent, lowered_topics: list[str], total_topics: int) -> float:
    """
    Compute the topic-match component of the relevance score.

    A topic counts as matched if it appears as a case-insensitive substring in
    the event title or description. Returns ``(matched / total) * 80``.
    """
    haystack = event.title.lower()
    if event.description:
        haystack += "\n" + event.description.lower()

    matched_topics = sum(1 for topic in lowered_topics if topic in haystack)
    return (matched_topics / total_topics) * _TOPIC_WEIGHT


def _recency_score(event: CommonEvent, today: date) -> float:
    """
    Compute the recency component of the relevance score.

    ``recency_score = max(0, 20 * (1 - days_until_event / 365))``, which is 0 for
    events more than 365 days away. Events in the past are treated as 0 days
    away, keeping the result within [0, 20].
    """
    event_date = date.fromisoformat(event.start_datetime[:10])  # YYYY-MM-DD prefix
    days_until_event = (event_date - today).days
    # Past events are clamped to 0 days away so recency stays in [0, 20].
    if days_until_event < 0:
        days_until_event = 0

    return max(0.0, _RECENCY_WEIGHT * (1 - days_until_event / _RECENCY_HORIZON_DAYS))


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` to the closed interval ``[low, high]``."""
    return max(low, min(high, value))
