"""
Unit tests for the event data models (CommonEvent, RankedEvent, ToolError, QueryContext).

Tests focus on validation logic, invariant enforcement, and edge cases.
"""

import pytest
from src.models.events import CommonEvent, RankedEvent, ToolError, QueryContext


class TestCommonEvent:
    """Tests for the CommonEvent dataclass."""

    def test_valid_common_event_with_all_fields(self):
        """Test that a CommonEvent with all fields is created successfully."""
        event = CommonEvent(
            title="Agentic AI Workshop",
            start_datetime="2025-09-15T18:00:00Z",
            city="Austin, Texas",
            source_platform="Meetup",
            event_url="https://www.meetup.com/events/123456",
            description="Learn about agentic AI systems",
            end_datetime="2025-09-15T20:00:00Z",
            location_name="Austin Tech Hub",
            match_confidence=0.95,
            source_invocation_order=0,
        )
        assert event.title == "Agentic AI Workshop"
        assert event.start_datetime == "2025-09-15T18:00:00Z"
        assert event.city == "Austin, Texas"
        assert event.source_platform == "Meetup"
        assert event.event_url == "https://www.meetup.com/events/123456"
        assert event.description == "Learn about agentic AI systems"
        assert event.end_datetime == "2025-09-15T20:00:00Z"
        assert event.location_name == "Austin Tech Hub"
        assert event.match_confidence == 0.95
        assert event.source_invocation_order == 0

    def test_valid_common_event_with_required_fields_only(self):
        """Test that a CommonEvent with only required fields is created successfully."""
        event = CommonEvent(
            title="AWS Deep Dive",
            start_datetime="2025-10-20T19:00:00Z",
            city="Austin, Texas",
            source_platform="Luma",
            event_url="https://lu.ma/events/evt-123",
        )
        assert event.title == "AWS Deep Dive"
        assert event.start_datetime == "2025-10-20T19:00:00Z"
        assert event.city == "Austin, Texas"
        assert event.source_platform == "Luma"
        assert event.event_url == "https://lu.ma/events/evt-123"
        assert event.description is None
        assert event.end_datetime is None
        assert event.location_name is None
        assert event.match_confidence == 0.0
        assert event.source_invocation_order == 0

    def test_title_cannot_be_empty_string(self):
        """Test that title cannot be an empty string."""
        with pytest.raises(ValueError, match="title must be a non-empty string"):
            CommonEvent(
                title="",
                start_datetime="2025-09-15T18:00:00Z",
                city="Austin, Texas",
                source_platform="Meetup",
                event_url="https://www.meetup.com/events/123456",
            )

    def test_title_cannot_be_whitespace_only(self):
        """Test that title cannot be whitespace-only."""
        with pytest.raises(ValueError, match="title must be a non-empty string"):
            CommonEvent(
                title="   ",
                start_datetime="2025-09-15T18:00:00Z",
                city="Austin, Texas",
                source_platform="Meetup",
                event_url="https://www.meetup.com/events/123456",
            )

    def test_city_cannot_be_empty_string(self):
        """Test that city cannot be an empty string."""
        with pytest.raises(ValueError, match="city must be a non-empty string"):
            CommonEvent(
                title="Agentic AI Workshop",
                start_datetime="2025-09-15T18:00:00Z",
                city="",
                source_platform="Meetup",
                event_url="https://www.meetup.com/events/123456",
            )

    def test_source_platform_cannot_be_empty_string(self):
        """Test that source_platform cannot be an empty string."""
        with pytest.raises(ValueError, match="source_platform must be a non-empty string"):
            CommonEvent(
                title="Agentic AI Workshop",
                start_datetime="2025-09-15T18:00:00Z",
                city="Austin, Texas",
                source_platform="",
                event_url="https://www.meetup.com/events/123456",
            )

    def test_event_url_cannot_be_empty_string(self):
        """Test that event_url cannot be an empty string."""
        with pytest.raises(ValueError, match="event_url must be a non-empty string"):
            CommonEvent(
                title="Agentic AI Workshop",
                start_datetime="2025-09-15T18:00:00Z",
                city="Austin, Texas",
                source_platform="Meetup",
                event_url="",
            )

    def test_start_datetime_valid_iso_8601_utc(self):
        """Test that start_datetime accepts valid ISO 8601 UTC format."""
        event = CommonEvent(
            title="Event",
            start_datetime="2025-12-31T23:59:59Z",
            city="Austin, Texas",
            source_platform="Meetup",
            event_url="https://example.com",
        )
        assert event.start_datetime == "2025-12-31T23:59:59Z"

    def test_start_datetime_invalid_format_no_z_suffix(self):
        """Test that start_datetime rejects format without Z suffix."""
        with pytest.raises(ValueError, match="start_datetime must match ISO 8601 UTC format"):
            CommonEvent(
                title="Event",
                start_datetime="2025-09-15T18:00:00",
                city="Austin, Texas",
                source_platform="Meetup",
                event_url="https://example.com",
            )

    def test_start_datetime_invalid_format_with_offset(self):
        """Test that start_datetime rejects format with timezone offset."""
        with pytest.raises(ValueError, match="start_datetime must match ISO 8601 UTC format"):
            CommonEvent(
                title="Event",
                start_datetime="2025-09-15T18:00:00-05:00",
                city="Austin, Texas",
                source_platform="Meetup",
                event_url="https://example.com",
            )

    def test_start_datetime_invalid_date_format(self):
        """Test that start_datetime rejects invalid date formats."""
        with pytest.raises(ValueError, match="start_datetime must match ISO 8601 UTC format"):
            CommonEvent(
                title="Event",
                start_datetime="09/15/2025T18:00:00Z",
                city="Austin, Texas",
                source_platform="Meetup",
                event_url="https://example.com",
            )

    def test_end_datetime_valid_iso_8601_utc(self):
        """Test that end_datetime accepts valid ISO 8601 UTC format."""
        event = CommonEvent(
            title="Event",
            start_datetime="2025-09-15T18:00:00Z",
            end_datetime="2025-09-15T20:00:00Z",
            city="Austin, Texas",
            source_platform="Meetup",
            event_url="https://example.com",
        )
        assert event.end_datetime == "2025-09-15T20:00:00Z"

    def test_end_datetime_invalid_format(self):
        """Test that end_datetime rejects invalid ISO 8601 UTC format."""
        with pytest.raises(ValueError, match="end_datetime must match ISO 8601 UTC format"):
            CommonEvent(
                title="Event",
                start_datetime="2025-09-15T18:00:00Z",
                end_datetime="2025-09-15T20:00:00",
                city="Austin, Texas",
                source_platform="Meetup",
                event_url="https://example.com",
            )

    def test_end_datetime_can_be_none(self):
        """Test that end_datetime can be None (optional field)."""
        event = CommonEvent(
            title="Event",
            start_datetime="2025-09-15T18:00:00Z",
            end_datetime=None,
            city="Austin, Texas",
            source_platform="Meetup",
            event_url="https://example.com",
        )
        assert event.end_datetime is None

    def test_match_confidence_valid_range(self):
        """Test that match_confidence accepts values in [0.0, 1.0]."""
        for confidence in [0.0, 0.5, 1.0]:
            event = CommonEvent(
                title="Event",
                start_datetime="2025-09-15T18:00:00Z",
                city="Austin, Texas",
                source_platform="Meetup",
                event_url="https://example.com",
                match_confidence=confidence,
            )
            assert event.match_confidence == confidence

    def test_match_confidence_below_zero(self):
        """Test that match_confidence rejects values below 0.0."""
        with pytest.raises(ValueError, match="match_confidence must be in \\[0.0, 1.0\\]"):
            CommonEvent(
                title="Event",
                start_datetime="2025-09-15T18:00:00Z",
                city="Austin, Texas",
                source_platform="Meetup",
                event_url="https://example.com",
                match_confidence=-0.1,
            )

    def test_match_confidence_above_one(self):
        """Test that match_confidence rejects values above 1.0."""
        with pytest.raises(ValueError, match="match_confidence must be in \\[0.0, 1.0\\]"):
            CommonEvent(
                title="Event",
                start_datetime="2025-09-15T18:00:00Z",
                city="Austin, Texas",
                source_platform="Meetup",
                event_url="https://example.com",
                match_confidence=1.1,
            )

    def test_source_invocation_order_valid(self):
        """Test that source_invocation_order accepts non-negative integers."""
        for order in [0, 1, 5, 100]:
            event = CommonEvent(
                title="Event",
                start_datetime="2025-09-15T18:00:00Z",
                city="Austin, Texas",
                source_platform="Meetup",
                event_url="https://example.com",
                source_invocation_order=order,
            )
            assert event.source_invocation_order == order

    def test_source_invocation_order_negative(self):
        """Test that source_invocation_order rejects negative integers."""
        with pytest.raises(ValueError, match="source_invocation_order must be a non-negative integer"):
            CommonEvent(
                title="Event",
                start_datetime="2025-09-15T18:00:00Z",
                city="Austin, Texas",
                source_platform="Meetup",
                event_url="https://example.com",
                source_invocation_order=-1,
            )


