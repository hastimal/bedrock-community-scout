"""
Unit tests for the normalizer module.

Tests focus on:
- Successful normalization of complete events
- Handling of missing required fields
- Handling of optional fields
- Datetime format conversion
- URL preservation
- Logging of skipped records
"""

import pytest
import logging
from src.tools.normalizer import normalize_event, _convert_to_iso8601_utc
from src.models.events import CommonEvent


class TestNormalizeEvent:
    """Tests for the normalize_event() function."""

    def test_normalize_valid_event_with_all_fields(self):
        """Test normalizing a complete event with all fields."""
        raw_event = {
            "title": "Agentic AI Workshop",
            "start_datetime": "2025-09-15T18:00:00Z",
            "city": "Austin, Texas",
            "event_url": "https://www.meetup.com/events/123456",
            "description": "Learn about agentic AI",
            "end_datetime": "2025-09-15T20:00:00Z",
            "location_name": "Austin Tech Hub",
        }
        event = normalize_event(raw_event, "Meetup", source_invocation_order=0, match_confidence=0.95)
        
        assert event is not None
        assert isinstance(event, CommonEvent)
        assert event.title == "Agentic AI Workshop"
        assert event.start_datetime == "2025-09-15T18:00:00Z"
        assert event.city == "Austin, Texas"
        assert event.source_platform == "Meetup"
        assert event.event_url == "https://www.meetup.com/events/123456"
        assert event.description == "Learn about agentic AI"
        assert event.end_datetime == "2025-09-15T20:00:00Z"
        assert event.location_name == "Austin Tech Hub"
        assert event.match_confidence == 0.95
        assert event.source_invocation_order == 0

    def test_normalize_valid_event_with_required_fields_only(self):
        """Test normalizing an event with only required fields."""
        raw_event = {
            "title": "AWS Deep Dive",
            "start_datetime": "2025-10-20T19:00:00Z",
            "city": "Austin, Texas",
            "event_url": "https://lu.ma/events/evt-123",
        }
        event = normalize_event(raw_event, "Luma")
        
        assert event is not None
        assert event.title == "AWS Deep Dive"
        assert event.start_datetime == "2025-10-20T19:00:00Z"
        assert event.city == "Austin, Texas"
        assert event.source_platform == "Luma"
        assert event.event_url == "https://lu.ma/events/evt-123"
        assert event.description is None
        assert event.end_datetime is None
        assert event.location_name is None

    def test_normalize_missing_title(self, caplog):
        """Test that event is excluded when title is missing."""
        raw_event = {
            "start_datetime": "2025-09-15T18:00:00Z",
            "city": "Austin, Texas",
            "event_url": "https://www.meetup.com/events/123456",
        }
        with caplog.at_level(logging.WARNING):
            event = normalize_event(raw_event, "Meetup")
        
        assert event is None
        assert "missing or empty required field 'title'" in caplog.text

    def test_normalize_empty_title(self, caplog):
        """Test that event is excluded when title is empty string."""
        raw_event = {
            "title": "",
            "start_datetime": "2025-09-15T18:00:00Z",
            "city": "Austin, Texas",
            "event_url": "https://www.meetup.com/events/123456",
        }
        with caplog.at_level(logging.WARNING):
            event = normalize_event(raw_event, "Meetup")
        
        assert event is None
        assert "missing or empty required field 'title'" in caplog.text

    def test_normalize_whitespace_only_title(self, caplog):
        """Test that event is excluded when title is whitespace-only."""
        raw_event = {
            "title": "   ",
            "start_datetime": "2025-09-15T18:00:00Z",
            "city": "Austin, Texas",
            "event_url": "https://www.meetup.com/events/123456",
        }
        with caplog.at_level(logging.WARNING):
            event = normalize_event(raw_event, "Meetup")
        
        assert event is None
        assert "missing or empty required field 'title'" in caplog.text

    def test_normalize_missing_city(self, caplog):
        """Test that event is excluded when city is missing."""
        raw_event = {
            "title": "Event",
            "start_datetime": "2025-09-15T18:00:00Z",
            "event_url": "https://www.meetup.com/events/123456",
        }
        with caplog.at_level(logging.WARNING):
            event = normalize_event(raw_event, "Meetup")
        
        assert event is None
        assert "missing or empty required field 'city'" in caplog.text

    def test_normalize_missing_event_url(self, caplog):
        """Test that event is excluded when event_url is missing."""
        raw_event = {
            "title": "Event",
            "start_datetime": "2025-09-15T18:00:00Z",
            "city": "Austin, Texas",
        }
        with caplog.at_level(logging.WARNING):
            event = normalize_event(raw_event, "Meetup")
        
        assert event is None
        assert "missing or empty required field 'event_url'" in caplog.text

    def test_normalize_missing_start_datetime(self, caplog):
        """Test that event is excluded when start_datetime is missing."""
        raw_event = {
            "title": "Event",
            "city": "Austin, Texas",
            "event_url": "https://www.meetup.com/events/123456",
        }
        with caplog.at_level(logging.WARNING):
            event = normalize_event(raw_event, "Meetup")
        
        assert event is None
        assert "missing or empty required field 'start_datetime'" in caplog.text

    def test_normalize_preserves_url_byte_for_byte(self):
        """Test that event_url is preserved exactly as provided."""
        url = "https://www.meetup.com/events/123456?param=value&other=test"
        raw_event = {
            "title": "Event",
            "start_datetime": "2025-09-15T18:00:00Z",
            "city": "Austin, Texas",
            "event_url": url,
        }
        event = normalize_event(raw_event, "Meetup")
        
        assert event is not None
        assert event.event_url == url

    def test_normalize_handles_missing_optional_fields(self):
        """Test that missing optional fields are set to None."""
        raw_event = {
            "title": "Event",
            "start_datetime": "2025-09-15T18:00:00Z",
            "city": "Austin, Texas",
            "event_url": "https://www.meetup.com/events/123456",
            # description, end_datetime, location_name all missing
        }
        event = normalize_event(raw_event, "Meetup")
        
        assert event is not None
        assert event.description is None
        assert event.end_datetime is None
        assert event.location_name is None

    def test_normalize_handles_empty_optional_fields(self):
        """Test that empty optional fields are set to None."""
        raw_event = {
            "title": "Event",
            "start_datetime": "2025-09-15T18:00:00Z",
            "city": "Austin, Texas",
            "event_url": "https://www.meetup.com/events/123456",
            "description": "",
            "end_datetime": "   ",
            "location_name": None,
        }
        event = normalize_event(raw_event, "Meetup")
        
        assert event is not None
        assert event.description is None
        assert event.end_datetime is None
        assert event.location_name is None

    def test_normalize_strips_whitespace_from_title(self):
        """Test that whitespace is stripped from title."""
        raw_event = {
            "title": "  Event Title  ",
            "start_datetime": "2025-09-15T18:00:00Z",
            "city": "Austin, Texas",
            "event_url": "https://www.meetup.com/events/123456",
        }
        event = normalize_event(raw_event, "Meetup")
        
        assert event is not None
        assert event.title == "Event Title"

    def test_normalize_strips_whitespace_from_city(self):
        """Test that whitespace is stripped from city."""
        raw_event = {
            "title": "Event",
            "start_datetime": "2025-09-15T18:00:00Z",
            "city": "  Austin, Texas  ",
            "event_url": "https://www.meetup.com/events/123456",
        }
        event = normalize_event(raw_event, "Meetup")
        
        assert event is not None
        assert event.city == "Austin, Texas"

    def test_normalize_invalid_start_datetime_format(self, caplog):
        """Test that event is excluded with invalid start_datetime format."""
        raw_event = {
            "title": "Event",
            "start_datetime": "not-a-date",
            "city": "Austin, Texas",
            "event_url": "https://www.meetup.com/events/123456",
        }
        with caplog.at_level(logging.WARNING):
            event = normalize_event(raw_event, "Meetup")
        
        assert event is None
        assert "failed to parse start_datetime" in caplog.text

    def test_normalize_invalid_end_datetime_format(self, caplog):
        """Test that event is excluded with completely invalid end_datetime format."""
        raw_event = {
            "title": "Event",
            "start_datetime": "2025-09-15T18:00:00Z",
            "end_datetime": "not-a-date",  # Completely invalid
            "city": "Austin, Texas",
            "event_url": "https://www.meetup.com/events/123456",
        }
        with caplog.at_level(logging.WARNING):
            event = normalize_event(raw_event, "Meetup")
        
        assert event is None
        assert "failed to parse end_datetime" in caplog.text

    def test_normalize_end_datetime_without_z_is_accepted(self):
        """Test that end_datetime without Z suffix is accepted and converted."""
        raw_event = {
            "title": "Event",
            "start_datetime": "2025-09-15T18:00:00Z",
            "end_datetime": "2025-09-15T20:00:00",  # No Z, but valid format
            "city": "Austin, Texas",
            "event_url": "https://www.meetup.com/events/123456",
        }
        event = normalize_event(raw_event, "Meetup")
        
        assert event is not None
        # Should be converted to ISO 8601 UTC with Z
        assert event.end_datetime == "2025-09-15T20:00:00Z"

    def test_normalize_non_dict_raw_event(self, caplog):
        """Test that function handles non-dict raw_event gracefully."""
        with caplog.at_level(logging.WARNING):
            event = normalize_event("not-a-dict", "Meetup")
        
        assert event is None
        assert "raw_event must be a dict" in caplog.text

    def test_normalize_preserves_source_platform_name(self):
        """Test that source_platform is preserved in the event."""
        raw_event = {
            "title": "Event",
            "start_datetime": "2025-09-15T18:00:00Z",
            "city": "Austin, Texas",
            "event_url": "https://www.meetup.com/events/123456",
        }
        event = normalize_event(raw_event, "CustomPlatform")
        
        assert event is not None
        assert event.source_platform == "CustomPlatform"

    def test_normalize_passes_match_confidence(self):
        """Test that match_confidence is passed through."""
        raw_event = {
            "title": "Event",
            "start_datetime": "2025-09-15T18:00:00Z",
            "city": "Austin, Texas",
            "event_url": "https://www.meetup.com/events/123456",
        }
        event = normalize_event(raw_event, "Meetup", match_confidence=0.75)
        
        assert event is not None
        assert event.match_confidence == 0.75

    def test_normalize_passes_source_invocation_order(self):
        """Test that source_invocation_order is passed through."""
        raw_event = {
            "title": "Event",
            "start_datetime": "2025-09-15T18:00:00Z",
            "city": "Austin, Texas",
            "event_url": "https://www.meetup.com/events/123456",
        }
        event = normalize_event(raw_event, "Meetup", source_invocation_order=3)
        
        assert event is not None
        assert event.source_invocation_order == 3

    def test_normalize_uses_event_title_for_logging(self, caplog):
        """Test that event title is used in logging output."""
        raw_event = {
            "title": "My Special Event",
            "start_datetime": "2025-09-15T18:00:00Z",
            # city is missing to trigger the log
        }
        with caplog.at_level(logging.WARNING):
            event = normalize_event(raw_event, "Meetup")
        
        assert event is None
        assert "My Special Event" in caplog.text

    def test_normalize_uses_event_identifier_for_logging(self, caplog):
        """Test that custom event_identifier is used in logging."""
        raw_event = {
            "title": "Event",
            "start_datetime": "2025-09-15T18:00:00Z",
            # city is missing to trigger the log
        }
        with caplog.at_level(logging.WARNING):
            event = normalize_event(raw_event, "Meetup", event_identifier="event_123")
        
        assert event is None
        assert "event_123" in caplog.text


