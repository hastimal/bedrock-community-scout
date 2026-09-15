"""
Event normalization utility for converting raw platform-specific event data
into the normalized CommonEvent model.

This module provides utilities for extracting and validating CommonEvent fields
from raw source platform data (e.g., JSON dicts from Meetup API, parsed HTML from
web search snippets, etc.).

The normalizer ensures:
- All required fields are present and non-empty
- Datetimes are converted to ISO 8601 UTC format
- Event URLs are preserved byte-for-byte from the source
- Optional fields are set to null when absent
- Skipped records (missing required fields) are logged with details
"""

import re
import logging
from typing import Optional
from datetime import datetime, timezone

from src.models.events import CommonEvent


# Configure logging for skipped event records
logger = logging.getLogger(__name__)


def normalize_event(
    raw_event: dict,
    source_platform: str,
    source_invocation_order: int = 0,
    match_confidence: float = 0.0,
    event_identifier: Optional[str] = None,
) -> Optional[CommonEvent]:
    """
    Normalize a raw platform-specific event dict into a CommonEvent.

    This function validates that all required CommonEvent fields are present in the
    raw event data. If any required field is missing or empty, the event is excluded
    and a log entry is created. Optional fields are set to null when absent.

    **Required fields in raw_event:**
    - title: Event title (string, non-empty)
    - start_datetime: Event start date and time (string, will be converted to ISO 8601 UTC)
    - city: City where event occurs (string, non-empty)
    - event_url: Source URL (string, non-empty; will be preserved byte-for-byte)

    **Optional fields in raw_event:**
    - description: Event description (string or null)
    - end_datetime: Event end date and time (string, ISO 8601 UTC format, or null)
    - location_name: Venue name or "Online" (string or null)

    **Datetime Conversion:**
    - If start_datetime is provided as a string (e.g., ISO 8601, Unix timestamp, or other format),
      it is converted to ISO 8601 UTC format (YYYY-MM-DDTHH:MM:SSZ).
    - If start_datetime is already in ISO 8601 UTC format, it is preserved.
    - If conversion fails, the event is excluded and logged.

    **URL Preservation:**
    - The event_url field is preserved exactly as provided in raw_event.
    - No truncation, encoding, or modification is applied.

    **Parameters:**
    - raw_event: Raw event dict from the source platform
    - source_platform: Platform name (e.g., "Meetup", "Luma"); used for CommonEvent.source_platform
    - source_invocation_order: 0-based index of the tool invocation; used for tie-breaking in deduplication
    - match_confidence: Confidence score [0.0, 1.0] from the source tool (default 0.0)
    - event_identifier: Optional event ID/name for logging (defaults to event title if available)

    **Returns:**
    - CommonEvent: Normalized event record if all required fields are present and valid
    - None: If any required field is missing, empty, or invalid; a log entry is created

    **Logging:**
    On exclusion, logs: "Skipped event from {platform}: missing/invalid {field_name}"
    Example: "Skipped event from Meetup (event_123): missing required field 'title'"
    """
    if not isinstance(raw_event, dict):
        logger.warning(f"Skipped event from {source_platform}: raw_event must be a dict, got {type(raw_event)}")
        return None

    # Extract event identifier for logging (prefer title, fall back to provided identifier)
    if event_identifier is None:
        event_identifier = raw_event.get("title", "unknown_event")

    # Helper function to validate required field
    def get_required_field(field_name: str) -> Optional[str]:
        value = raw_event.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            logger.warning(
                f"Skipped event from {source_platform} ({event_identifier}): "
                f"missing or empty required field '{field_name}'"
            )
            return None
        if not isinstance(value, str):
            logger.warning(
                f"Skipped event from {source_platform} ({event_identifier}): "
                f"field '{field_name}' must be a string, got {type(value)}"
            )
            return None
        return value.strip()

    # Helper function to validate optional field
    def get_optional_field(field_name: str) -> Optional[str]:
        value = raw_event.get(field_name)
        if value is None:
            return None
        if not isinstance(value, str):
            logger.warning(
                f"Skipped event from {source_platform} ({event_identifier}): "
                f"field '{field_name}' must be a string or null, got {type(value)}"
            )
            return None
        stripped = value.strip()
        return stripped if stripped else None

    # Extract and validate required fields
    title = get_required_field("title")
    if title is None:
        return None

    city = get_required_field("city")
    if city is None:
        return None

    event_url = get_required_field("event_url")
    if event_url is None:
        return None

    start_datetime_raw = get_required_field("start_datetime")
    if start_datetime_raw is None:
        return None

    # Convert start_datetime to ISO 8601 UTC format
    try:
        start_datetime = _convert_to_iso8601_utc(start_datetime_raw)
    except ValueError as e:
        logger.warning(
            f"Skipped event from {source_platform} ({event_identifier}): "
            f"failed to parse start_datetime '{start_datetime_raw}': {str(e)}"
        )
        return None

    # Extract and validate optional fields
    description = get_optional_field("description")
    location_name = get_optional_field("location_name")

    # Handle end_datetime (optional)
    end_datetime = None
    if "end_datetime" in raw_event and raw_event.get("end_datetime") is not None:
        end_datetime_raw = get_optional_field("end_datetime")
        if end_datetime_raw is not None:
            try:
                end_datetime = _convert_to_iso8601_utc(end_datetime_raw)
            except ValueError as e:
                logger.warning(
                    f"Skipped event from {source_platform} ({event_identifier}): "
                    f"failed to parse end_datetime '{end_datetime_raw}': {str(e)}"
                )
                return None

    # Construct and return CommonEvent
    try:
        event = CommonEvent(
            title=title,
            start_datetime=start_datetime,
            city=city,
            source_platform=source_platform,
            event_url=event_url,
            description=description,
            end_datetime=end_datetime,
            location_name=location_name,
            match_confidence=match_confidence,
            source_invocation_order=source_invocation_order,
        )
        return event
    except ValueError as e:
        logger.warning(
            f"Skipped event from {source_platform} ({event_identifier}): "
            f"failed to construct CommonEvent: {str(e)}"
        )
        return None


