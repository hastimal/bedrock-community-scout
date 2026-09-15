"""
Community Scout — minimal Streamlit demo UI.

This is a thin presentation layer over the DEPLOYED Amazon Bedrock AgentCore Runtime.
It performs NO event search, ranking, deduplication, or normalization itself — it
only collects topic selections, invokes the Runtime with a natural-language prompt,
and renders the grounded Markdown response the Runtime returns.

Request path (unchanged by this UI):

    Streamlit UI
        -> AgentCore Runtime  (invoke_agent_runtime)
            -> Strands + Claude Sonnet 4.6
                -> AgentCore Gateway
                    -> Web Search
                        -> Meetup / Luma

The UI never calls Meetup/Luma or the Gateway directly and never reproduces any
backend search/ranking logic.
"""

import json
import os
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError
import streamlit as st


# --- Configuration (no hardcoded credentials) ------------------------------- #
# Region and Runtime ARN are configurable via environment variables, defaulting to
# the deployed Community Scout Runtime. AWS credentials are resolved by the standard
# boto3/botocore credential chain (env vars, shared config/credentials, SSO, IAM role,
# etc.) — this app never handles or stores credentials itself.
DEFAULT_REGION = "us-east-1"
DEFAULT_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:155079498937:runtime/"
    "community_scout_runtime-nWJoV8DknT"
)

AWS_REGION = os.environ.get("AWS_REGION", DEFAULT_REGION)
AGENTCORE_RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", DEFAULT_RUNTIME_ARN)

LOCATION = "Austin, Texas"
TIME_WINDOW = "Next 90 days"
TOPIC_CHOICES = ["Agentic AI", "AWS", "Kubernetes"]


def _build_prompt(topics: list[str]) -> str:
    """Build the natural-language prompt from the selected topics.

    Mirrors the demo prompt contract; the Runtime (Strands/Claude) does the actual
    query interpretation. The location and 90-day window are stated in plain language;
    the deterministic backend computes the authoritative dates.
    """
    if len(topics) == 1:
        topic_text = topics[0]
    elif len(topics) == 2:
        topic_text = f"{topics[0]} and {topics[1]}"
    else:
        topic_text = f"{', '.join(topics[:-1])}, and {topics[-1]}"
    return (
        f"Find {topic_text} community events in Austin during the next 90 days."
    )


def _new_session_id() -> str:
    """Generate a unique runtimeSessionId of at least 33 characters.

    AgentCore requires a session id of >= 33 characters. Two concatenated uuid4 hex
    strings yield 64 characters, comfortably satisfying the minimum.
    """
    return (uuid.uuid4().hex + uuid.uuid4().hex)  # 64 chars


def _strip_sse_framing(text: str) -> str:
    """Extract the payload from AgentCore's SSE-style response, if present.

    AgentCore Runtime frames the streamed response as Server-Sent Events, e.g.::

        data: "Found 4 events in Austin, Texas:\n\n1. [Event](URL)\n ..."

    This joins the payloads of all ``data:`` lines (concatenated with newlines, per the
    SSE spec) and returns them. When the text has no ``data:`` lines, it is returned
    unchanged so plain/JSON responses still flow through.
    """
    lines = text.splitlines()
    data_payloads = [
        line[len("data:"):].lstrip()
        for line in lines
        if line.lstrip().startswith("data:")
    ]
    if not data_payloads:
        return text
    # Each data payload may itself be a JSON-encoded scalar string; decode before
    # joining so multi-chunk streams don't retain per-chunk quotes/escapes.
    decoded = [_decode_json_string_scalar(chunk) for chunk in data_payloads]
    return "\n".join(decoded)


def _decode_json_string_scalar(text: str) -> str:
    """If ``text`` is a JSON-encoded scalar string, decode it; else return as-is.

    Safely turns a quoted, JSON-escaped payload such as ``"Found 4 events...\n..."``
    into real text (``\n`` -> newline, quotes decoded) using :func:`json.loads` — never
    ``eval``. Non-string JSON or non-JSON text is returned unchanged.
    """
    stripped = text.strip()
    if not (stripped.startswith('"') and stripped.endswith('"')):
        return text
    try:
        decoded = json.loads(stripped)
    except (ValueError, TypeError):
        return text
    return decoded if isinstance(decoded, str) else text


