"""
Unit tests for parsing/extraction against the REAL deployed AgentCore Web Search
response shape.

The live MCP call to the deployed Gateway returns a response whose content blocks
are shaped ``{"text": "<serialized JSON>"}`` with NO ``"type"`` key, and whose
individual results carry the evidence body in a ``"text"`` field (not ``"snippet"``).
These tests pin both behaviors:

1. ``_parse_search_results`` parses a ``{"text": "<JSON>"}`` block even without a
   ``"type": "text"`` marker, while remaining compatible with the previously
   supported shapes (structured ``json`` blocks, ``{"type": "text", ...}`` blocks,
   and plain result dicts).
2. ``_extract_raw_event`` reads event evidence from the result's ``"text"`` field,
   falling back to ``"snippet"``. The event date is extracted from the evidence text
   (never ``publishedDate``), the original URL is preserved byte-for-byte, and Austin
   is required in the evidence.
"""

import json

from src.tools.agentcore_web_search_event_source import AgentCoreWebSearchEventSource


REAL_EVENT_URL = "https://www.meetup.com/austin-ai/events/301-agentic-ai/?ref=home"
# Evidence text carries the event date and the city; publishedDate is deliberately
# a DIFFERENT date so a test can prove it is never used as the event date.
REAL_EVIDENCE_TEXT = (
    "Join the Austin, Texas community on 2025-09-15T18:00:00Z for an Agentic AI on "
    "AWS meetup with hands-on labs."
)
PUBLISHED_DATE = "2025-08-01"  # NOT the event date; must be ignored for dating.


def _real_gateway_response():
    """Return a response matching the exact shape from the deployed AWS Gateway."""
    inner = {
        "id": "abc123",
        "results": [
            {
                "publishedDate": PUBLISHED_DATE,
                "text": REAL_EVIDENCE_TEXT,
                "title": "Agentic AI on AWS",
                "url": REAL_EVENT_URL,
            }
        ],
    }
    return {
        "status": "success",
        "toolUseId": "tool-use-xyz",
        "content": [
            {"text": json.dumps(inner)},  # NOTE: no "type" key.
        ],
    }


# --------------------------------------------------------------------------- #
# _parse_search_results
# --------------------------------------------------------------------------- #
def test_parses_real_gateway_content_block_without_type_key():
    """A {"text": "<JSON>"} block (no "type") yields the inner results."""
    response = _real_gateway_response()

    results = AgentCoreWebSearchEventSource._parse_search_results(response)

    assert len(results) == 1
    result = results[0]
    assert result["url"] == REAL_EVENT_URL
    assert result["title"] == "Agentic AI on AWS"
    assert result["text"] == REAL_EVIDENCE_TEXT
    assert result["publishedDate"] == PUBLISHED_DATE


def test_parses_legacy_typed_text_block_still_supported():
    """A legacy {"type": "text", "text": "<JSON>"} block is still parsed."""
    inner = {"results": [{"url": REAL_EVENT_URL, "title": "T", "text": "x"}]}
    response = {"content": [{"type": "text", "text": json.dumps(inner)}]}

    results = AgentCoreWebSearchEventSource._parse_search_results(response)

    assert len(results) == 1
    assert results[0]["url"] == REAL_EVENT_URL


def test_parses_structured_json_block_still_supported():
    """A structured {"json": {"results": [...]}} block is still parsed."""
    response = {
        "content": [
            {"json": {"results": [{"url": REAL_EVENT_URL, "title": "T", "text": "x"}]}}
        ]
    }

    results = AgentCoreWebSearchEventSource._parse_search_results(response)

    assert len(results) == 1
    assert results[0]["url"] == REAL_EVENT_URL


def test_plain_result_dict_with_text_field_is_not_json_parsed():
    """A plain result dict (has url/title, text is prose) is returned as-is.

    Its "text" field is evidence prose, not serialized JSON, so it must be treated
    as a result rather than attempted as JSON.
    """
    response = {
        "content": [
            {"url": REAL_EVENT_URL, "title": "Agentic AI", "text": REAL_EVIDENCE_TEXT}
        ]
    }

    results = AgentCoreWebSearchEventSource._parse_search_results(response)

    assert len(results) == 1
    assert results[0]["url"] == REAL_EVENT_URL
    assert results[0]["text"] == REAL_EVIDENCE_TEXT


# --------------------------------------------------------------------------- #
# _extract_raw_event
# --------------------------------------------------------------------------- #
def test_extracts_event_using_text_field_as_evidence():
    """Evidence is read from "text"; date comes from the text, URL preserved exactly."""
    source = AgentCoreWebSearchEventSource()
    result = {
        "publishedDate": PUBLISHED_DATE,
        "text": REAL_EVIDENCE_TEXT,
        "title": "Agentic AI on AWS",
        "url": REAL_EVENT_URL,
    }

    raw_event = source._extract_raw_event(result, "2025-01-01", "2025-12-31")

    assert raw_event is not None
    assert raw_event["title"] == "Agentic AI on AWS"
    assert raw_event["event_url"] == REAL_EVENT_URL  # byte-for-byte preserved.
    assert raw_event["city"] == "Austin, Texas"
    # Event date comes from the evidence text, NOT publishedDate.
    assert raw_event["start_datetime"].startswith("2025-09-15")
    assert PUBLISHED_DATE not in raw_event["start_datetime"]


def test_published_date_is_never_used_as_event_date():
    """When the text has no date, the record is excluded — publishedDate is not used."""
    source = AgentCoreWebSearchEventSource()
    result = {
        "publishedDate": PUBLISHED_DATE,  # present, but must never be the event date.
        "text": "An Austin, Texas Agentic AI meetup. (no explicit event date here)",
        "title": "Agentic AI",
        "url": REAL_EVENT_URL,
    }

    raw_event = source._extract_raw_event(result, "2025-01-01", "2025-12-31")

    # No event date could be extracted from the evidence -> excluded, not fabricated.
    assert raw_event is None


def test_snippet_field_still_supported_as_fallback():
    """Compatibility: results using "snippet" (no "text") still extract correctly."""
    source = AgentCoreWebSearchEventSource()
    result = {
        "title": "Agentic AI on AWS",
        "url": REAL_EVENT_URL,
        "snippet": REAL_EVIDENCE_TEXT,
    }

    raw_event = source._extract_raw_event(result, "2025-01-01", "2025-12-31")

    assert raw_event is not None
    assert raw_event["start_datetime"].startswith("2025-09-15")
    assert raw_event["city"] == "Austin, Texas"


def test_end_to_end_parse_then_extract_from_real_response():
    """Parse the real gateway response, then extract — the full inbound path."""
    source = AgentCoreWebSearchEventSource()
    response = _real_gateway_response()

    results = source._parse_search_results(response)
    assert len(results) == 1

    raw_event = source._extract_raw_event(results[0], "2025-01-01", "2025-12-31")
    assert raw_event is not None
    assert raw_event["event_url"] == REAL_EVENT_URL
    assert raw_event["start_datetime"].startswith("2025-09-15")
    assert raw_event["city"] == "Austin, Texas"
