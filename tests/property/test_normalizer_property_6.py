"""
Property-based tests for the normalize_event() function.

Property 6: Normalization Preserves and Validates Required Fields
Validates: Requirements 5.1, 5.2, 5.5

# Feature: community-scout-austin, Property 6: normalization preserves required fields and excludes incomplete records

This test suite validates that:
(a) When a raw event contains all required fields, normalize_event() produces a CommonEvent
    with all required fields non-empty and non-null.
(b) When a raw event is missing any required field, normalize_event() excludes it from output
    (returns None).
"""

import pytest
from hypothesis import given, strategies as st, settings, HealthCheck
from datetime import datetime, timedelta
import logging

from src.tools.normalizer import normalize_event
from src.models.events import CommonEvent


# Configure logging to capture warning messages during tests
logging.basicConfig(level=logging.WARNING)


@st.composite
def valid_iso8601_utc_datetime(draw) -> str:
    """Generate valid ISO 8601 UTC datetime strings."""
    year = draw(st.integers(min_value=2000, max_value=2100))
    month = draw(st.integers(min_value=1, max_value=12))
    day = draw(st.integers(min_value=1, max_value=28))  # Use 28 to avoid month edge cases
    hour = draw(st.integers(min_value=0, max_value=23))
    minute = draw(st.integers(min_value=0, max_value=59))
    second = draw(st.integers(min_value=0, max_value=59))
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"


@st.composite
def valid_non_empty_string(draw, max_size: int = 1000) -> str:
    """Generate valid non-empty strings (without control characters)."""
    # Use only printable characters to avoid issues with \r, \n, etc.
    return draw(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_.,':;",
            min_size=1,
            max_size=max_size,
        ).filter(lambda x: x.strip() != "")
    )


