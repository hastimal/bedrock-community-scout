"""
Unit tests for the Streamlit UI's AgentCore Runtime response decoding.

These cover ``ui/app.py``'s pure, presentation-only decoding helpers, which normalize
the deployed Runtime's SSE-style response (``data: "..."`` with JSON-escaped content)
into clean Markdown for ``st.markdown``. No backend logic is exercised or reimplemented
here — only response formatting.

The UI module is imported by file path so importing Streamlit's page code does not
require a running Streamlit server (it executes in "bare mode").
"""

import importlib.util
import os

import pytest


@pytest.fixture(scope="module")
def ui_app():
    """Load ui/app.py as a module (bare-mode Streamlit execution)."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(here, "ui", "app.py")
    spec = importlib.util.spec_from_file_location("ui_app_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- The exact reported SSE shape ------------------------------------------- #
SSE_RESPONSE = (
    b'data: "Found 4 events in Austin, Texas:\\n\\n'
    b'1. [Google AI Deep Dive - Ep 3](https://www.meetup.com/austin-ai/events/1/)\\n'
    b'   - Source: Meetup\\n   - City: Austin, Texas\\n   - Date: 2026-09-17\\n\\n'
    b'2. [AI x Edu September Event](https://lu.ma/ai-x-edu)\\n'
    b'   - Source: Meetup\\n   - City: Austin, Texas\\n   - Date: 2026-09-16\\n"'
)


def test_decodes_sse_json_escaped_response_to_markdown(ui_app):
    """SSE ``data: "..."`` with JSON escapes becomes clean Markdown with real newlines."""
    out = ui_app._decode_runtime_response(SSE_RESPONSE, "text/event-stream")

    # No SSE prefix, no surrounding quotes, no literal escape sequences.
    assert not out.lstrip().startswith("data:")
    assert not (out.startswith('"') and out.endswith('"'))
    assert "\\n" not in out
    # Real newlines are present.
    assert "\n" in out
    # Content is the grounded text.
    assert out.startswith("Found 4 events in Austin, Texas:")
    # Markdown event links preserved (titles remain clickable).
    assert "[Google AI Deep Dive - Ep 3](https://www.meetup.com/austin-ai/events/1/)" in out
    assert "[AI x Edu September Event](https://lu.ma/ai-x-edu)" in out


def test_strip_sse_framing_extracts_data_payload(ui_app):
    # A single quoted data payload is de-framed and its JSON scalar decoded.
    assert ui_app._strip_sse_framing('data: "hello"') == "hello"
    # No SSE framing -> unchanged.
    assert ui_app._strip_sse_framing("no framing here") == "no framing here"
    # A non-quoted data payload is returned as-is (no JSON decode possible).
    assert ui_app._strip_sse_framing("data: plain line") == "plain line"


def test_strip_sse_framing_joins_and_decodes_multiple_data_lines(ui_app):
    # Each quoted data payload is decoded, then joined with a newline.
    assert ui_app._strip_sse_framing('data: "a"\ndata: "b"') == "a\nb"


def test_decode_json_string_scalar_unescapes(ui_app):
    assert ui_app._decode_json_string_scalar('"line1\\nline2"') == "line1\nline2"
    # Non-quoted text is returned unchanged.
    assert ui_app._decode_json_string_scalar("plain text") == "plain text"


def test_plain_markdown_response_passthrough(ui_app):
    raw = b"# Heading\n[link](https://lu.ma/x)"
    out = ui_app._decode_runtime_response(raw, "text/plain")
    assert out == "# Heading\n[link](https://lu.ma/x)"


def test_json_envelope_response_is_unwrapped(ui_app):
    raw = b'{"result": "Found 1 event\\n[E](https://lu.ma/e)"}'
    out = ui_app._decode_runtime_response(raw, "application/json")
    assert out == "Found 1 event\n[E](https://lu.ma/e)"
    assert "\\n" not in out


def test_empty_response_is_empty_string(ui_app):
    assert ui_app._decode_runtime_response(b"", "") == ""
    assert ui_app._decode_runtime_response(None, "") == ""  # type: ignore[arg-type]