def _convert_to_iso8601_utc(datetime_str: str) -> str:
    """
    Convert a datetime string to ISO 8601 UTC format (YYYY-MM-DDTHH:MM:SSZ).

    Attempts to parse the input using several common formats and converts to UTC if needed.

    **Supported Input Formats:**
    - ISO 8601 UTC (already in correct format): "2025-09-15T18:00:00Z"
    - ISO 8601 with timezone offset: "2025-09-15T18:00:00-05:00"
    - ISO 8601 without timezone: "2025-09-15T18:00:00"
    - Date only: "2025-09-15" (assumes midnight UTC)
    - Full English month date only: "June 24, 2026" (assumes midnight UTC)
    - Common datetime formats

    **Returns:**
    - String in ISO 8601 UTC format: "YYYY-MM-DDTHH:MM:SSZ"

    **Raises:**
    - ValueError: If the input cannot be parsed or converted to UTC

    **Note:**
    If the input datetime is timezone-aware, it is converted to UTC.
    If the input is timezone-naive, it is assumed to be UTC.
    """
    if not isinstance(datetime_str, str):
        raise ValueError(f"datetime_str must be a string, got {type(datetime_str)}")

    datetime_str = datetime_str.strip()

    # If already in ISO 8601 UTC format, return as-is
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", datetime_str):
        return datetime_str

    # Try parsing with various formats
    formats_to_try = [
        "%Y-%m-%dT%H:%M:%SZ",           # ISO 8601 UTC
        "%Y-%m-%dT%H:%M:%S%z",         # ISO 8601 with timezone
        "%Y-%m-%dT%H:%M:%S",           # ISO 8601 without timezone (assume UTC)
        "%Y-%m-%d %H:%M:%S",           # Space-separated datetime (assume UTC)
        "%Y-%m-%d",                     # Date only (assume midnight UTC)
        "%B %d, %Y at %I:%M %p",       # Common format: "September 15, 2025 at 6:00 PM"
        "%B %d, %Y",                    # Full English month date only: "June 24, 2026" (midnight UTC)
    ]

    for fmt in formats_to_try:
        try:
            dt = datetime.strptime(datetime_str, fmt)
            # If timezone-naive, assume UTC
            if dt.tzinfo is None:
                return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                # Convert to UTC by using astimezone()
                dt_utc = dt.astimezone(timezone.utc)
                return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue

    # If no format matched, raise an error
    raise ValueError(
        f"Could not parse datetime string '{datetime_str}'. "
        f"Supported formats: ISO 8601 (with/without timezone), date only, common datetime formats."
    )
