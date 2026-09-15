"""
Orchestration tests for the Community Scout handler (src/scout.py).

These prove the narrow orchestration guarantee: intermediate Strands/Bedrock
assistant text emitted by ``Agent.stream_async`` is NEVER yielded, printed, or
forwarded to the user. The only user-visible response comes from the deterministic
pipeline (aggregate -> deduplicate -> rank -> response_generator), built solely from
the tool results collected off the stream.

The Strands agent is replaced with a fake whose ``stream_async`` interleaves arbitrary
model text with the event source's real tool results (``list[CommonEvent]`` and
``ToolError``). No network, AWS, or model calls occur.
"""

import asyncio

import pytest

from src import scout
from src.models.events import CommonEvent, ToolError


# --------------------------------------------------------------------------- #
# Fakes: a Strands-like agent whose stream mixes assistant text with tool results
# --------------------------------------------------------------------------- #
# Arbitrary model text that MUST NOT leak into the user-facing output. This mirrors
# the reported live bug where the model narrated a fabricated July–October 2025 list.
LEAKY_ASSISTANT_TEXT = [
    "Here are some events I found:",
    "1. Fake AI Meetup — July 2025",
    "2. Imaginary AWS Summit — August 2025",
    "Let me call the search tool now...",
    {"data": "streaming delta text that must be ignored"},
    {"event": {"contentBlockDelta": {"delta": {"text": "more model prose"}}}},
    {"message": {"role": "assistant", "content": [{"text": "assistant narration"}]}},
]


def _sample_events():
    """Two real CommonEvent records as an event source would return them."""
    return [
        CommonEvent(
            title="Agentic AI on AWS",
            start_datetime="2026-10-07T18:30:00Z",
            city="Austin, Texas",
            source_platform="Meetup",
            event_url="https://www.meetup.com/austin-ai/events/301/",
            description="Hands-on agentic AI session.",
            match_confidence=0.9,
            source_invocation_order=0,
        ),
        CommonEvent(
            title="Kubernetes Deep Dive",
            start_datetime="2026-11-03T18:00:00Z",
            city="Austin, Texas",
            source_platform="Luma",
            event_url="https://lu.ma/austin-k8s",
            match_confidence=0.7,
            source_invocation_order=0,
        ),
    ]


class _FakeAgent:
    """Stand-in for a Strands ``Agent`` whose stream yields the given items in order."""

    def __init__(self, stream_items):
        self._stream_items = stream_items

    async def stream_async(self, prompt):
        for item in self._stream_items:
            yield item


def _fake_factory(stream_items):
    """Return an agent_factory(model_id, system_prompt) -> _FakeAgent."""
    return lambda model_id, system_prompt=None: _FakeAgent(stream_items)


def _run(coro):
    return asyncio.run(coro)


async def _collect_handler_output(request):
    """Drive the async-generator handler to completion and join yielded chunks."""
    chunks = []
    async for chunk in scout.handler(request):
        chunks.append(chunk)
    return chunks


# --------------------------------------------------------------------------- #
# _run_agent: text suppressed, tool results collected
# --------------------------------------------------------------------------- #
def test_run_agent_collects_tool_results_and_drops_assistant_text():
    """_run_agent returns only tool results; assistant text/deltas are dropped."""
    events = _sample_events()
    error = ToolError(tool_name="AgentCoreWebSearchEventSource", reason="HTTP 503")
    # Interleave arbitrary model text with the tool results.
    stream = [
        LEAKY_ASSISTANT_TEXT[0],
        events,
        LEAKY_ASSISTANT_TEXT[1],
        LEAKY_ASSISTANT_TEXT[4],
        error,
        LEAKY_ASSISTANT_TEXT[5],
    ]

    outputs = _run(
        scout._run_agent("Agentic AI, AWS", "model-x", agent_factory=_fake_factory(stream))
    )

    # Exactly the two tool results, in order; no text strings collected.
    assert outputs == [events, error]
    assert all(not isinstance(o, str) for o in outputs)


def test_run_agent_ignores_pure_text_stream():
    """A stream containing only assistant text yields zero tool results."""
    outputs = _run(
        scout._run_agent(
            "Agentic AI", "model-x", agent_factory=_fake_factory(LEAKY_ASSISTANT_TEXT)
        )
    )
    assert outputs == []


