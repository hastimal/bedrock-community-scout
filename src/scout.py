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

import json
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


#: Base system prompt driving the LLM tool-use loop. It instructs the model to
#: interpret the query, invoke the domain-filtered Web Search event source, extract
#: structured events from the returned snippets, and never fabricate any event fact.
#: The authoritative current date and default time window are appended per invocation
#: by :func:`_build_system_prompt` (never baked in at import time), so the model must
#: resolve relative dates from the application-supplied date, not its own knowledge.
SYSTEM_PROMPT_TEMPLATE = f"""\
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

3. TIME WINDOW: Resolve the time window using ONLY the authoritative CURRENT DATE \
supplied below by the application — never your own notion of today's date. Extract \
an explicit time window from the query when present (absolute dates, or relative \
expressions such as "next 90 days", "next 30 days", or "next month"), computing all \
relative expressions relative to that CURRENT DATE. If the query does not specify a \
time window, use the DEFAULT {DEFAULT_WINDOW_DAYS}-DAY WINDOW supplied below verbatim.

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


def _build_date_context() -> str:
    """Build the authoritative CURRENT DATE / DEFAULT WINDOW block for this invocation.

    The current UTC date and the deterministic default 90-day window are computed in
    Python (via :func:`_default_window`) at call time — never at import time — so a
    long-running AgentCore Runtime instance never serves a stale date. This block is
    appended to the system prompt so the model resolves all relative date expressions
    against the application-supplied date rather than its own internal knowledge.
    """
    current_date = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    default_start, default_end = _default_window()
    return (
        f"CURRENT DATE: {current_date}\n"
        f"DEFAULT {DEFAULT_WINDOW_DAYS}-DAY WINDOW: {default_start} through "
        f"{default_end}\n"
        "\n"
        "All relative date expressions in the user's query (for example "
        '"next 90 days", "next 30 days", "next month") MUST be resolved relative to '
        "the CURRENT DATE above. Do not use any other notion of the current date. "
        "When the query specifies no time window, use the DEFAULT window above exactly."
    )


def _build_system_prompt() -> str:
    """Compose the effective per-invocation system prompt.

    Combines the static :data:`SYSTEM_PROMPT_TEMPLATE` rules with the authoritative
    date context from :func:`_build_date_context`. Built per invocation so the injected
    date is always current.
    """
    return f"{SYSTEM_PROMPT_TEMPLATE}\n{_build_date_context()}"


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


def _build_agent(model_id: str, system_prompt: str):
    """Construct the Strands ``Agent`` for the tool-use loop.

    Isolated so tests can substitute a fake agent. Strands/Bedrock is used ONLY for
    reasoning and tool invocation; its model-generated text is never surfaced.

    ``system_prompt`` is the effective per-invocation prompt (base rules plus the
    authoritative CURRENT DATE / DEFAULT WINDOW block), so the model resolves relative
    dates from the application-supplied date rather than its own knowledge.

    ``callback_handler=None`` disables Strands' default callback handler, which would
    otherwise print model/tool streaming output to stdout. Merely ignoring the events
    returned by ``stream_async()`` does not silence that callback, so it must be
    disabled here. The only user-visible output comes from the deterministic pipeline's
    :func:`format_response`.

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
    return Agent(
        model=model_id,
        system_prompt=system_prompt,
        # Register the JSON-safe Strands tool wrapper (returns a structured
        # {"status": ..., "events"/"tool_name"/"reason": ...} envelope), NOT a function
        # that returns raw ``list[CommonEvent]`` — otherwise Strands would serialize the
        # result via ``repr()`` and the collector's ``json.loads`` would fail.
        tools=[agentcore_web_search_event_source],
        callback_handler=None,
    )