class TestRankedEvent:
    """Tests for the RankedEvent dataclass."""

    def test_valid_ranked_event(self):
        """Test that a valid RankedEvent is created successfully."""
        event = CommonEvent(
            title="Event",
            start_datetime="2025-09-15T18:00:00Z",
            city="Austin, Texas",
            source_platform="Meetup",
            event_url="https://example.com",
        )
        ranked = RankedEvent(event=event, relevance_score=85.5)
        assert ranked.event == event
        assert ranked.relevance_score == 85.5

    def test_ranked_event_score_at_boundaries(self):
        """Test that RankedEvent accepts scores at [0.0, 100.0] boundaries."""
        event = CommonEvent(
            title="Event",
            start_datetime="2025-09-15T18:00:00Z",
            city="Austin, Texas",
            source_platform="Meetup",
            event_url="https://example.com",
        )
        for score in [0.0, 50.0, 100.0]:
            ranked = RankedEvent(event=event, relevance_score=score)
            assert ranked.relevance_score == score

    def test_ranked_event_score_below_zero(self):
        """Test that RankedEvent rejects scores below 0.0."""
        event = CommonEvent(
            title="Event",
            start_datetime="2025-09-15T18:00:00Z",
            city="Austin, Texas",
            source_platform="Meetup",
            event_url="https://example.com",
        )
        with pytest.raises(ValueError, match="relevance_score must be in \\[0.0, 100.0\\]"):
            RankedEvent(event=event, relevance_score=-1.0)

    def test_ranked_event_score_above_hundred(self):
        """Test that RankedEvent rejects scores above 100.0."""
        event = CommonEvent(
            title="Event",
            start_datetime="2025-09-15T18:00:00Z",
            city="Austin, Texas",
            source_platform="Meetup",
            event_url="https://example.com",
        )
        with pytest.raises(ValueError, match="relevance_score must be in \\[0.0, 100.0\\]"):
            RankedEvent(event=event, relevance_score=101.0)

    def test_ranked_event_score_not_number(self):
        """Test that RankedEvent rejects non-numeric scores."""
        event = CommonEvent(
            title="Event",
            start_datetime="2025-09-15T18:00:00Z",
            city="Austin, Texas",
            source_platform="Meetup",
            event_url="https://example.com",
        )
        with pytest.raises(ValueError, match="relevance_score must be a number"):
            RankedEvent(event=event, relevance_score="85.5")


