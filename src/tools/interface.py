"""
Event Source Tool Protocol and Interface.

This module defines the uniform interface that all Event_Source_Tools must implement.
Each tool is responsible for fetching event data from a specific platform and returning
normalized CommonEvent records or a ToolError.

The EventSourceTool Protocol is designed for composability: multiple tools can be
registered with the Scout and invoked in parallel, with their results aggregated
and deduplicated.
"""

from typing import Protocol

from src.models.events import CommonEvent, ToolError


class EventSourceTool(Protocol):
    """
    Uniform interface for event source tools.

    All Event_Source_Tools conform to this protocol. Each tool encapsulates the logic
    for querying a specific platform (meetup.com, lu.ma, Eventbrite, etc.) and
    extracting normalized CommonEvent records.

    **Key Design Constraints:**
    - Location is fixed to Austin, Texas for v0.1 and is NOT a caller-supplied parameter.
      Tools internally assume all queries target Austin, Texas.
    - Each tool must complete within 10 seconds.
    - Tools return either a list of CommonEvent records (success) or a ToolError (failure).
      Never return partial results on failure.
    - All required CommonEvent fields must be populated; optional fields may be null.
    - The event_url field must be byte-for-byte identical to the original source URL.

    **Tool Responsibilities:**
    1. Accept topics (1–10, each 1–200 chars) and a time window (start_date, end_date).
    2. Construct a query targeting Austin, Texas and the specified topics.
    3. Fetch results from the source platform (using APIs, web search, etc.).
    4. Extract normalized CommonEvent records from the raw source data.
    5. Filter events by the specified time window (event start date must be in [start_date, end_date]).
    6. Return the normalized list or a ToolError on failure.

    **Extensibility:**
    To add a new event source (e.g., Eventbrite), implement a new tool class that
    satisfies this protocol. Register it with the Scout's Orchestrator, and it will
    be invoked alongside existing tools with no changes to other components.
    """

    def __call__(
        self,
        topics: list[str],  # 1–10 topics, each 1–200 characters
        start_date: str,    # ISO 8601 date string (YYYY-MM-DD)
        end_date: str,      # ISO 8601 date string (YYYY-MM-DD)
    ) -> list[CommonEvent] | ToolError:
        """
        Fetch and return events matching the given topics and time window.

        **Parameters:**
        - topics: List of 1–10 topic strings (each 1–200 chars). Examples: ["Agentic AI", "AWS"].
                 The tool searches for events matching any of these topics.
        - start_date: ISO 8601 date string (YYYY-MM-DD) marking the start of the time window (inclusive).
                     Example: "2025-08-01"
        - end_date: ISO 8601 date string (YYYY-MM-DD) marking the end of the time window (inclusive).
                   Example: "2025-10-30"

        **Location:** Austin, Texas (fixed for v0.1, not passed as a parameter).

        **Returns:**
        - List[CommonEvent]: Zero or more normalized event records matching the query and time window.
                            All required fields (title, start_datetime, city, source_platform, event_url)
                            must be non-empty. Optional fields (description, end_datetime, location_name)
                            may be null.
        - ToolError: Returned when the tool times out (>10 seconds) or encounters an error (HTTP error,
                    network failure, parsing failure, etc.). No partial results are returned on error.

        **Time Window Filtering:**
        The tool must filter returned events such that all event start dates (extracted from source data)
        fall within the inclusive range [start_date, end_date]. Events outside this range are excluded.

        **Factual Accuracy Constraint:**
        - The tool must NOT infer, fabricate, or substitute any event data.
        - All event fields must originate directly from the source platform.
        - If a required field cannot be reliably extracted, the event is excluded.

        **URL Preservation:**
        - The event_url field must be byte-for-byte identical to the original source URL.
        - No truncation, encoding, or modification is permitted.

        **Error Handling:**
        - On error or timeout, return a ToolError with a descriptive reason string.
        - Never return a mix of CommonEvent and ToolError records.
        - Do not raise exceptions; encode errors in the return value.

        **Performance:**
        - Tool must respond within 10 seconds.
        - The Scout may apply a timeout wrapper.
        """
        ...
