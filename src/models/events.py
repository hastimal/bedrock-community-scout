"""
Event data models for the Community Scout application.

This module defines the normalized data structures used throughout the Scout pipeline:
- CommonEvent: the normalized event model used by all event sources
- RankedEvent: an event paired with its relevance score
- ToolError: error information from a failed event source tool
- QueryContext: the interpreted user query context

All models enforce strict type safety and required field validation per the design specification.
"""

from dataclasses import dataclass, field
from typing import Optional
import re


@dataclass
class CommonEvent:
    """
    Normalized event data structure conforming to the Common Event Model.

    This is the standard data model that all Event_Source_Tools must populate.
    It ensures consistent event representation across multiple sources (meetup.com, lu.ma, etc.)
    and enables deterministic deduplication and ranking.

    **Required fields** (must be non-empty strings):
    - title: Event title
    - start_datetime: Event start date and time in ISO 8601 UTC format (e.g., "2025-09-15T18:00:00Z")
    - city: City where the event takes place (e.g., "Austin, Texas" for v0.1)
    - source_platform: Derived from the source URL domain (e.g., "Meetup", "Luma")
    - event_url: Unmodified source URL from the originating Event_Source_Tool

    **Optional fields** (may be null):
    - description: Event description or null
    - end_datetime: Event end date and time in ISO 8601 UTC format or null
    - location_name: Venue name, "Online", or null

    **Internal fields**:
    - match_confidence: Confidence score [0.0–1.0] from the source tool (default 0.0)
    - source_invocation_order: 0-based index of tool invocation (for tie-breaking in deduplication)

    **Invariants**:
    - Required fields (title, start_datetime, city, source_platform, event_url) must be non-empty strings
    - start_datetime must match ISO 8601 UTC format: YYYY-MM-DDTHH:MM:SSZ
    - end_datetime, when non-null, must match the same ISO 8601 UTC format
    - match_confidence must be in [0.0, 1.0]
    - source_invocation_order must be >= 0
    """

    title: str
    start_datetime: str
    city: str
    source_platform: str
    event_url: str
    description: Optional[str] = None
    end_datetime: Optional[str] = None
    location_name: Optional[str] = None
    match_confidence: float = 0.0
    source_invocation_order: int = 0

    # ISO 8601 UTC datetime pattern: YYYY-MM-DDTHH:MM:SSZ
    _ISO_8601_UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def __post_init__(self):
        """
        Validate invariants on initialization.

        Raises:
            ValueError: If any invariant is violated (empty required field,
                       invalid datetime format, out-of-range confidence, etc.)
        """
        # Validate required fields are non-empty strings
        if not self.title or not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("title must be a non-empty string")
        if not self.city or not isinstance(self.city, str) or not self.city.strip():
            raise ValueError("city must be a non-empty string")
        if not self.source_platform or not isinstance(self.source_platform, str) or not self.source_platform.strip():
            raise ValueError("source_platform must be a non-empty string")
        if not self.event_url or not isinstance(self.event_url, str) or not self.event_url.strip():
            raise ValueError("event_url must be a non-empty string")

        # Validate start_datetime format (required, must be ISO 8601 UTC)
        if not isinstance(self.start_datetime, str) or not self._ISO_8601_UTC_PATTERN.match(self.start_datetime):
            raise ValueError(
                f"start_datetime must match ISO 8601 UTC format (YYYY-MM-DDTHH:MM:SSZ), got: {self.start_datetime}"
            )

        # Validate end_datetime format (optional, but if provided must be ISO 8601 UTC)
        if self.end_datetime is not None:
            if not isinstance(self.end_datetime, str) or not self._ISO_8601_UTC_PATTERN.match(self.end_datetime):
                raise ValueError(
                    f"end_datetime must match ISO 8601 UTC format (YYYY-MM-DDTHH:MM:SSZ), got: {self.end_datetime}"
                )

        # Validate match_confidence is in [0.0, 1.0]
        if not isinstance(self.match_confidence, (int, float)) or not (0.0 <= self.match_confidence <= 1.0):
            raise ValueError(f"match_confidence must be in [0.0, 1.0], got: {self.match_confidence}")

        # Validate source_invocation_order is non-negative integer
        if not isinstance(self.source_invocation_order, int) or self.source_invocation_order < 0:
            raise ValueError(f"source_invocation_order must be a non-negative integer, got: {self.source_invocation_order}")