@st.composite
def valid_url(draw) -> str:
    """Generate valid URLs."""
    domain = draw(st.sampled_from(["meetup.com", "lu.ma", "example.com"]))
    path = draw(st.text(min_size=1, max_size=100, alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_/"))
    return f"https://{domain}/{path}"


@st.composite
def valid_raw_event(draw) -> dict:
    """Generate a valid raw event dict with all required fields."""
    return {
        "title": draw(valid_non_empty_string(max_size=500)),
        "start_datetime": draw(valid_iso8601_utc_datetime()),
        "city": draw(valid_non_empty_string(max_size=200)),
        "event_url": draw(valid_url()),
        "description": draw(st.one_of(st.none(), valid_non_empty_string(max_size=2000))),
        "end_datetime": draw(st.one_of(st.none(), valid_iso8601_utc_datetime())),
        "location_name": draw(st.one_of(st.none(), valid_non_empty_string(max_size=500))),
    }


class TestNormalizationPreservesRequiredFields:
    """
    Test that normalization preserves and validates required fields.
    """

    @given(valid_raw_event())
    @settings(max_examples=100, suppress_health_check=[HealthCheck.filter_too_much])
    def test_all_required_fields_present_produces_valid_common_event(self, raw_event: dict):
        """
        Property 6a: When all required fields are present, normalize_event() produces
        a CommonEvent with all required fields non-empty and non-null.
        """
        result = normalize_event(
            raw_event=raw_event,
            source_platform="TestPlatform",
            source_invocation_order=0,
            match_confidence=0.5,
        )

        # Result should not be None
        assert result is not None, "normalize_event() should return a CommonEvent when all required fields are present"

        # Result should be a CommonEvent instance
        assert isinstance(result, CommonEvent), f"Expected CommonEvent, got {type(result)}"

        # All required fields must be non-empty and non-null
        assert result.title and isinstance(result.title, str), "title must be non-empty string"
        assert result.start_datetime and isinstance(result.start_datetime, str), "start_datetime must be non-empty string"
        assert result.city and isinstance(result.city, str), "city must be non-empty string"
        assert result.source_platform and isinstance(result.source_platform, str), "source_platform must be non-empty string"
        assert result.event_url and isinstance(result.event_url, str), "event_url must be non-empty string"

        # Verify that required fields match the input (or are deterministically normalized)
        # Note: title and city may have leading/trailing whitespace stripped (deterministic normalization)
        assert result.title == raw_event["title"].strip(), "title must be preserved or whitespace-normalized"
        assert result.city == raw_event["city"].strip(), "city must be preserved or whitespace-normalized"
        # event_url must be preserved byte-for-byte
        assert result.event_url == raw_event["event_url"], "event_url must be preserved byte-for-byte"
        assert result.source_platform == "TestPlatform", "source_platform must match the input parameter"

        # start_datetime must be in ISO 8601 UTC format (already valid since input is ISO 8601 UTC)
        import re
        iso8601_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        assert iso8601_pattern.match(result.start_datetime), f"start_datetime must be ISO 8601 UTC format, got {result.start_datetime}"

        # Optional fields should be preserved or set to None
        if raw_event.get("description"):
            # Description may be whitespace-normalized
            assert result.description == raw_event["description"].strip(), "description must be preserved or whitespace-normalized"
        else:
            assert result.description is None, "description must be None when not present"

    @given(valid_raw_event())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.filter_too_much])
    def test_missing_title_excludes_event(self, raw_event: dict):
        """
        Property 6b: When title is missing, normalize_event() excludes the event (returns None).
        """
        raw_event_missing_title = raw_event.copy()
        del raw_event_missing_title["title"]

        result = normalize_event(
            raw_event=raw_event_missing_title,
            source_platform="TestPlatform",
            source_invocation_order=0,
        )

        assert result is None, "normalize_event() should return None when title is missing"

    @given(valid_raw_event())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.filter_too_much])
    def test_missing_start_datetime_excludes_event(self, raw_event: dict):
        """
        Property 6b: When start_datetime is missing, normalize_event() excludes the event (returns None).
        """
        raw_event_missing_start = raw_event.copy()
        del raw_event_missing_start["start_datetime"]

        result = normalize_event(
            raw_event=raw_event_missing_start,
            source_platform="TestPlatform",
            source_invocation_order=0,
        )

        assert result is None, "normalize_event() should return None when start_datetime is missing"

    @given(valid_raw_event())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.filter_too_much])
    def test_missing_city_excludes_event(self, raw_event: dict):
        """
        Property 6b: When city is missing, normalize_event() excludes the event (returns None).
        """
        raw_event_missing_city = raw_event.copy()
        del raw_event_missing_city["city"]

        result = normalize_event(
            raw_event=raw_event_missing_city,
            source_platform="TestPlatform",
            source_invocation_order=0,
        )

        assert result is None, "normalize_event() should return None when city is missing"

    @given(valid_raw_event())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.filter_too_much])
    def test_missing_event_url_excludes_event(self, raw_event: dict):
        """
        Property 6b: When event_url is missing, normalize_event() excludes the event (returns None).
        """
        raw_event_missing_url = raw_event.copy()
        del raw_event_missing_url["event_url"]

        result = normalize_event(
            raw_event=raw_event_missing_url,
            source_platform="TestPlatform",
            source_invocation_order=0,
        )

        assert result is None, "normalize_event() should return None when event_url is missing"

    @given(valid_raw_event())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.filter_too_much])
    def test_empty_title_excludes_event(self, raw_event: dict):
        """
        Property 6b: When title is empty or whitespace-only, normalize_event() excludes the event.
        """
        raw_event_empty_title = raw_event.copy()
        raw_event_empty_title["title"] = "   "  # Whitespace-only

        result = normalize_event(
            raw_event=raw_event_empty_title,
            source_platform="TestPlatform",
            source_invocation_order=0,
        )

        assert result is None, "normalize_event() should return None when title is empty/whitespace-only"

    @given(valid_raw_event())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.filter_too_much])
    def test_empty_city_excludes_event(self, raw_event: dict):
        """
        Property 6b: When city is empty or whitespace-only, normalize_event() excludes the event.
        """
        raw_event_empty_city = raw_event.copy()
        raw_event_empty_city["city"] = ""

        result = normalize_event(
            raw_event=raw_event_empty_city,
            source_platform="TestPlatform",
            source_invocation_order=0,
        )

        assert result is None, "normalize_event() should return None when city is empty"

    @given(valid_raw_event())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.filter_too_much])
    def test_empty_event_url_excludes_event(self, raw_event: dict):
        """
        Property 6b: When event_url is empty or whitespace-only, normalize_event() excludes the event.
        """
        raw_event_empty_url = raw_event.copy()
        raw_event_empty_url["event_url"] = ""

        result = normalize_event(
            raw_event=raw_event_empty_url,
            source_platform="TestPlatform",
            source_invocation_order=0,
        )

        assert result is None, "normalize_event() should return None when event_url is empty"

    @given(valid_raw_event())
    @settings(max_examples=50, suppress_health_check=[HealthCheck.filter_too_much])
    def test_optional_fields_set_to_none_when_absent(self, raw_event: dict):
        """
        Property 6: When optional fields are absent, they are set to None in the CommonEvent.
        """
        # Remove optional fields
        raw_event_no_optional = {
            "title": raw_event["title"],
            "start_datetime": raw_event["start_datetime"],
            "city": raw_event["city"],
            "event_url": raw_event["event_url"],
        }

        result = normalize_event(
            raw_event=raw_event_no_optional,
            source_platform="TestPlatform",
            source_invocation_order=0,
        )

        assert result is not None, "normalize_event() should return a CommonEvent when all required fields are present"
        assert result.description is None, "description should be None when not present"
        assert result.end_datetime is None, "end_datetime should be None when not present"
        assert result.location_name is None, "location_name should be None when not present"

    def test_url_immutability_preservation(self):
        """
        Property 6: The event_url field must be preserved byte-for-byte unchanged.
        """
        raw_event = {
            "title": "Test Event",
            "start_datetime": "2025-09-15T18:00:00Z",
            "city": "Austin, Texas",
            "event_url": "https://meetup.com/events/12345?utm_source=scout&param=value",
        }

        result = normalize_event(
            raw_event=raw_event,
            source_platform="Meetup",
            source_invocation_order=0,
        )

        assert result is not None
        # URL must be byte-for-byte identical
        assert result.event_url == "https://meetup.com/events/12345?utm_source=scout&param=value"
        assert result.event_url == raw_event["event_url"]

    def test_source_platform_preserved(self):
        """
        Property 6: The source_platform parameter must be accurately recorded in the output.
        """
        raw_event = {
            "title": "Test Event",
            "start_datetime": "2025-09-15T18:00:00Z",
            "city": "Austin, Texas",
            "event_url": "https://example.com/event/123",
        }

        # Test with different source platforms
        for platform in ["Meetup", "Luma", "Eventbrite", "Custom"]:
            result = normalize_event(
                raw_event=raw_event,
                source_platform=platform,
                source_invocation_order=0,
            )

            assert result is not None
            assert result.source_platform == platform, f"source_platform should be {platform}"