# --------------------------------------------------------------------------- #
# handler: user-visible output comes only from the deterministic pipeline
# --------------------------------------------------------------------------- #
def test_handler_output_excludes_assistant_text_and_includes_tool_events(monkeypatch):
    """The handler's only output is the deterministic response built from tool results."""
    events = _sample_events()
    stream = [LEAKY_ASSISTANT_TEXT[0], events, LEAKY_ASSISTANT_TEXT[1]]

    monkeypatch.setattr(scout, "_build_agent", lambda model_id, system_prompt: _FakeAgent(stream))
    # Avoid depending on real config/model resolution.
    monkeypatch.setattr(scout.config, "get_model_id", lambda: "model-x")

    chunks = _run(_collect_handler_output({"prompt": "Agentic AI, AWS"}))

    # Exactly one user-facing chunk (the formatted deterministic response).
    assert len(chunks) == 1
    output = chunks[0]

    # Real tool-sourced events are present.
    assert "Agentic AI on AWS" in output
    assert "Kubernetes Deep Dive" in output
    assert "https://www.meetup.com/austin-ai/events/301/" in output

    # None of the arbitrary model text leaks into the output.
    for leak in ("Here are some events I found", "Fake AI Meetup", "July 2025",
                 "Imaginary AWS Summit", "August 2025", "Let me call the search tool"):
        assert leak not in output


def test_handler_surfaces_tool_error_from_stream_without_aborting(monkeypatch):
    """A ToolError collected off the stream is surfaced in the footer (no abort)."""
    error = ToolError(
        tool_name="AgentCoreWebSearchEventSource",
        reason="Tool invocation timed out after 10 seconds",
    )
    stream = ["assistant narration that must be hidden", error]

    monkeypatch.setattr(scout, "_build_agent", lambda model_id, system_prompt: _FakeAgent(stream))
    monkeypatch.setattr(scout.config, "get_model_id", lambda: "model-x")

    chunks = _run(_collect_handler_output({"prompt": "Agentic AI"}))

    assert len(chunks) == 1
    output = chunks[0]
    assert "AgentCoreWebSearchEventSource" in output
    assert "could not be retrieved" in output
    assert "assistant narration that must be hidden" not in output


def test_handler_session_failure_yields_error_and_no_partial_results(monkeypatch):
    """If the agent session fails, a single error message is yielded (no partials)."""

    def _boom(model_id, system_prompt):
        raise RuntimeError("session could not be established")

    monkeypatch.setattr(scout, "_build_agent", _boom)
    monkeypatch.setattr(scout.config, "get_model_id", lambda: "model-x")

    chunks = _run(_collect_handler_output({"prompt": "Agentic AI"}))

    assert len(chunks) == 1
    assert "agent session could not be established" in chunks[0]


def test_handler_validation_failure_short_circuits_before_agent(monkeypatch):
    """An empty prompt is rejected deterministically without building the agent."""
    built = {"called": False}

    def _factory(model_id, system_prompt):
        built["called"] = True
        return _FakeAgent([])

    monkeypatch.setattr(scout, "_build_agent", _factory)
    monkeypatch.setattr(scout.config, "get_model_id", lambda: "model-x")

    chunks = _run(_collect_handler_output({"prompt": ""}))

    assert len(chunks) == 1
    assert built["called"] is False  # agent never constructed on validation failure


# --------------------------------------------------------------------------- #
# _build_agent: Strands' default streaming callback must be disabled
# --------------------------------------------------------------------------- #
class _CapturingAgent:
    """Captures the kwargs passed to the Strands ``Agent`` constructor."""

    last_kwargs = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs


def test_build_agent_disables_default_callback_handler(monkeypatch):
    """_build_agent must pass callback_handler=None so nothing prints to stdout.

    Strands' default callback handler streams model/tool output to stdout; ignoring
    stream_async() events does not disable it. Passing callback_handler=None does.
    """
    _CapturingAgent.last_kwargs = None
    monkeypatch.setattr(scout, "Agent", _CapturingAgent)

    scout._build_agent("model-x", "EFFECTIVE SYSTEM PROMPT")

    kwargs = _CapturingAgent.last_kwargs
    assert kwargs is not None
    # The default streaming callback is explicitly disabled.
    assert "callback_handler" in kwargs
    assert kwargs["callback_handler"] is None
    # Model, prompt, and tool wiring are unchanged. The effective per-invocation
    # system prompt is passed through verbatim.
    assert kwargs["model"] == "model-x"
    assert kwargs["system_prompt"] == "EFFECTIVE SYSTEM PROMPT"
    assert scout.agentcore_web_search_event_source in kwargs["tools"]