@dataclass
class RankedEvent:
    """
    An event paired with its computed relevance score.

    Used after the Ranker component has evaluated all events and assigned scores.
    Enables the Response Generator to iterate through ranked results in order.

    Attributes:
        event: The CommonEvent being ranked
        relevance_score: Relevance score in [0.0, 100.0], computed by the Ranker
                        as: topic_score (0–80) + recency_score (0–20)
    """

    event: CommonEvent
    relevance_score: float

    def __post_init__(self):
        """
        Validate that relevance_score is in the valid range [0.0, 100.0].

        Raises:
            ValueError: If relevance_score is outside [0.0, 100.0]
        """
        if not isinstance(self.relevance_score, (int, float)):
            raise ValueError(f"relevance_score must be a number, got: {type(self.relevance_score)}")
        if not (0.0 <= self.relevance_score <= 100.0):
            raise ValueError(f"relevance_score must be in [0.0, 100.0], got: {self.relevance_score}")


@dataclass
class ToolError:
    """
    Error information returned by a failed Event_Source_Tool.

    When an Event_Source_Tool times out (>10 seconds) or encounters an error
    (HTTP error, network failure, parsing failure, etc.), it returns a ToolError
    instead of a list of CommonEvent records.

    The Scout collects ToolError entries and includes them in the response footer
    to inform the user which sources could not be retrieved and why.

    Attributes:
        tool_name: The name of the Event_Source_Tool that failed
                   (e.g., "AgentCoreWebSearchEventSource", "MeetupEventSource")
        reason: Human-readable description of the failure
                (e.g., "HTTP 503 from Web Search Tool", "Tool invocation timed out after 10 seconds")
    """

    tool_name: str
    reason: str

    def __post_init__(self):
        """
        Validate that tool_name and reason are non-empty strings.

        Raises:
            ValueError: If tool_name or reason is empty or not a string
        """
        if not self.tool_name or not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("tool_name must be a non-empty string")
        if not self.reason or not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")


@dataclass
class QueryContext:
    """
    The interpreted user query context extracted by the Query Interpreter.

    Represents the parsed intent of a user's natural-language query.
    Used by Event_Source_Tools to construct searches and by the Ranker
    to compute relevance scores.

    Attributes:
        topics: List of 1–10 extracted topics (each 1–200 characters)
                Examples: ["Agentic AI", "AWS", "Kubernetes"]
        location: Geographic location for the search (fixed to "Austin, Texas" for v0.1)
                  Not required in user input but always populated by the Query Interpreter
        start_date: ISO 8601 date string (YYYY-MM-DD) marking the start of the time window
                    Example: "2025-08-01"
        end_date: ISO 8601 date string (YYYY-MM-DD) marking the end of the time window (inclusive)
                  Example: "2025-10-30"
    """

    topics: list
    location: str
    start_date: str
    end_date: str

    def __post_init__(self):
        """
        Validate QueryContext invariants.

        Raises:
            ValueError: If topics list is empty, location is empty, dates are invalid, etc.
        """
        # Validate topics list
        if not isinstance(self.topics, list):
            raise ValueError(f"topics must be a list, got: {type(self.topics)}")
        if len(self.topics) == 0:
            raise ValueError("topics list must contain at least 1 topic")
        if len(self.topics) > 10:
            raise ValueError(f"topics list must contain at most 10 topics, got: {len(self.topics)}")

        # Validate each topic
        for i, topic in enumerate(self.topics):
            if not isinstance(topic, str):
                raise ValueError(f"topic at index {i} must be a string, got: {type(topic)}")
            if not topic or not topic.strip():
                raise ValueError(f"topic at index {i} must be non-empty")
            if len(topic) > 200:
                raise ValueError(f"topic at index {i} must be <= 200 characters, got: {len(topic)}")

        # Validate location
        if not self.location or not isinstance(self.location, str) or not self.location.strip():
            raise ValueError("location must be a non-empty string")
        if len(self.location) > 200:
            raise ValueError(f"location must be <= 200 characters, got: {len(self.location)}")

        # Validate date format (ISO 8601 YYYY-MM-DD)
        date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        if not isinstance(self.start_date, str) or not date_pattern.match(self.start_date):
            raise ValueError(f"start_date must be ISO 8601 date (YYYY-MM-DD), got: {self.start_date}")
        if not isinstance(self.end_date, str) or not date_pattern.match(self.end_date):
            raise ValueError(f"end_date must be ISO 8601 date (YYYY-MM-DD), got: {self.end_date}")

        # Validate that start_date <= end_date
        if self.start_date > self.end_date:
            raise ValueError(f"start_date must be <= end_date, got start_date={self.start_date}, end_date={self.end_date}")
