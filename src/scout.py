"""
Scout Orchestrator: the Community Scout entry point on Amazon Bedrock AgentCore.

This module wires together every component built in the earlier layers into a
single agentic workflow hosted on the Amazon Bedrock AgentCore Runtime:

    query validation -> LLM tool-use loop (AgentCoreWebSearchEventSource)
        -> aggregate() -> deduplicate() -> rank() -> format_response()

**Runtime (Requirement 10.1, 10.2, 10.3):**
The orchestrator instantiates a :class:`BedrockAgentCoreApp` and exposes a single
``@app.entrypoint`` handler. The handler drives a Strands :class:`Agent` that is
given the ``agentcore_web_search_event_source`` tool as a callable AgentCore tool
action. The Bedrock foundation model performs query interpretation and structured
event extraction from the returned Web Search snippets; the deterministic pipeline
(aggregate/deduplicate/rank/format) is pure Python.

**Model selection (Requirement 10.3):**
The model ID is read from :func:`src.config.get_model_id` at handler invocation
time — never at import time — so changing ``BEDROCK_MODEL_ID`` does not require a
runtime restart.

**Query interpretation (Requirements 1.1–1.6, 11.5):**
The system prompt instructs the LLM to extract topics and an optional time window
(the location is fixed to Austin, Texas). Deterministic guardrails from
:mod:`src.validation` run *before* the agent is invoked to reject empty queries,
request clarification when no topic is present, reject queries with more than ten
topics, and clamp over-long time windows to the default 90-day window.

**Factual accuracy (Requirements 3.3, 9.1):**
The system prompt forbids the LLM from inventing, inferring, or fabricating any
event field. All event facts originate from the Web Search Tool results returned
by ``agentcore_web_search_event_source``.

**Error containment (Requirements 2.4, 10.5, 10.6):**
``ToolError`` entries produced by the event source are surfaced in the response
footer without aborting the session. If the AgentCore session itself fails to
establish or maintain, the handler yields an error message and returns no partial
results.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src import config
from src.models.events import CommonEvent, QueryContext, ToolError
from src.pipeline.aggregator import aggregate
from src.pipeline.deduplicator import deduplicate
from src.pipeline.ranker import rank
from src.pipeline.response_generator import format_response
from src.tools.agentcore_web_search_event_source import (
    agentcore_web_search_event_source,
)
from src.validation import (
    DEFAULT_WINDOW_DAYS,
    validate_query,
    validate_time_window,
)

# The AgentCore runtime SDK and the Strands agent framework are runtime
# dependencies (see requirements.txt: bedrock-agentcore, strands-agents). They are
# imported at module load. When they are unavailable (e.g., a lint-only or unit-test
# environment without the runtime installed), fall back to lightweight shims so the
# module remains importable and the deterministic pipeline stays testable. The real
# SDKs supply the production implementations.
try:  # pragma: no cover - exercised only where the AgentCore SDK is installed
    from bedrock_agentcore import BedrockAgentCoreApp
except ImportError:  # pragma: no cover - fallback for environments without the SDK

    class BedrockAgentCoreApp:  # type: ignore[no-redef]
        """Minimal fallback for the AgentCore application object.

        Provides just enough surface — an ``entrypoint`` decorator — for this
        module to import and for the handler to be registered when the real
        ``bedrock-agentcore`` package is not installed.
        """

        def entrypoint(self, func):
            """No-op passthrough decorator mirroring ``@app.entrypoint``."""
            return func


try:  # pragma: no cover - exercised only where Strands is installed
    from strands import Agent
except ImportError:  # pragma: no cover - fallback for environments without Strands
    Agent = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


# The single AgentCore application instance for the Scout Orchestrator.
app = BedrockAgentCoreApp()


#: The fixed Location for v0.1. The LLM must not require the User to supply it.
LOCATION = "Austin, Texas"


#: System prompt driving the LLM tool-use loop. It instructs the model to interpret
#: the query, invoke the domain-filtered Web Search event source, extract structured
#: events from the returned snippets, and never fabricate any event fact.
SYSTEM_PROMPT = f"""\
You are the Community Scout, an assistant that helps technology professionals \
discover upcoming technology community events — meetups, tech talks, and \
community gatherings — in {LOCATION}.

Follow these rules exactly:

1. TOPICS: Extract one or more technology topics of interest from the user's \
query (for example: "Agentic AI", "AWS", "Kubernetes"). If you cannot identify \
at least one topic, do NOT invoke any tool — instead ask the user to specify a \
technology topic.

2. LOCATION: The location is fixed to {LOCATION} for this version. Never ask the \
user for a location and never search any other city.

3. TIME WINDOW: Extract an explicit time window from the query when present \
(absolute dates or relative expressions such as "next 30 days" or "next month"). \
If the query does not specify a time window, use a default window of \
{DEFAULT_WINDOW_DAYS} calendar days starting today.

4. TOOL USE: To find events, call the agentcore_web_search_event_source tool with \
the extracted topics and the start_date and end_date (ISO 8601 YYYY-MM-DD) of the \
time window. This tool searches meetup.com and lu.ma via the AgentCore Web Search \
Tool with a domain filter and returns structured event data extracted from the \
result snippets.

5. FACTUAL ACCURACY: Use ONLY the event data returned by the tool. NEVER invent, \
infer, guess, or fabricate any event title, date, city, venue, description, or \
URL that is not present in the tool results. If the tool returns no events, say \
so plainly and do not manufacture placeholder events.

6. Report event source failures to the user rather than hiding them, and never \
present partial or fabricated results as if they were complete.
"""


@app.entrypoint
async def handler(request: dict):
    """
    AgentCore entry point for a single Community Scout discovery session.

    Drives the end-to-end workflow for one natural-language query: deterministic
    validation, the LLM-driven tool-use loop over the Web Search event source, and
    the deterministic aggregate -> deduplicate -> rank -> format pipeline. The final
    user-facing string is yielded (the handler is an async generator, matching the
    AgentCore streaming contract).

    Args:
        request: The AgentCore request payload. The user's natural-language query is
            read from the ``"prompt"`` key.

    Yields:
        str: A single fully formatted response string. On a validation failure, a
            clarification/error message is yielded instead. If the AgentCore session
            cannot be established or maintained, an error message is yielded and no
            partial results are produced.
    """
    prompt = _extract_prompt(request)

    # Deterministic guardrails run BEFORE any extraction or tool call
    # (Requirements 1.5, 1.6, 11.5). On failure, return the message and stop.
    validation_message = validate_query(prompt)
    if validation_message is not None:
        yield validation_message
        return

    # Read the model ID at invocation time, never at import time (Requirement 10.3).
    model_id = config.get_model_id()

    try:
        tool_outputs = await _run_agent(prompt, model_id)
    except Exception as exc:  # noqa: BLE001 - session failures must not raise out
        # AgentCore session failed to establish or maintain (Requirement 10.6):
        # return an error message, never partial results.
        logger.error("AgentCore session failed: %s", exc)
        yield (
            "The Community Scout could not complete this request because the "
            "agent session could not be established. Please try again."
        )
        return

    # Aggregate tool results, then run the deterministic pipeline. ToolError entries
    # are surfaced in the footer without aborting the session (Requirements 2.4, 10.5).
    events, errors = aggregate(tool_outputs)
    deduplicated = deduplicate(events)

    context = _build_query_context(prompt, deduplicated)
    ranked = rank(deduplicated, context)

    yield format_response(ranked, context, errors)


async def _run_agent(prompt: str, model_id: str) -> list:
    """
    Construct the Strands ``Agent`` and drive the tool-use loop.

    The agent is built at invocation time with the system prompt, the resolved
    ``model_id``, and the ``agentcore_web_search_event_source`` tool. The agent's
    streamed events are consumed to completion; every ``CommonEvent`` list and
    ``ToolError`` produced by the event source is collected and returned for the
    deterministic pipeline.

    Args:
        prompt: The validated user query.
        model_id: The Bedrock model ID resolved from configuration.

    Returns:
        A list of tool results — each element is either a ``list[CommonEvent]`` or a
        ``ToolError`` — suitable for :func:`src.pipeline.aggregator.aggregate`.

    Raises:
        RuntimeError: If the Strands agent framework is unavailable in this
            environment. Raised so the handler can translate it into a session-failure
            message per Requirement 10.6.
    """
    if Agent is None:
        raise RuntimeError(
            "Strands Agent is not available; install 'strands-agents' to run the "
            "Community Scout agent loop."
        )

    agent = Agent(
        model=model_id,
        system_prompt=SYSTEM_PROMPT,
        tools=[agentcore_web_search_event_source],
    )

    tool_outputs: list = []
    async for event in agent.stream_async(prompt):
        _collect_tool_output(event, tool_outputs)

    return tool_outputs


def _collect_tool_output(event: Any, tool_outputs: list) -> None:
    """
    Extract any event-source tool result carried by a streamed agent event.

    The Strands stream emits heterogeneous events; tool results appear as either a
    ``list[CommonEvent]``, a ``ToolError``, or nested inside a mapping (e.g. under a
    ``"result"``/``"output"``/``"content"`` key). This helper appends any recognized
    ``list[CommonEvent]`` or ``ToolError`` to ``tool_outputs`` and ignores everything
    else (text deltas, reasoning, lifecycle events).

    Args:
        event: A single item yielded by the agent stream.
        tool_outputs: The accumulator list mutated in place.
    """
    result = _coerce_tool_result(event)
    if result is not None:
        tool_outputs.append(result)


def _coerce_tool_result(value: Any) -> "list[CommonEvent] | ToolError | None":
    """
    Return a tool result (``list[CommonEvent]`` or ``ToolError``) from ``value``.

    Recognizes a ``ToolError`` directly, a list composed of ``CommonEvent`` records,
    or a mapping that nests such a value under a common result key. Returns ``None``
    when ``value`` carries no recognizable tool result.
    """
    if isinstance(value, ToolError):
        return value

    if isinstance(value, list) and value and all(
        isinstance(item, CommonEvent) for item in value
    ):
        return value

    if isinstance(value, dict):
        for key in ("result", "output", "content", "tool_result", "toolResult"):
            if key in value:
                nested = _coerce_tool_result(value[key])
                if nested is not None:
                    return nested

    return None


def _extract_prompt(request: Any) -> str:
    """
    Read the user's natural-language query from the AgentCore request payload.

    Reads the ``"prompt"`` key from a mapping request. Returns an empty string when
    the prompt is absent or the request is not a mapping, so downstream validation
    can reject it deterministically (Requirement 1.6).
    """
    if isinstance(request, dict):
        prompt = request.get("prompt")
        if isinstance(prompt, str):
            return prompt
    return ""


def _build_query_context(prompt: str, events: list[CommonEvent]) -> QueryContext:
    """
    Build a ``QueryContext`` for ranking and response formatting.

    Topics are derived from the validated query so the deterministic ranker can score
    events by topic match. The location is fixed to Austin, Texas, and the time window
    is normalized through :func:`src.validation.validate_time_window`, falling back to
    the default 90-day window when no explicit window is derivable.

    Args:
        prompt: The validated user query.
        events: The deduplicated events (used only as context; not modified).

    Returns:
        A ``QueryContext`` with topics, the fixed location, and a validated time
        window.
    """
    topics = _extract_topics(prompt)
    start_date, end_date = validate_time_window(*_default_window())
    return QueryContext(
        topics=topics,
        location=LOCATION,
        start_date=start_date,
        end_date=end_date,
    )


def _extract_topics(prompt: str) -> list[str]:
    """
    Derive a deterministic, non-empty list of candidate topics from the query.

    This mirrors the conservative candidate-topic tokenization used by
    :func:`src.validation.validate_query` (splitting on commas and the conjunctions
    "and"/"&"). It provides the ranker with topic strings to match against event
    titles and descriptions. ``validate_query`` has already guaranteed the query is
    non-empty, yields at least one candidate topic, and has at most ten topics, so the
    result here is always a valid 1–10 item list. As a defensive fallback, the whole
    stripped prompt is used when no split token survives.

    Args:
        prompt: The validated user query.

    Returns:
        A list of 1–10 candidate topic strings.
    """
    import re

    split_pattern = re.compile(r"\s*(?:,|\band\b|&)\s*", re.IGNORECASE)
    candidates = [
        token.strip()
        for token in split_pattern.split(prompt.strip())
        if token.strip()
    ]
    if not candidates:
        candidates = [prompt.strip()]
    # QueryContext enforces the 1–10 bound; cap defensively to stay within it.
    return candidates[:10]


def _default_window() -> tuple[str, str]:
    """
    Build the default time window: 90 calendar days starting today (UTC).

    Returns:
        A ``(start_date, end_date)`` tuple of ISO 8601 date strings (YYYY-MM-DD).
    """
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=DEFAULT_WINDOW_DAYS)
    return today.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