def test_build_agent_raises_when_strands_unavailable(monkeypatch):
    """When Strands is unavailable, _build_agent raises (translated to session error)."""
    monkeypatch.setattr(scout, "Agent", None)
    with pytest.raises(RuntimeError, match="Strands Agent is not available"):
        scout._build_agent("model-x", "sp")


# --------------------------------------------------------------------------- #
# Authoritative current date / default window injected per invocation
# --------------------------------------------------------------------------- #
import datetime as _dt


class _FixedDatetime(_dt.datetime):
    """datetime subclass whose now() is pinned to 2026-09-15 12:00 UTC."""

    @classmethod
    def now(cls, tz=None):
        return _dt.datetime(2026, 9, 15, 12, 0, 0, tzinfo=tz or _dt.timezone.utc)


def test_date_context_uses_authoritative_python_date_not_model_knowledge(monkeypatch):
    """The date context is computed in Python from a mocked 'today' of 2026-09-15."""
    monkeypatch.setattr(scout, "datetime", _FixedDatetime)

    ctx = scout._build_date_context()

    assert "CURRENT DATE: 2026-09-15" in ctx
    # Deterministic default 90-day window from the authoritative date.
    assert "2026-09-15 through 2026-12-14" in ctx
    # It instructs the model to resolve relative expressions from the supplied date.
    assert "next 90 days" in ctx
    assert "Do not use any other notion of the current date." in ctx


def test_effective_system_prompt_contains_authoritative_window(monkeypatch):
    """The composed per-invocation system prompt embeds the 2026 date and window."""
    monkeypatch.setattr(scout, "datetime", _FixedDatetime)

    system_prompt = scout._build_system_prompt()

    # Base rules are present...
    assert "You are the Community Scout" in system_prompt
    # ...and the authoritative date/window is appended.
    assert "CURRENT DATE: 2026-09-15" in system_prompt
    assert "DEFAULT 90-DAY WINDOW: 2026-09-15 through 2026-12-14" in system_prompt
    # The stale/model-derived "today" wording is not what drives the window.
    assert "calendar days starting today" not in system_prompt


def test_run_agent_passes_authoritative_system_prompt_to_agent(monkeypatch):
    """_run_agent builds the effective prompt (2026 date) and hands it to the factory."""
    monkeypatch.setattr(scout, "datetime", _FixedDatetime)

    captured = {}

    def _factory(model_id, system_prompt):
        captured["model_id"] = model_id
        captured["system_prompt"] = system_prompt
        return _FakeAgent([])

    _run(
        scout._run_agent(
            "Find Agentic AI, AWS, and Kubernetes community events in Austin "
            "during the next 90 days.",
            "model-x",
            agent_factory=_factory,
        )
    )

    sp = captured["system_prompt"]
    assert "CURRENT DATE: 2026-09-15" in sp
    assert "2026-09-15 through 2026-12-14" in sp


def test_date_context_is_recomputed_per_invocation_not_stale(monkeypatch):
    """Two invocations on different mocked dates yield different date contexts.

    Proves the date is computed per invocation (not frozen at import), so a
    long-running AgentCore Runtime instance never serves a stale date.
    """
    class _Day1(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(2026, 9, 15, tzinfo=tz or _dt.timezone.utc)

    class _Day2(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(2026, 12, 1, tzinfo=tz or _dt.timezone.utc)

    monkeypatch.setattr(scout, "datetime", _Day1)
    ctx1 = scout._build_date_context()
    monkeypatch.setattr(scout, "datetime", _Day2)
    ctx2 = scout._build_date_context()

    assert "CURRENT DATE: 2026-09-15" in ctx1
    assert "CURRENT DATE: 2026-12-01" in ctx2
    assert ctx1 != ctx2


# --------------------------------------------------------------------------- #
# Strands toolResult deserialization (the confirmed live wrapping structure)
# --------------------------------------------------------------------------- #
import json as _json

from src.pipeline.aggregator import aggregate as _aggregate
from src.tools import agentcore_web_search_event_source as _es_module


def _tool_payload(events):
    """Build the JSON payload the Strands tool wrapper emits for a list of events."""
    fields = (
        "title", "start_datetime", "city", "source_platform", "event_url",
        "description", "end_datetime", "location_name", "match_confidence",
        "source_invocation_order",
    )
    return _json.dumps(
        {"status": "success",
         "events": [{f: getattr(e, f) for f in fields} for e in events]}
    )


def _strands_tool_result_event(payload_text):
    """Wrap a JSON payload string in the exact confirmed Strands stream structure."""
    return {
        "message": {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "tooluse_abc123",
                        "status": "success",
                        "content": [{"text": payload_text}],
                    }
                }
            ],
        }
    }