class TestToolError:
    """Tests for the ToolError dataclass."""

    def test_valid_tool_error(self):
        """Test that a valid ToolError is created successfully."""
        error = ToolError(
            tool_name="AgentCoreWebSearchEventSource",
            reason="HTTP 503 from Web Search Tool"
        )
        assert error.tool_name == "AgentCoreWebSearchEventSource"
        assert error.reason == "HTTP 503 from Web Search Tool"

    def test_tool_error_name_cannot_be_empty(self):
        """Test that tool_name cannot be empty."""
        with pytest.raises(ValueError, match="tool_name must be a non-empty string"):
            ToolError(tool_name="", reason="Some error")

    def test_tool_error_reason_cannot_be_empty(self):
        """Test that reason cannot be empty."""
        with pytest.raises(ValueError, match="reason must be a non-empty string"):
            ToolError(tool_name="SomeTool", reason="")

    def test_tool_error_reason_cannot_be_whitespace_only(self):
        """Test that reason cannot be whitespace-only."""
        with pytest.raises(ValueError, match="reason must be a non-empty string"):
            ToolError(tool_name="SomeTool", reason="   ")


class TestQueryContext:
    """Tests for the QueryContext dataclass."""

    def test_valid_query_context(self):
        """Test that a valid QueryContext is created successfully."""
        context = QueryContext(
            topics=["Agentic AI", "AWS"],
            location="Austin, Texas",
            start_date="2025-09-01",
            end_date="2025-11-30",
        )
        assert context.topics == ["Agentic AI", "AWS"]
        assert context.location == "Austin, Texas"
        assert context.start_date == "2025-09-01"
        assert context.end_date == "2025-11-30"

    def test_query_context_single_topic(self):
        """Test that QueryContext accepts a single topic."""
        context = QueryContext(
            topics=["Kubernetes"],
            location="Austin, Texas",
            start_date="2025-09-01",
            end_date="2025-11-30",
        )
        assert len(context.topics) == 1
        assert context.topics[0] == "Kubernetes"

    def test_query_context_ten_topics(self):
        """Test that QueryContext accepts up to 10 topics."""
        topics = [f"Topic {i}" for i in range(1, 11)]
        context = QueryContext(
            topics=topics,
            location="Austin, Texas",
            start_date="2025-09-01",
            end_date="2025-11-30",
        )
        assert len(context.topics) == 10

    def test_query_context_more_than_ten_topics(self):
        """Test that QueryContext rejects more than 10 topics."""
        topics = [f"Topic {i}" for i in range(1, 12)]
        with pytest.raises(ValueError, match="topics list must contain at most 10 topics"):
            QueryContext(
                topics=topics,
                location="Austin, Texas",
                start_date="2025-09-01",
                end_date="2025-11-30",
            )

    def test_query_context_no_topics(self):
        """Test that QueryContext rejects empty topics list."""
        with pytest.raises(ValueError, match="topics list must contain at least 1 topic"):
            QueryContext(
                topics=[],
                location="Austin, Texas",
                start_date="2025-09-01",
                end_date="2025-11-30",
            )

    def test_query_context_topic_too_long(self):
        """Test that QueryContext rejects topics longer than 200 characters."""
        long_topic = "a" * 201
        with pytest.raises(ValueError, match="topic at index 0 must be <= 200 characters"):
            QueryContext(
                topics=[long_topic],
                location="Austin, Texas",
                start_date="2025-09-01",
                end_date="2025-11-30",
            )

    def test_query_context_topic_exactly_200_chars(self):
        """Test that QueryContext accepts topics exactly 200 characters."""
        topic_200 = "a" * 200
        context = QueryContext(
            topics=[topic_200],
            location="Austin, Texas",
            start_date="2025-09-01",
            end_date="2025-11-30",
        )
        assert context.topics[0] == topic_200

    def test_query_context_empty_topic(self):
        """Test that QueryContext rejects empty topics."""
        with pytest.raises(ValueError, match="topic at index 0 must be non-empty"):
            QueryContext(
                topics=["", "Valid Topic"],
                location="Austin, Texas",
                start_date="2025-09-01",
                end_date="2025-11-30",
            )

    def test_query_context_location_cannot_be_empty(self):
        """Test that location cannot be empty."""
        with pytest.raises(ValueError, match="location must be a non-empty string"):
            QueryContext(
                topics=["Topic"],
                location="",
                start_date="2025-09-01",
                end_date="2025-11-30",
            )

    def test_query_context_location_too_long(self):
        """Test that QueryContext rejects locations longer than 200 characters."""
        long_location = "a" * 201
        with pytest.raises(ValueError, match="location must be <= 200 characters"):
            QueryContext(
                topics=["Topic"],
                location=long_location,
                start_date="2025-09-01",
                end_date="2025-11-30",
            )

    def test_query_context_start_date_invalid_format(self):
        """Test that QueryContext rejects invalid start_date format."""
        with pytest.raises(ValueError, match="start_date must be ISO 8601 date"):
            QueryContext(
                topics=["Topic"],
                location="Austin, Texas",
                start_date="09/01/2025",
                end_date="2025-11-30",
            )

    def test_query_context_end_date_invalid_format(self):
        """Test that QueryContext rejects invalid end_date format."""
        with pytest.raises(ValueError, match="end_date must be ISO 8601 date"):
            QueryContext(
                topics=["Topic"],
                location="Austin, Texas",
                start_date="2025-09-01",
                end_date="11/30/2025",
            )

    def test_query_context_start_date_after_end_date(self):
        """Test that QueryContext rejects when start_date > end_date."""
        with pytest.raises(ValueError, match="start_date must be <= end_date"):
            QueryContext(
                topics=["Topic"],
                location="Austin, Texas",
                start_date="2025-11-30",
                end_date="2025-09-01",
            )

    def test_query_context_same_start_and_end_date(self):
        """Test that QueryContext allows same start and end date."""
        context = QueryContext(
            topics=["Topic"],
            location="Austin, Texas",
            start_date="2025-09-01",
            end_date="2025-09-01",
        )
        assert context.start_date == context.end_date