class TestConvertToISO8601UTC:
    """Tests for the _convert_to_iso8601_utc() helper function."""

    def test_convert_already_iso8601_utc(self):
        """Test that ISO 8601 UTC format is returned unchanged."""
        result = _convert_to_iso8601_utc("2025-09-15T18:00:00Z")
        assert result == "2025-09-15T18:00:00Z"

    def test_convert_iso8601_with_positive_offset(self):
        """Test conversion of ISO 8601 with positive timezone offset."""
        # "2025-09-15T14:00:00+05:00" is 09:00:00 UTC
        # 14:00 with +05:00 offset means 5 hours ahead of UTC
        # So 14:00 - 05:00 = 09:00 UTC
        result = _convert_to_iso8601_utc("2025-09-15T14:00:00+05:00")
        assert result == "2025-09-15T09:00:00Z"

    def test_convert_iso8601_with_negative_offset(self):
        """Test conversion of ISO 8601 with negative timezone offset."""
        # "2025-09-15T23:00:00-05:00" is 04:00:00 UTC (next day)
        result = _convert_to_iso8601_utc("2025-09-15T23:00:00-05:00")
        assert result == "2025-09-16T04:00:00Z"

    def test_convert_iso8601_without_timezone(self):
        """Test that ISO 8601 without timezone is assumed to be UTC."""
        result = _convert_to_iso8601_utc("2025-09-15T18:00:00")
        assert result == "2025-09-15T18:00:00Z"

    def test_convert_date_only(self):
        """Test that date-only string is converted to midnight UTC."""
        result = _convert_to_iso8601_utc("2025-09-15")
        assert result == "2025-09-15T00:00:00Z"

    def test_convert_space_separated_datetime(self):
        """Test conversion of space-separated datetime format."""
        result = _convert_to_iso8601_utc("2025-09-15 18:00:00")
        assert result == "2025-09-15T18:00:00Z"

    def test_convert_common_format(self):
        """Test conversion of common format: 'Month Day, Year at Hour:Minute AM/PM'."""
        result = _convert_to_iso8601_utc("September 15, 2025 at 6:00 PM")
        assert result == "2025-09-15T18:00:00Z"

    def test_convert_invalid_format(self):
        """Test that invalid datetime format raises ValueError."""
        with pytest.raises(ValueError, match="Could not parse datetime string"):
            _convert_to_iso8601_utc("not-a-date")

    def test_convert_whitespace_stripping(self):
        """Test that leading/trailing whitespace is stripped."""
        result = _convert_to_iso8601_utc("  2025-09-15  ")
        assert result == "2025-09-15T00:00:00Z"

    def test_convert_non_string_input(self):
        """Test that non-string input raises ValueError."""
        with pytest.raises(ValueError, match="datetime_str must be a string"):
            _convert_to_iso8601_utc(123)

    def test_convert_edge_case_midnight_utc(self):
        """Test conversion of midnight UTC."""
        result = _convert_to_iso8601_utc("2025-01-01T00:00:00Z")
        assert result == "2025-01-01T00:00:00Z"

    def test_convert_edge_case_end_of_day_utc(self):
        """Test conversion of end-of-day UTC."""
        result = _convert_to_iso8601_utc("2025-12-31T23:59:59Z")
        assert result == "2025-12-31T23:59:59Z"