def _agentic_ai_events():
    return [
        CommonEvent(title="Agentic AI Workshop", start_datetime="2026-10-07T18:30:00Z",
                    city="Austin, Texas", source_platform="Meetup",
                    event_url="https://www.meetup.com/austin-ai/events/301/?ref=home",
                    description="Hands-on.", match_confidence=0.9),
        CommonEvent(title="Multi-Agent Systems", start_datetime="2026-10-14T18:30:00Z",
                    city="Austin, Texas", source_platform="Meetup",
                    event_url="https://www.meetup.com/austin-ai/events/302/"),
        CommonEvent(title="Agentic AI on Luma", start_datetime="2026-10-21T18:00:00Z",
                    city="Austin, Texas", source_platform="Luma",
                    event_url="https://lu.ma/austin-agentic"),
    ]


def _aws_events():
    return [
        CommonEvent(title="AWS Cloud Deep Dive", start_datetime="2026-11-03T18:00:00Z",
                    city="Austin, Texas", source_platform="Meetup",
                    event_url="https://www.meetup.com/austin-cloud/events/88/"),
        CommonEvent(title="Serverless on AWS", start_datetime="2026-11-10T18:00:00Z",
                    city="Austin, Texas", source_platform="Luma",
                    event_url="https://lu.ma/austin-serverless"),
    ]


def test_collect_reconstructs_common_events_from_strands_tool_result():
    """A Strands toolResult carrying serialized events yields reconstructed CommonEvents."""
    events = _agentic_ai_events()
    stream_event = _strands_tool_result_event(_tool_payload(events))

    outputs = []
    scout._collect_tool_output(stream_event, outputs)

    assert len(outputs) == 1
    group = outputs[0]
    assert isinstance(group, list)
    assert len(group) == 3
    assert all(isinstance(e, CommonEvent) for e in group)
    assert [e.title for e in group] == [
        "Agentic AI Workshop", "Multi-Agent Systems", "Agentic AI on Luma"
    ]
    # event_url preserved byte-for-byte (including query string).
    assert group[0].event_url == "https://www.meetup.com/austin-ai/events/301/?ref=home"
    # Optional fields preserved.
    assert group[0].description == "Hands-on."
    assert group[0].match_confidence == 0.9


def test_multiple_tool_result_blocks_reach_aggregation():
    """Agentic AI (3), AWS (2), Kubernetes (0) across separate stream events aggregate to 5."""
    outputs = []
    scout._collect_tool_output(
        _strands_tool_result_event(_tool_payload(_agentic_ai_events())), outputs
    )
    scout._collect_tool_output(
        _strands_tool_result_event(_tool_payload(_aws_events())), outputs
    )
    # Kubernetes returned zero events.
    scout._collect_tool_output(
        _strands_tool_result_event(_json.dumps({"status": "success", "events": []})),
        outputs,
    )

    events, errors = _aggregate(outputs)

    assert len(events) == 5
    assert errors == []
    assert [e.title for e in events] == [
        "Agentic AI Workshop", "Multi-Agent Systems", "Agentic AI on Luma",
        "AWS Cloud Deep Dive", "Serverless on AWS",
    ]


def test_empty_events_payload_yields_empty_group():
    """An empty events list is a valid result (no events found), not an error."""
    outputs = []
    scout._collect_tool_output(
        _strands_tool_result_event(_json.dumps({"status": "success", "events": []})),
        outputs,
    )
    assert outputs == [[]]


