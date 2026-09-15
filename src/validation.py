"""
Query validation helpers for the Community Scout application.

This module provides deterministic guardrails that run before the LLM-driven
topic/time-window extraction and before any Event_Source_Tool is invoked. These
checks enforce the query-validation rules from the design specification:

    | Condition                     | Behavior                                          |
    |-------------------------------|---------------------------------------------------|
    | Empty or whitespace-only query| Return error before any extraction or tool call   |
    | Cannot extract topic          | Return clarification message asking for a topic   |
    | More than 10 topics           | Reject with error before invoking any tool        |
    | Time window > 5 years         | Reject range; use default 90-day window, inform    |

The helpers are intentionally deterministic and side-effect-light so they can be
called safely from the Scout orchestrator prior to spending tokens or invoking
tools. Informational fallbacks (e.g., time-window clamping) are surfaced via the
module logger so callers keep a stable return signature.

Requirements: 1.5, 1.6
"""

import re
import logging
from datetime import datetime, timedelta, timezone

# Configure logging for validation decisions and informational fallbacks
logger = logging.getLogger(__name__)


# Maximum number of Topics a single Query may contain (design: reject > 10).
MAX_TOPICS = 10

# Default Time_Window length in calendar days when none is provided or when the
# requested window is rejected (design/Requirement 1.4).
DEFAULT_WINDOW_DAYS = 90

# Maximum allowed Time_Window span from the current date (design/Requirement 1.3).
MAX_WINDOW_DAYS = 5 * 365

# ISO 8601 date pattern used across the project: YYYY-MM-DD
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Splits candidate topics on commas or a conjunction ("and"/"&") surrounded by
# whitespace. Used only to count *candidate* topics deterministically; the LLM
# performs the authoritative extraction downstream.
_TOPIC_SPLIT_PATTERN = re.compile(r"\s*(?:,|\band\b|&)\s*", re.IGNORECASE)


def validate_query(raw: str) -> None | str:
    """
    Validate a raw natural-language Query before extraction or tool invocation.

    This performs the deterministic guardrails that must run before any LLM
    extraction or Event_Source_Tool call:

    - Empty or whitespace-only input is rejected with an error message
      (Requirement 1.6).
    - Input from which no candidate Topic token can be derived is rejected with a
      clarification message identifying that a topic is needed (Requirement 1.5).
    - Input containing more than ``MAX_TOPICS`` candidate Topics is rejected with
      an error message indicating the exceeded limit (Requirement 11.5).

    Candidate Topics are counted deterministically by splitting on commas and the
    conjunctions "and"/"&". This is a conservative guard, not the authoritative
    topic extraction, which is performed by the LLM downstream.

    Parameters:
        raw: The raw query string submitted by the User.

    Returns:
        None: When the query passes all deterministic guardrails and may proceed
            to extraction.
        str: A human-readable error or clarification message when the query is
            invalid. The caller should return this message to the User without
            invoking any Event_Source_Tool.
    """
    # Requirement 1.6: empty or whitespace-only query -> error before extraction.
    if raw is None or not isinstance(raw, str) or not raw.strip():
        logger.info("Rejected query: empty or whitespace-only input")
        return (
            "Your query appears to be empty. Please enter a query describing the "
            "technology topics and (optionally) the time window you're interested in."
        )

    stripped = raw.strip()

    # Requirement 1.5: no extractable Topic -> clarification message.
    candidate_topics = [
        token.strip()
        for token in _TOPIC_SPLIT_PATTERN.split(stripped)
        if token.strip()
    ]
    if not candidate_topics:
        logger.info("Rejected query: no extractable topic tokens")
        return (
            "I couldn't identify a topic in your query. Please specify at least one "
            "technology topic of interest (for example: \"Agentic AI\" or "
            "\"Kubernetes\")."
        )

    # Requirement 11.5: more than MAX_TOPICS topics -> reject before any tool call.
    if len(candidate_topics) > MAX_TOPICS:
        logger.info(
            "Rejected query: %d candidate topics exceed the limit of %d",
            len(candidate_topics),
            MAX_TOPICS,
        )
        return (
            f"Your query contains {len(candidate_topics)} topics, which exceeds the "
            f"maximum of {MAX_TOPICS}. Please narrow your search to at most "
            f"{MAX_TOPICS} topics and try again."
        )

    # Query passes all deterministic guardrails.
    return None


def validate_time_window(start: str, end: str) -> tuple[str, str]:
    """
    Validate and, if necessary, clamp a requested Time_Window.

    The requested window is provided as ISO 8601 date strings (YYYY-MM-DD). If the
    span between ``start`` and ``end`` exceeds ``MAX_WINDOW_DAYS`` (5 years), the
    requested range is rejected and the function falls back to the default
    ``DEFAULT_WINDOW_DAYS`` (90-day) window starting from the current UTC date. The
    fallback is recorded deterministically via the module logger so the return
    signature can remain a plain ``tuple[str, str]``.

    Invalid or unparseable inputs (missing values, wrong format, or an inverted
    range where ``start`` is after ``end``) are also treated as a rejection and
    trigger the same default-window fallback.

    Parameters:
        start: Requested window start as an ISO 8601 date string (YYYY-MM-DD).
        end: Requested window end as an ISO 8601 date string (YYYY-MM-DD).

    Returns:
        tuple[str, str]: The validated ``(start_date, end_date)`` ISO 8601 date
            strings. This is either the original window (when valid and within the
            5-year limit) or the default 90-day window starting today (when the
            requested window is rejected).
    """
    default_window = _default_window()

    # Validate presence and format of both bounds.
    if not _is_iso_date(start) or not _is_iso_date(end):
        logger.info(
            "Time window rejected (invalid or missing dates: start=%r, end=%r); "
            "falling back to default %d-day window %s",
            start,
            end,
            DEFAULT_WINDOW_DAYS,
            default_window,
        )
        return default_window

    start_date = datetime.strptime(start, "%Y-%m-%d").date()
    end_date = datetime.strptime(end, "%Y-%m-%d").date()

    # An inverted range is not a valid window.
    if start_date > end_date:
        logger.info(
            "Time window rejected (start %s is after end %s); falling back to "
            "default %d-day window %s",
            start,
            end,
            DEFAULT_WINDOW_DAYS,
            default_window,
        )
        return default_window

    # Reject windows exceeding the 5-year limit (Requirement 1.3).
    span_days = (end_date - start_date).days
    if span_days > MAX_WINDOW_DAYS:
        logger.info(
            "Time window rejected (span of %d days exceeds the %d-day / 5-year "
            "maximum); falling back to default %d-day window %s",
            span_days,
            MAX_WINDOW_DAYS,
            DEFAULT_WINDOW_DAYS,
            default_window,
        )
        return default_window

    # The requested window is valid and within limits; return it unchanged.
    return start, end


def _default_window() -> tuple[str, str]:
    """
    Build the default Time_Window: 90 calendar days starting from today (UTC).

    Returns:
        tuple[str, str]: ``(start_date, end_date)`` ISO 8601 date strings where
            ``start_date`` is the current UTC date and ``end_date`` is
            ``DEFAULT_WINDOW_DAYS`` days later.
    """
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=DEFAULT_WINDOW_DAYS)
    return today.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _is_iso_date(value: str) -> bool:
    """
    Return True if ``value`` is a valid ISO 8601 date string (YYYY-MM-DD).

    Both the surface format and the calendar validity are checked (e.g.,
    "2025-02-30" is rejected even though it matches the pattern).
    """
    if not isinstance(value, str) or not _ISO_DATE_PATTERN.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True
