"""
Unit tests for the AgentCore Gateway MCP transport wiring.

These tests pin the SigV4 transport configuration used by
``AgentCoreWebSearchEventSource._create_mcp_transport``. The deployed AgentCore
Gateway uses ``AuthorizerType: AWS_IAM``, so the transport must be the AWS-supported
SigV4 streamable-HTTP client (``aws_iam_streamablehttp_client``) configured for the
``bedrock-agentcore`` service in ``us-east-1``, using the configurable gateway URL.

Only the transport/authentication wiring is exercised here — event models,
normalization, ranking, deduplication, query construction, domain filters, and
response formatting are covered by their own tests and intentionally not touched.
"""

import sys
import types

import pytest

from src.tools.agentcore_web_search_event_source import AgentCoreWebSearchEventSource


@pytest.fixture
def captured_transport(monkeypatch):
    """Install a fake ``aws_iam_streamablehttp_client`` and capture its call kwargs.

    Replaces the ``mcp_proxy_for_aws.client`` module with a stub so the test never
    performs real signing or network I/O, and records the keyword arguments passed
    by ``_create_mcp_transport``.
    """
    calls = {}
    sentinel = object()

    def fake_client(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return sentinel

    fake_client_module = types.ModuleType("mcp_proxy_for_aws.client")
    fake_client_module.aws_iam_streamablehttp_client = fake_client
    fake_pkg = types.ModuleType("mcp_proxy_for_aws")
    fake_pkg.client = fake_client_module

    monkeypatch.setitem(sys.modules, "mcp_proxy_for_aws", fake_pkg)
    monkeypatch.setitem(sys.modules, "mcp_proxy_for_aws.client", fake_client_module)

    return calls, sentinel


def test_transport_uses_sigv4_client_with_expected_service_and_region(captured_transport):
    """Transport is built via aws_iam_streamablehttp_client for bedrock-agentcore/us-east-1."""
    calls, sentinel = captured_transport

    source = AgentCoreWebSearchEventSource(gateway_url="https://example-gateway/mcp")
    transport = source._create_mcp_transport()

    assert transport is sentinel, "transport must be the SigV4 client's return value"

    kwargs = calls["kwargs"]
    assert kwargs["aws_service"] == "bedrock-agentcore"
    assert kwargs["aws_region"] == "us-east-1"
    # No positional args — all configuration passed by keyword.
    assert calls["args"] == ()


def test_transport_endpoint_uses_configured_gateway_url(captured_transport):
    """The endpoint passed to the SigV4 client is the configured gateway URL."""
    calls, _ = captured_transport
    url = "https://my-iam-gateway.example.com/community-scout/mcp"

    source = AgentCoreWebSearchEventSource(gateway_url=url)
    source._create_mcp_transport()

    assert calls["kwargs"]["endpoint"] == url


def test_transport_endpoint_honors_env_var(captured_transport, monkeypatch):
    """The gateway URL remains configurable via AGENTCORE_WEB_SEARCH_GATEWAY_URL."""
    calls, _ = captured_transport
    env_url = "https://env-configured-gateway.example.com/mcp"
    monkeypatch.setenv("AGENTCORE_WEB_SEARCH_GATEWAY_URL", env_url)

    # No explicit gateway_url -> falls back to the environment variable.
    source = AgentCoreWebSearchEventSource()
    source._create_mcp_transport()

    assert calls["kwargs"]["endpoint"] == env_url


def test_call_tool_timeout_is_timedelta_of_ten_seconds(monkeypatch):
    """read_timeout_seconds must be a datetime.timedelta of 10s, not a bare int.

    Regression guard: the installed MCP/Strands session calls
    ``read_timeout_seconds.total_seconds()``, so passing an ``int`` raises
    ``AttributeError: 'int' object has no attribute 'total_seconds'`` during a live
    invocation. This test captures the argument actually passed to
    ``MCPClient.call_tool_sync`` and asserts it is a ``timedelta`` equal to the
    ``_TOOL_TIMEOUT_SECONDS`` budget.
    """
    from datetime import timedelta

    from src.tools import agentcore_web_search_event_source as event_source_module

    captured = {}

    class _CapturingMCPClient:
        def __init__(self, transport_factory):
            self._transport_factory = transport_factory

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def call_tool_sync(self, *args, **kwargs):
            captured["read_timeout_seconds"] = kwargs.get("read_timeout_seconds")
            # Return an empty response so parsing yields no results and no error.
            return {"content": []}

    # Swap in the capturing client and stub the transport so no real signing/IO occurs.
    monkeypatch.setattr(
        event_source_module, "MCPClient", _CapturingMCPClient, raising=False
    )
    monkeypatch.setattr(
        AgentCoreWebSearchEventSource,
        "_create_mcp_transport",
        lambda self: object(),
    )

    source = AgentCoreWebSearchEventSource(gateway_url="https://example-gateway/mcp")
    result = source(["Agentic AI"], "2025-01-01", "2025-12-31")

    # Invocation completed without raising and produced no events for the empty response.
    assert result == []

    timeout_arg = captured["read_timeout_seconds"]
    assert isinstance(timeout_arg, timedelta), (
        "read_timeout_seconds must be a datetime.timedelta so the MCP session can call "
        "total_seconds() on it"
    )
    assert timeout_arg == timedelta(
        seconds=event_source_module._TOOL_TIMEOUT_SECONDS
    )
    assert timeout_arg.total_seconds() == 10


def test_call_tool_uses_gateway_namespaced_tool_name(monkeypatch):
    """The MCP tool name must match the deployed Gateway's tools/list entry.

    The live AgentCore Gateway namespaces the managed Web Search connector tool with
    the target name, exposing it as ``community-scout-web-search___WebSearch`` (not the
    bare ``WebSearch``). This test captures the ``name`` argument passed to
    ``MCPClient.call_tool_sync`` and asserts it matches the deployed name.
    """
    from src.tools import agentcore_web_search_event_source as event_source_module

    captured = {}

    class _CapturingMCPClient:
        def __init__(self, transport_factory):
            self._transport_factory = transport_factory

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def call_tool_sync(self, *args, **kwargs):
            captured["name"] = kwargs.get("name")
            return {"content": []}

    monkeypatch.setattr(
        event_source_module, "MCPClient", _CapturingMCPClient, raising=False
    )
    monkeypatch.setattr(
        AgentCoreWebSearchEventSource,
        "_create_mcp_transport",
        lambda self: object(),
    )

    source = AgentCoreWebSearchEventSource(gateway_url="https://example-gateway/mcp")
    result = source(["Agentic AI"], "2025-01-01", "2025-12-31")

    assert result == []
    assert captured["name"] == "community-scout-web-search___WebSearch"
    # The class constant is the single source of truth for the tool name.
    assert (
        AgentCoreWebSearchEventSource._WEB_SEARCH_TOOL_NAME
        == "community-scout-web-search___WebSearch"
    )