def test_error_status_payload_becomes_tool_error():
    """A tool payload with status=error is reconstructed as a ToolError."""
    payload = _json.dumps({
        "status": "error",
        "tool_name": "AgentCoreWebSearchEventSource",
        "reason": "Web Search Tool invocation failed: HTTP 503",
    })
    outputs = []
    scout._collect_tool_output(_strands_tool_result_event(payload), outputs)

    assert len(outputs) == 1
    err = outputs[0]
    assert isinstance(err, ToolError)
    assert err.tool_name == "AgentCoreWebSearchEventSource"
    assert "503" in err.reason


def test_malformed_tool_result_text_is_handled_safely_without_eval():
    """A non-JSON toolResult text (e.g. a Python repr) is ignored, never eval'd."""
    # This is exactly the repr-style text that previously leaked through; it must NOT
    # be parsed as Python and must not raise.
    repr_text = "[CommonEvent(title='x', start_datetime='2026-10-07T18:30:00Z')]"
    outputs = []
    scout._collect_tool_output(_strands_tool_result_event(repr_text), outputs)
    assert outputs == []


def test_record_missing_required_field_is_skipped_not_fabricated():
    """A serialized record missing a required field is dropped, not reconstructed."""
    payload = _json.dumps({
        "status": "success",
        "events": [
            {  # missing 'city'
                "title": "No City", "start_datetime": "2026-10-07T18:30:00Z",
                "source_platform": "Meetup", "event_url": "https://www.meetup.com/x/",
            },
            {  # valid
                "title": "Valid", "start_datetime": "2026-10-08T18:30:00Z",
                "city": "Austin, Texas", "source_platform": "Luma",
                "event_url": "https://lu.ma/valid",
            },
        ],
    })
    outputs = []
    scout._collect_tool_output(_strands_tool_result_event(payload), outputs)

    assert len(outputs) == 1
    group = outputs[0]
    assert [e.title for e in group] == ["Valid"]  # invalid record skipped


def test_handler_end_to_end_reconstructs_events_from_strands_wrapper(monkeypatch):
    """Full handler: Strands toolResult -> reconstructed events -> deterministic output."""
    stream = [
        "model narration that must never be shown to the user",
        _strands_tool_result_event(_tool_payload(_agentic_ai_events())),
        _strands_tool_result_event(_tool_payload(_aws_events())),
    ]
    monkeypatch.setattr(scout, "_build_agent", lambda model_id, system_prompt: _FakeAgent(stream))
    monkeypatch.setattr(scout.config, "get_model_id", lambda: "model-x")

    chunks = _run(_collect_handler_output({"prompt": "Agentic AI, AWS"}))

    assert len(chunks) == 1
    output = chunks[0]
    # Reconstructed, tool-sourced events appear in the deterministic response.
    assert "Agentic AI Workshop" in output
    assert "AWS Cloud Deep Dive" in output
    assert "https://www.meetup.com/austin-ai/events/301/?ref=home" in output
    # Model narration never leaks.
    assert "model narration that must never be shown" not in output


def test_tool_wrapper_returns_json_string_success_payload(monkeypatch):
    """The Strands tool wrapper returns a JSON STRING (double-quoted), not a dict/repr."""
    events = _agentic_ai_events()
    monkeypatch.setattr(
        _es_module.AgentCoreWebSearchEventSource,
        "__call__",
        lambda self, topics, start_date, end_date: events,
    )
    result = _es_module.agentcore_web_search_event_source(
        ["Agentic AI"], "2026-09-15", "2026-12-14"
    )
    # 1) The tool returns a JSON STRING (this is what Strands puts in
    #    toolResult.content[].text) — valid JSON with double quotes, not a Python repr.
    assert isinstance(result, str)
    assert result.startswith('{"status": "success"')
    assert "'" not in result  # no single-quoted Python-dict syntax.

    # 2) json.loads(return_value) succeeds (the collector uses exactly this).
    payload = _json.loads(result)
    assert payload["status"] == "success"
    assert len(payload["events"]) == 3
    assert payload["events"][0]["event_url"] == (
        "https://www.meetup.com/austin-ai/events/301/?ref=home"
    )
    assert payload["events"][0]["title"] == "Agentic AI Workshop"