def _decode_runtime_response(raw: bytes, content_type: str) -> str:
    """Decode the Runtime's streaming response blob into display Markdown text.

    Handles AgentCore's SSE-style framing (``data: "..."``), decodes the JSON-escaped
    string payload so escape sequences like ``\n`` become real newlines and quoted
    strings are unquoted, and unwraps common JSON envelope shapes. Markdown (including
    event links) is preserved so URLs stay clickable. Tolerant of plain-string,
    JSON-string, and JSON-envelope responses; never raises.
    """
    if not raw:
        return ""
    try:
        text = raw.decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001 - display path must not raise
        return str(raw)

    # 1) Strip any SSE ``data:`` framing to get the raw payload.
    text = _strip_sse_framing(text).strip()

    # 2) If the payload is a JSON-encoded scalar string, decode escapes/quotes.
    text = _decode_json_string_scalar(text)

    # 3) Otherwise, if it is a JSON envelope/list, unwrap it to Markdown text.
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            return text
        if isinstance(parsed, str):
            return parsed
        if isinstance(parsed, list):
            # A stream of string chunks — join them.
            return "".join(str(chunk) for chunk in parsed)
        if isinstance(parsed, dict):
            for key in ("response", "result", "output", "text", "message", "body"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            # Fall back to the (already de-framed) text.
            return text

    return text


def _invoke_runtime(prompt: str) -> str:
    """Invoke the deployed AgentCore Runtime and return the grounded Markdown text.

    Uses boto3's ``bedrock-agentcore`` data-plane client. Credentials come from the
    standard AWS credential chain. Raises on transport/API errors; the caller renders
    a friendly message.
    """
    client = boto3.client("bedrock-agentcore", region_name=AWS_REGION)
    payload = json.dumps({"prompt": prompt}).encode("utf-8")

    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
        runtimeSessionId=_new_session_id(),
        contentType="application/json",
        accept="application/json",
        payload=payload,
    )

    body = response.get("response")
    raw = body.read() if hasattr(body, "read") else (body or b"")
    return _decode_runtime_response(raw, response.get("contentType", ""))


# --- Page ------------------------------------------------------------------- #
st.set_page_config(page_title="AI Community Scout — Austin", page_icon="🔎")

st.title("AI Community Scout — Austin")
st.caption(
    "Discover AI and cloud community opportunities using Amazon Bedrock AgentCore."
)

st.subheader("Topics")
selected_topics = [
    topic for topic in TOPIC_CHOICES if st.checkbox(topic, value=True, key=f"topic_{topic}")
]

col1, col2 = st.columns(2)
with col1:
    st.markdown(f"**Location:** {LOCATION}")
with col2:
    st.markdown(f"**Time window:** {TIME_WINDOW}")

find = st.button("Find Opportunities", type="primary")

if find:
    if not selected_topics:
        st.warning("Please select at least one topic.")
    else:
        prompt = _build_prompt(selected_topics)
        st.caption(f"Query: {prompt}")
        with st.spinner("Asking the Community Scout via Amazon Bedrock AgentCore…"):
            try:
                result_markdown = _invoke_runtime(prompt)
            except (BotoCoreError, ClientError) as exc:
                st.error(
                    "Sorry — the Community Scout could not be reached right now. "
                    "Please check your AWS credentials/region and try again."
                )
                st.caption(f"Details: {type(exc).__name__}")
            except Exception:  # noqa: BLE001 - keep the demo resilient
                st.error(
                    "Sorry — something went wrong while contacting the Community "
                    "Scout. Please try again."
                )
            else:
                if result_markdown.strip():
                    st.markdown(result_markdown)
                else:
                    st.info("No response was returned. Please try again.")