async def _run_agent(prompt: str, model_id: str, agent_factory=None) -> list:
    """
    Drive the Strands tool-use loop and collect ONLY tool results.

    The agent is built at invocation time with the system prompt, the resolved
    ``model_id``, and the ``agentcore_web_search_event_source`` tool. The agent's
    streamed events are consumed to completion for their side effect of running the
    tool loop; every ``CommonEvent`` list and ``ToolError`` produced by the event
    source is collected and returned for the deterministic pipeline.

    Intermediate model output — assistant text, reasoning, and content deltas emitted
    by ``Agent.stream_async`` before/during tool execution — is NEVER yielded,
    printed, forwarded, or otherwise exposed here. Strands/Bedrock is used strictly
    for reasoning and tool invocation; the only user-visible response comes from the
    deterministic pipeline in :func:`handler` (Requirement: suppress intermediate
    model text).

    Args:
        prompt: The validated user query.
        model_id: The Bedrock model ID resolved from configuration.
        agent_factory: Optional callable ``(model_id, system_prompt)`` returning an
            object with an async ``stream_async(prompt)`` method. When ``None``
            (default), resolved to :func:`_build_agent` at call time; overridable in
            tests.

    Returns:
        A list of tool results — each element is either a ``list[CommonEvent]`` or a
        ``ToolError`` — suitable for :func:`src.pipeline.aggregator.aggregate`.

    Raises:
        RuntimeError: If the Strands agent framework is unavailable in this
            environment (propagated from :func:`_build_agent`).
    """
    if agent_factory is None:
        # Resolved at call time (not bound as a default) so tests can monkeypatch
        # ``scout._build_agent`` and so the default path stays current.
        agent_factory = _build_agent

    # Build the effective system prompt per invocation so the authoritative current
    # date / default window is always fresh (never stale from a long-running runtime).
    system_prompt = _build_system_prompt()
    agent = agent_factory(model_id, system_prompt)

    tool_outputs: list = []
    async for event in agent.stream_async(prompt):
        # Collect recognized tool results only. Any model-generated text / reasoning /
        # lifecycle event is intentionally ignored and never forwarded to the user.
        _collect_tool_output(event, tool_outputs)

    return tool_outputs


#: Required CommonEvent fields; a deserialized tool-result record missing any of these
#: is discarded rather than reconstructed (evidence-only, no fabrication).
_REQUIRED_EVENT_FIELDS = ("title", "start_datetime", "city", "source_platform", "event_url")
#: Optional CommonEvent fields carried through when present in the tool result.
_OPTIONAL_EVENT_FIELDS = (
    "description",
    "end_datetime",
    "location_name",
    "match_confidence",
    "source_invocation_order",
)


def _collect_tool_output(event: Any, tool_outputs: list) -> None:
    """
    Extract event-source tool result(s) carried by a streamed agent event.

    Strands emits the completed tool result inside a ``message`` event as a
    ``toolResult`` block whose ``content[].text`` holds the JSON the tool returned
    (see :func:`src.tools.agentcore_web_search_event_source`). This helper finds every
    such ``toolResult`` block, reconstructs a ``list[CommonEvent]`` or a ``ToolError``
    from each, and appends them to ``tool_outputs``. Model assistant text, reasoning,
    and lifecycle events are ignored — never forwarded to the user.

    Args:
        event: A single item yielded by the agent stream.
        tool_outputs: The accumulator list mutated in place.
    """
    # Backward-compatible direct shapes (used by unit tests and defensive):
    if isinstance(event, ToolError):
        tool_outputs.append(event)
        return
    if isinstance(event, list) and event and all(
        isinstance(item, CommonEvent) for item in event
    ):
        tool_outputs.append(event)
        return

    if not isinstance(event, dict):
        return

    # Strands wraps the finished tool result under message -> content[] -> toolResult.
    for tool_result in _iter_tool_results(event):
        collected = _tool_result_to_output(tool_result)
        if collected is not None:
            tool_outputs.append(collected)