def test_tool_wrapper_serializes_tool_error_as_json_string(monkeypatch):
    """The Strands tool wrapper serializes a ToolError to a JSON error STRING."""
    monkeypatch.setattr(
        _es_module.AgentCoreWebSearchEventSource,
        "__call__",
        lambda self, topics, start_date, end_date: ToolError(
            tool_name="AgentCoreWebSearchEventSource", reason="timeout after 10s"
        ),
    )
    result = _es_module.agentcore_web_search_event_source(
        ["Agentic AI"], "2026-09-15", "2026-12-14"
    )
    assert isinstance(result, str)
    assert _json.loads(result) == {
        "status": "error",
        "tool_name": "AgentCoreWebSearchEventSource",
        "reason": "timeout after 10s",
    }


# --------------------------------------------------------------------------- #
# Full boundary chain: domain source -> JSON-safe wrapper -> Strands toolResult
# -> _collect_tool_output -> list[CommonEvent]
# --------------------------------------------------------------------------- #
def _wrapper_json_string_via_domain(monkeypatch, domain_result):
    """Run the registered Strands wrapper with the domain __call__ patched to a result.

    Returns the JSON STRING the wrapper produces — this is exactly what Strands places
    into ``toolResult.content[].text``.
    """
    monkeypatch.setattr(
        _es_module.AgentCoreWebSearchEventSource,
        "__call__",
        lambda self, topics, start_date, end_date: domain_result,
    )
    return _es_module.agentcore_web_search_event_source(
        ["Agentic AI"], "2026-09-15", "2026-12-14"
    )


def _chain_collect(monkeypatch, domain_result):
    """Full chain: wrapper -> JSON string -> Strands toolResult event -> collector.

    Returns ``(envelope_dict, outputs)`` where ``envelope_dict`` is the parsed JSON the
    wrapper emitted and ``outputs`` is what the collector reconstructed.
    """
    text = _wrapper_json_string_via_domain(monkeypatch, domain_result)
    # The wrapper's JSON string is used VERBATIM as the toolResult text — no re-dumping;
    # this mirrors the real Strands serialization boundary.
    assert isinstance(text, str)
    envelope = _json.loads(text)
    stream_event = _strands_tool_result_event(text)
    outputs = []
    scout._collect_tool_output(stream_event, outputs)
    return envelope, outputs


def test_chain_multiple_events_roundtrip(monkeypatch):
    """Multiple domain events survive the wrapper->JSON->toolResult->collector chain."""
    domain = _agentic_ai_events()
    envelope, outputs = _chain_collect(monkeypatch, domain)

    assert envelope["status"] == "success"
    assert len(envelope["events"]) == 3
    assert len(outputs) == 1
    reconstructed = outputs[0]
    assert all(isinstance(e, CommonEvent) for e in reconstructed)
    assert [e.title for e in reconstructed] == [e.title for e in domain]


def test_chain_preserves_event_url_exactly(monkeypatch):
    """The event_url is preserved byte-for-byte across the entire boundary chain."""
    url = "https://www.meetup.com/austin-ai/events/301/?ref=home&x=1#frag"
    domain = [
        CommonEvent(title="URL Fidelity", start_datetime="2026-10-07T18:30:00Z",
                    city="Austin, Texas", source_platform="Meetup", event_url=url)
    ]
    envelope, outputs = _chain_collect(monkeypatch, domain)

    assert envelope["events"][0]["event_url"] == url
    assert outputs[0][0].event_url == url


def test_chain_optional_fields_included_only_when_present(monkeypatch):
    """Optional fields appear in the envelope only when set; reconstruct correctly."""
    minimal = CommonEvent(
        title="Minimal", start_datetime="2026-10-07T18:30:00Z", city="Austin, Texas",
        source_platform="Meetup", event_url="https://www.meetup.com/min/",
    )
    full = CommonEvent(
        title="Full", start_datetime="2026-10-08T18:30:00Z", city="Austin, Texas",
        source_platform="Luma", event_url="https://lu.ma/full",
        description="desc", end_datetime="2026-10-08T20:30:00Z",
        location_name="Online", match_confidence=0.8, source_invocation_order=2,
    )
    envelope, outputs = _chain_collect(monkeypatch, [minimal, full])

    min_dict, full_dict = envelope["events"]
    # Minimal: the None-valued optional fields are omitted from the envelope.
    assert "description" not in min_dict
    assert "end_datetime" not in min_dict
    assert "location_name" not in min_dict
    # Full: all optional fields present.
    assert full_dict["description"] == "desc"
    assert full_dict["end_datetime"] == "2026-10-08T20:30:00Z"
    assert full_dict["location_name"] == "Online"

    # Reconstruction preserves them (and defaults the omitted ones to None).
    r_min, r_full = outputs[0]
    assert r_min.description is None
    assert r_min.location_name is None
    assert r_full.description == "desc"
    assert r_full.end_datetime == "2026-10-08T20:30:00Z"
    assert r_full.location_name == "Online"
    assert r_full.match_confidence == 0.8
    assert r_full.source_invocation_order == 2


