"""
Response generation for the Community Scout pipeline.

This module provides a pure-Python step that renders the final, user-facing
response from the ranked events, the interpreted query context, and any errors
reported by Event_Source_Tools. It has no external dependencies and makes no
LLM calls.

The response has three parts:

1. A header stating the total number of events returned (calculated after
   deduplication — i.e. the length of ``ranked_events``) for Austin, Texas.
2. A single ranked list rendering each event's display fields. The order is
   preserved exactly as produced by the Ranker; this module never re-sorts.
3. An optional footer listing any Event_Source_Tools that failed, with the
   reported reason, when ``errors`` is non-empty.

When ``ranked_events`` is empty, the module returns a "no events found" message
that identifies the requested topics, Austin, Texas, and the requested time
window, and suggests broadening the search criteria.

Per Requirement 8.5 (and Requirement 9.2), the ``event_url`` is rendered
byte-for-byte identical to the CommonEvent value — it is never altered,
truncated, encoded, or omitted.
"""

from src.models.events import QueryContext, RankedEvent, ToolError

# Placeholder rendered for any required display field that is absent or empty.
_MISSING = "N/A"


def format_response(
    ranked_events: list[RankedEvent],
    context: QueryContext,
    errors: list[ToolError],
) -> str:
    """
    Render the final user-facing response for a completed discovery session.

    The response begins with a header stating the total number of events
    returned (post-deduplication, i.e. ``len(ranked_events)``). Each event is
    then rendered sequentially in the exact order supplied by the Ranker — this
    function never re-sorts. For every event the following display fields are
    rendered: title, source platform name, city, start date formatted as
    ``YYYY-MM-DD`` (the date portion of the ISO 8601 UTC ``start_datetime``), and
    the ``event_url`` rendered as a markdown hyperlink ``[title](url)``.

    Any required display field that is absent or empty is rendered as ``"N/A"``.
    CommonEvent enforces non-empty required fields at construction, so this is a
    defensive fallback. The ``event_url`` is always emitted exactly as stored on
    the CommonEvent — it is never altered, truncated, encoded, or omitted.

    When ``errors`` is non-empty, a footer is appended listing each failed tool
    by ``tool_name`` and its ``reason``.

    When ``ranked_events`` is empty, a "no events found" message is returned that
    identifies the requested topics, Austin, Texas, and the requested time window
    (``context.start_date`` to ``context.end_date``), and suggests broadening the
    search criteria.

    Args:
        ranked_events: Events sorted by the Ranker (relevance descending). The
            order is preserved as-is; this function does not re-sort.
        context: The interpreted query context, used to describe the search when
            no events are found.
        errors: Errors reported by Event_Source_Tools. When non-empty, a failure
            footer is appended to the response.

    Returns:
        A markdown-formatted string containing the header, ranked event list, and
        (when applicable) the failure footer; or a "no events found" message when
        ``ranked_events`` is empty.
    """
    if not ranked_events:
        return _format_no_events(context, errors)

    lines: list[str] = []

    total = len(ranked_events)
    plural = "s" if total != 1 else ""
    lines.append(f"Found {total} event{plural} in Austin, Texas:")
    lines.append("")

    for index, ranked in enumerate(ranked_events, start=1):
        lines.extend(_format_event(index, ranked))
        lines.append("")

    footer = _format_error_footer(errors)
    if footer:
        lines.extend(footer)

    # Join and strip a single trailing newline for a clean result.
    return "\n".join(lines).rstrip("\n")


def _format_event(index: int, ranked: RankedEvent) -> list[str]:
    """
    Render a single ranked event as a list of markdown lines.

    Renders the title as a numbered markdown hyperlink to the event URL, followed
    by the source platform, city, and start date. Missing required display fields
    are rendered as ``"N/A"``. The ``event_url`` is emitted byte-for-byte.
    """
    event = ranked.event

    title = _display(getattr(event, "title", None))
    source_platform = _display(getattr(event, "source_platform", None))
    city = _display(getattr(event, "city", None))
    start_date = _format_start_date(getattr(event, "start_datetime", None))

    # The event_url must be preserved exactly. Do not strip, encode, or alter it.
    event_url = getattr(event, "event_url", None)

    if event_url:
        # Render the title as a markdown hyperlink: [title](url).
        heading = f"{index}. [{title}]({event_url})"
    else:
        # No URL to link to; render the title without a hyperlink.
        heading = f"{index}. {title}"

    return [
        heading,
        f"   - Source: {source_platform}",
        f"   - City: {city}",
        f"   - Date: {start_date}",
    ]


def _format_start_date(start_datetime) -> str:
    """
    Derive the ``YYYY-MM-DD`` start date from an ISO 8601 UTC datetime string.

    Takes the leading date portion (the first 10 characters) of the
    ``start_datetime`` string. Returns ``"N/A"`` when the value is absent, empty,
    or too short to contain a date.
    """
    if not start_datetime or not isinstance(start_datetime, str):
        return _MISSING

    date_part = start_datetime[:10]
    # A valid date portion is exactly YYYY-MM-DD (10 characters).
    if len(date_part) < 10:
        return _MISSING

    return date_part


def _format_error_footer(errors: list[ToolError]) -> list[str]:
    """
    Render the failure footer as a list of markdown lines.

    Returns an empty list when ``errors`` is empty. Otherwise lists each failed
    tool by name with its reported reason.
    """
    if not errors:
        return []

    lines = ["---", "Some sources could not be retrieved:"]
    for error in errors:
        tool_name = _display(getattr(error, "tool_name", None))
        reason = _display(getattr(error, "reason", None))
        lines.append(f"- {tool_name}: {reason}")

    return lines


def _format_no_events(context: QueryContext, errors: list[ToolError]) -> str:
    """
    Render the "no events found" message.

    Identifies the requested topics, Austin, Texas, and the requested time window
    (``context.start_date`` to ``context.end_date``), and suggests broadening the
    search criteria. Appends the failure footer when ``errors`` is non-empty.
    """
    topics = getattr(context, "topics", None) or []
    topics_text = ", ".join(str(topic) for topic in topics) if topics else _MISSING
    start_date = _display(getattr(context, "start_date", None))
    end_date = _display(getattr(context, "end_date", None))

    lines = [
        "Found 0 events in Austin, Texas.",
        "",
        (
            f"No events were found for the requested topics ({topics_text}) in "
            f"Austin, Texas between {start_date} and {end_date}."
        ),
        "Try broadening your search criteria — for example, use fewer or more "
        "general topics, or widen the time window.",
    ]

    footer = _format_error_footer(errors)
    if footer:
        lines.append("")
        lines.extend(footer)

    return "\n".join(lines).rstrip("\n")


def _display(value) -> str:
    """
    Return a display-safe string for a required field.

    Returns ``"N/A"`` when the value is ``None`` or an empty/whitespace-only
    string; otherwise returns the value coerced to ``str``.
    """
    if value is None:
        return _MISSING
    if isinstance(value, str) and not value.strip():
        return _MISSING
    return str(value)