def _iter_tool_results(event: dict):
    """Yield each ``toolResult`` mapping found within a streamed ``message`` event.

    Handles the confirmed Strands structure::

        {"message": {"role": "user",
                     "content": [{"toolResult": {"status": "...",
                                                 "content": [{"text": "<JSON>"}]}}]}}

    Also tolerates a top-level ``content`` list or a bare ``toolResult`` mapping.
    """
    message = event.get("message")
    containers = []
    if isinstance(message, dict):
        containers.append(message.get("content"))
    containers.append(event.get("content"))

    for content in containers:
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("toolResult"), dict):
                    yield block["toolResult"]

    if isinstance(event.get("toolResult"), dict):
        yield event["toolResult"]


def _tool_result_to_output(
    tool_result: dict,
) -> "list[CommonEvent] | ToolError | None":
    """Reconstruct a ``list[CommonEvent]`` or ``ToolError`` from one toolResult block.

    The tool's JSON payload is carried in ``tool_result["content"][].text``. It is
    parsed with :func:`json.loads` (never ``eval``/``exec`` and never parsing arbitrary
    Python) and mapped as follows:

      * ``{"status": "error", "tool_name": ..., "reason": ...}`` -> ``ToolError``.
      * ``{"status": "success", "events": [ {<fields>}, ... ]}`` -> ``list[CommonEvent]``
        (possibly empty).

    A malformed payload, a Strands-reported error status, or records missing required
    fields are handled safely (returning a ``ToolError``, an empty list, or skipping the
    bad record) — never raising and never fabricating event facts.
    """
    payload = _parse_tool_result_payload(tool_result)
    if payload is None:
        # Nothing parseable in this block.
        # If Strands itself flagged an error status on the block, surface it.
        if tool_result.get("status") == "error":
            return ToolError(
                tool_name="AgentCoreWebSearchEventSource",
                reason="Web Search tool reported an error.",
            )
        return None

    if not isinstance(payload, dict):
        return None

    status = payload.get("status")
    if status == "error":
        return ToolError(
            tool_name=str(payload.get("tool_name") or "AgentCoreWebSearchEventSource"),
            reason=str(payload.get("reason") or "Web Search tool reported an error."),
        )

    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        return None

    events: list[CommonEvent] = []
    for record in raw_events:
        reconstructed = _reconstruct_common_event(record)
        if reconstructed is not None:
            events.append(reconstructed)
    # An empty list is a valid, meaningful result (no events found).
    return events


def _parse_tool_result_payload(tool_result: dict) -> Any:
    """JSON-parse the tool payload from ``tool_result["content"][].text``. Never raises.

    Returns the first successfully parsed JSON value across the content blocks, or
    ``None`` when nothing parses. Uses only :func:`json.loads`.
    """
    content = tool_result.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            continue
    return None


def _reconstruct_common_event(record: Any) -> "CommonEvent | None":
    """Rebuild a ``CommonEvent`` from a JSON record dict, or ``None`` if invalid.

    Validates that all required fields are present and non-empty, preserves optional
    fields when present, and preserves ``event_url`` exactly. Construction is delegated
    to the ``CommonEvent`` dataclass, whose ``__post_init__`` enforces the domain
    invariants; any validation failure results in the record being skipped (no
    fabrication). Never raises.
    """
    if not isinstance(record, dict):
        return None

    kwargs: dict = {}
    for field_name in _REQUIRED_EVENT_FIELDS:
        value = record.get(field_name)
        if not isinstance(value, str) or not value.strip():
            # Missing/invalid required field -> skip this record.
            return None
        kwargs[field_name] = value

    for field_name in _OPTIONAL_EVENT_FIELDS:
        if field_name in record and record.get(field_name) is not None:
            kwargs[field_name] = record[field_name]

    try:
        return CommonEvent(**kwargs)
    except (ValueError, TypeError):
        # Invariant violation (e.g. bad datetime format) -> skip, never fabricate.
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