def test_chain_empty_events(monkeypatch):
    """An empty domain result becomes {status:success, events:[]} -> empty group."""
    envelope, outputs = _chain_collect(monkeypatch, [])
    assert envelope == {"status": "success", "events": []}
    assert outputs == [[]]


def test_chain_tool_error(monkeypatch):
    """A domain ToolError becomes {status:error,...} -> reconstructed ToolError."""
    domain_err = ToolError(
        tool_name="AgentCoreWebSearchEventSource",
        reason="Tool invocation timed out after 10 seconds",
    )
    envelope, outputs = _chain_collect(monkeypatch, domain_err)

    assert envelope == {
        "status": "error",
        "tool_name": "AgentCoreWebSearchEventSource",
        "reason": "Tool invocation timed out after 10 seconds",
    }
    assert len(outputs) == 1
    assert isinstance(outputs[0], ToolError)
    assert outputs[0].tool_name == "AgentCoreWebSearchEventSource"
    assert "timed out" in outputs[0].reason


def test_build_agent_registers_json_safe_wrapper(monkeypatch):
    """_build_agent registers the JSON-safe wrapper (JSON-string-returning), not a raw list tool.

    Guards the MOST IMPORTANT requirement: the tool the agent invokes must return the
    JSON string the collector expects, not raw list[CommonEvent].
    """
    _CapturingAgent.last_kwargs = None
    monkeypatch.setattr(scout, "Agent", _CapturingAgent)

    scout._build_agent("model-x", "sp")

    registered = _CapturingAgent.last_kwargs["tools"]
    assert scout.agentcore_web_search_event_source in registered

    # Invoking the registered tool with a patched domain result yields a JSON STRING
    # (exactly what Strands places into toolResult.content[].text).
    monkeypatch.setattr(
        _es_module.AgentCoreWebSearchEventSource,
        "__call__",
        lambda self, t, s, e: _agentic_ai_events(),
    )
    result = scout.agentcore_web_search_event_source(
        ["Agentic AI"], "2026-09-15", "2026-12-14"
    )
    assert isinstance(result, str)
    # It is valid JSON that json.loads (as the collector uses) can parse.
    payload = _json.loads(result)
    assert payload["status"] == "success"


def test_wrapper_output_is_valid_json_not_python_repr(monkeypatch):
    """Regression: the tool return must be valid JSON, not a Python repr dict string.

    The live bug was that Strands placed a Python ``repr`` of a dict
    (``{'status': 'success', ...}`` with single quotes) into
    ``toolResult.content[].text``, so the collector's ``json.loads`` failed. The wrapper
    now returns ``json.dumps(payload)``, so its return value parses cleanly and does not
    contain single-quoted Python-dict syntax.
    """
    events = _agentic_ai_events()
    monkeypatch.setattr(
        _es_module.AgentCoreWebSearchEventSource,
        "__call__",
        lambda self, t, s, e: events,
    )
    text = _es_module.agentcore_web_search_event_source(
        ["Agentic AI"], "2026-09-15", "2026-12-14"
    )

    # A Python repr of the equivalent dict would raise here; valid JSON does not.
    parsed = _json.loads(text)
    assert parsed["status"] == "success"

    # Feeding this exact text through the Strands toolResult structure reconstructs events.
    outputs = []
    scout._collect_tool_output(_strands_tool_result_event(text), outputs)
    assert len(outputs) == 1
    assert [e.title for e in outputs[0]] == [e.title for e in events]
    # event_url preserved exactly.
    assert outputs[0][0].event_url == events[0].event_url
