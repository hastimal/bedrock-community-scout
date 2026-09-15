"""
Event aggregation for the Community Scout pipeline.

This module provides a pure-Python aggregation step that runs after all
Event_Source_Tool invocations complete. Each tool invocation returns either a
list of ``CommonEvent`` records (on success) or a single ``ToolError`` (on
timeout or failure). The aggregator collects these mixed results into two
separate collections:

- A single flattened list of every ``CommonEvent`` returned by the successful
  tools.
- A separate list of every ``ToolError`` returned by the failed tools.

The flattened event list preserves ``source_invocation_order``: events are
emitted in the order their tool results appear in ``tool_results``, and within
each result in their original list order. No event fields are modified — in
particular ``event_url`` is preserved byte-for-byte — so the aggregated set is
exactly the union of all returned ``CommonEvent`` lists.

The ``ToolError`` entries are surfaced later in the response footer to inform
the user which sources could not be retrieved and why. This module has no
external dependencies and makes no LLM calls.
"""

from src.models.events import CommonEvent, ToolError


def aggregate(
    tool_results: list[list[CommonEvent] | ToolError],
) -> tuple[list[CommonEvent], list[ToolError]]:
    """
    Collect mixed Event_Source_Tool results into events and errors.

    Each entry in ``tool_results`` is either a list of ``CommonEvent`` records
    (a successful tool invocation) or a single ``ToolError`` (a failed
    invocation). This function flattens all ``CommonEvent`` lists into one list
    and collects all ``ToolError`` entries into a separate list.

    The flattened event list preserves ``source_invocation_order``: results are
    processed in the order they appear in ``tool_results``, and each list's
    events are appended in their original order. Events are returned unchanged
    (no fields are modified, and ``event_url`` is preserved byte-for-byte), so
    the aggregated event set is exactly the union of all input ``CommonEvent``
    lists.

    Args:
        tool_results: The results returned by each Event_Source_Tool invocation.
            Each element is either a ``list[CommonEvent]`` (success) or a
            ``ToolError`` (failure).

    Returns:
        A tuple ``(events, errors)`` where ``events`` is the flattened list of
        all ``CommonEvent`` records and ``errors`` is the list of all
        ``ToolError`` entries, each ordered by first appearance in
        ``tool_results``.
    """
    events: list[CommonEvent] = []
    errors: list[ToolError] = []

    for result in tool_results:
        if isinstance(result, ToolError):
            errors.append(result)
        else:
            events.extend(result)

    return events, errors
