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
