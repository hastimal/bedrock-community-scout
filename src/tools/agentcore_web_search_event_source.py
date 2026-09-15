"""
AgentCoreWebSearchEventSource: the v0.1 Event_Source_Tool implementation.

This module implements :class:`AgentCoreWebSearchEventSource`, an Event_Source_Tool
that satisfies the ``EventSourceTool`` Protocol defined in ``src/tools/interface.py``.

It retrieves publicly indexed event pages from meetup.com and lu.ma using the Amazon
Bedrock AgentCore Web Search Tool, exposed as an MCP connector. Snippets returned by
the Web Search Tool are parsed into raw event records, normalized into
``CommonEvent`` instances via :func:`src.tools.normalizer.normalize_event`, and
post-filtered by verified event start date so that only events inside the requested
time window are returned.

**Isolation guarantees (Requirement 3.8):**
All Web Search Tool configuration — the gateway URL, the domain filter list, the
search-query template, and the time-window/date-filter construction — is confined to
this class. To add a new source platform (e.g., Eventbrite), extend ``_DOMAIN_FILTER``
inside this class; no other component needs to change.

**Factual accuracy (Requirements 3.3, 9.1):**
Event fields are extracted using only facts explicitly present in a snippet's text or
derivable from its source URL. If any required field cannot be reliably extracted, the
record is excluded — never inferred, fabricated, or substituted.

**Error handling (Requirement 3.7):**
On any Web Search Tool error, unavailability, or timeout, a ``ToolError`` is returned.
Partial event records are never returned alongside an error, and exceptions are never
raised out of the tool entry point — failures are encoded in the return value.
"""

import re
import logging
from typing import Any, Optional
from urllib.parse import urlparse

from src.models.events import CommonEvent, ToolError
from src.tools.normalizer import normalize_event

# Strands is a runtime dependency (see requirements.txt: strands-agents). The tool
# decorator and MCP client are imported at module load. When Strands is unavailable
# (e.g., a lint-only environment), fall back to lightweight shims so the module remains
# importable and syntactically valid; the real Strands runtime supplies the actual
# implementations in production.
try:  # pragma: no cover - exercised only in environments with Strands installed
    from strands import tool
    from strands.tools.mcp import MCPClient
except ImportError:  # pragma: no cover - fallback for environments without Strands
    MCPClient = None  # type: ignore[assignment]

    def tool(func):  # type: ignore[no-redef]
        """No-op fallback for the Strands ``@tool`` decorator.

        Used only when ``strands-agents`` is not installed. Returns the function
        unchanged so the module can be imported for static analysis and unit tests
        that patch the Web Search interaction.
        """
        return func


logger = logging.getLogger(__name__)


# Timeout budget for a single Web Search Tool invocation (Requirement 2.1 / 3.7).
_TOOL_TIMEOUT_SECONDS = 10


class AgentCoreWebSearchEventSource:
    """
    Event_Source_Tool backed by the Amazon Bedrock AgentCore Web Search Tool.

    Implements the ``EventSourceTool`` Protocol
    (``(topics, start_date, end_date) -> list[CommonEvent] | ToolError``). The Location
    is fixed to Austin, Texas for v0.1 and is intentionally NOT a caller-supplied
    parameter — it is embedded internally in the constructed search query.

    All Web Search Tool configuration is isolated inside this class:

    - ``_GATEWAY_URL``: AgentCore Gateway Web Search MCP connector endpoint.
    - ``_WEB_SEARCH_TOOL_NAME``: MCP tool name to invoke.
    - ``_DOMAIN_FILTER``: domain include list — extend this to add sources.
    - ``_QUERY_TEMPLATE``: search-query text template embedding topics + time window.

    Instances are callable; the module-level :func:`agentcore_web_search_event_source`
    is the ``@tool``-decorated entry point AgentCore invokes.
    """

    #: The fixed Location for v0.1. Not a caller parameter.
    _LOCATION = "Austin, Texas"

    #: AgentCore Gateway Web Search MCP connector endpoint. Read from the environment so
    #: it can be overridden per deployment without code changes; falls back to the
    #: managed default gateway URL.
    _GATEWAY_URL_ENV_VAR = "AGENTCORE_WEB_SEARCH_GATEWAY_URL"
    _DEFAULT_GATEWAY_URL = "https://gateway.bedrock-agentcore.amazonaws.com/web-search/mcp"

    #: The MCP tool name exposed by the Web Search connector.
    _WEB_SEARCH_TOOL_NAME = "WebSearch"

    #: Domain filter applied to every Web Search Tool call. Extend the include list to
    #: add new source platforms (Requirement 3.8).
    _DOMAIN_FILTER = {"include": ["meetup.com", "lu.ma"]}

    #: Query text template. The time window is embedded here (Requirement 3.2); the page
    #: publication date is never used as a substitute for the event date.
    _QUERY_TEMPLATE = "{topics} events in {location} between {start_date} and {end_date}"

    #: Mapping from source URL domain to canonical platform name (Requirement 9.2).
    _PLATFORM_BY_DOMAIN = {
        "meetup.com": "Meetup",
        "lu.ma": "Luma",
    }

    def __init__(self, gateway_url: Optional[str] = None):
        """
        Args:
            gateway_url: Optional override for the Web Search MCP gateway URL. When
                omitted, the ``AGENTCORE_WEB_SEARCH_GATEWAY_URL`` environment variable is
                consulted, falling back to the managed default.
        """
        import os

        self._gateway_url = (
            gateway_url
            or os.environ.get(self._GATEWAY_URL_ENV_VAR)
            or self._DEFAULT_GATEWAY_URL
        )

    # ------------------------------------------------------------------ #
    # EventSourceTool Protocol entry point
    # ------------------------------------------------------------------ #
    def __call__(
        self,
        topics: list[str],
        start_date: str,
        end_date: str,
    ) -> "list[CommonEvent] | ToolError":
        """
        Fetch Austin, Texas tech events matching ``topics`` within the time window.

        Args:
            topics: 1–10 topic strings (each 1–200 chars). Example: ["Agentic AI", "AWS"].
            start_date: Inclusive time-window start, ISO 8601 date (YYYY-MM-DD).
            end_date: Inclusive time-window end, ISO 8601 date (YYYY-MM-DD).

        Returns:
            list[CommonEvent]: Zero or more normalized events whose verified start date
                falls within ``[start_date, end_date]``. Returns an empty list when the
                Web Search Tool yields no matching results.
            ToolError: When the Web Search Tool errors out, is unavailable, or times out.
                No partial records accompany a ToolError.
        """
        try:
            raw_results = self._invoke_web_search(topics, start_date, end_date)
        except Exception as exc:  # noqa: BLE001 - errors must be encoded, not raised
            reason = f"Web Search Tool invocation failed: {exc}"
            logger.warning(
                "AgentCoreWebSearchEventSource error for topics=%s: %s", topics, reason
            )
            return ToolError(tool_name="AgentCoreWebSearchEventSource", reason=reason)

        # No results for the query/domain filter -> empty list (Requirement 3.6).
        if not raw_results:
            return []

        events: list[CommonEvent] = []
        for result in raw_results:
            raw_event = self._extract_raw_event(result)
            if raw_event is None:
                # Required field missing/ambiguous in the evidence: exclude, never
                # fabricate (Requirements 3.3, 9.1).
                continue

            source_platform = self._derive_platform(raw_event.get("event_url", ""))
            if source_platform is None:
                # URL not from a configured domain -> cannot derive platform reliably.
                continue

            match_confidence = self._extract_confidence(result)

            event = normalize_event(
                raw_event=raw_event,
                source_platform=source_platform,
                source_invocation_order=0,
                match_confidence=match_confidence,
                event_identifier=raw_event.get("title"),
            )
            if event is None:
                # normalize_event already logged the exclusion reason.
                continue

            # Post-filter by verified event start date (Requirements 3.2, 3.5). The page
            # publication date is never used as a substitute for the event start date.
            if self._is_within_window(event.start_datetime, start_date, end_date):
                events.append(event)

        return events

    # ------------------------------------------------------------------ #
    # Web Search Tool invocation (isolated configuration)
    # ------------------------------------------------------------------ #
    def _build_query(self, topics: list[str], start_date: str, end_date: str) -> str:
        """Construct the search-query text embedding topics, location, and time window."""
        topic_text = " ".join(t.strip() for t in topics if t and t.strip())
        return self._QUERY_TEMPLATE.format(
            topics=topic_text,
            location=self._LOCATION,
            start_date=start_date,
            end_date=end_date,
        )

    def _build_arguments(self, topics: list[str], start_date: str, end_date: str) -> dict:
        """Assemble the MCP ``tools/call`` arguments, including the domain filter.

        The time window is embedded in the query text only. ``publishedDateFilter`` is
        intentionally NOT set, because a page's publication date is not the event date
        (Requirement 3.2).
        """
        return {
            "query": self._build_query(topics, start_date, end_date),
            "filters": {"domainFilter": self._DOMAIN_FILTER},
        }

    def _invoke_web_search(
        self, topics: list[str], start_date: str, end_date: str
    ) -> list[dict]:
        """Invoke the Web Search Tool over MCP and return the raw result dicts.

        Raises:
            RuntimeError: If the Strands MCP client is unavailable in this environment.
            Exception: Any transport/tool error is allowed to propagate; the caller
                converts it into a ``ToolError``.
        """
        if MCPClient is None:
            raise RuntimeError(
                "Strands MCPClient is not available; install 'strands-agents' to invoke "
                "the AgentCore Web Search Tool."
            )

        arguments = self._build_arguments(topics, start_date, end_date)

        # The MCPClient manages the connection lifecycle to the AgentCore Gateway Web
        # Search connector. The client is used as a context manager so the session is
        # torn down after the call.
        client = MCPClient(lambda: self._create_mcp_transport())
        with client:
            response = client.call_tool_sync(
                tool_use_id="agentcore-web-search",
                name=self._WEB_SEARCH_TOOL_NAME,
                arguments=arguments,
                read_timeout_seconds=_TOOL_TIMEOUT_SECONDS,
            )
        return self._parse_search_results(response)

    #: AWS region and service for SigV4-signed requests to the AgentCore Gateway.
    _AWS_REGION = "us-east-1"
    _AWS_SERVICE = "bedrock-agentcore"

    def _create_mcp_transport(self):
        """Create the MCP transport to the AgentCore Gateway Web Search connector.

        Isolated here so the transport/gateway wiring can change without affecting the
        rest of the class. The deployed AgentCore Gateway uses ``AuthorizerType: AWS_IAM``,
        so requests must be SigV4-signed. This uses the AWS-supported SigV4 MCP transport
        (``aws_iam_streamablehttp_client``), which signs each streamable-HTTP request with
        the caller's AWS credentials for the ``bedrock-agentcore`` service in ``us-east-1``.
        The AgentCore Web Search Tool is a managed AWS service; no API keys or third-party
        credentials are required.
        """
        from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client

        return aws_iam_streamablehttp_client(
            endpoint=self._gateway_url,
            aws_region=self._AWS_REGION,
            aws_service=self._AWS_SERVICE,
        )

    # ------------------------------------------------------------------ #
    # Snippet parsing / field extraction
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_search_results(response: Any) -> list[dict]:
        """Normalize the MCP tool response into a flat list of result dicts.

        The Web Search Tool returns content blocks; each JSON block carries a list of
        results with ``title``, ``url``, ``snippet``, and ``publishedDate`` fields.
        Tolerant of both structured-content and text-content responses.
        """
        if response is None:
            return []

        # Dict-shaped responses (e.g., {"content": [...]}) or already-flat lists.
        content = response
        if isinstance(response, dict):
            content = response.get("content", response.get("results", []))

        results: list[dict] = []

        # A list of content blocks or plain result dicts.
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                # Structured JSON content block.
                if "json" in block and isinstance(block["json"], dict):
                    inner = block["json"].get("results", [])
                    if isinstance(inner, list):
                        results.extend(r for r in inner if isinstance(r, dict))
                    continue
                # Text content block containing serialized JSON.
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    import json

                    try:
                        parsed = json.loads(block["text"])
                    except (ValueError, TypeError):
                        continue
                    if isinstance(parsed, dict):
                        inner = parsed.get("results", [])
                        if isinstance(inner, list):
                            results.extend(r for r in inner if isinstance(r, dict))
                    elif isinstance(parsed, list):
                        results.extend(r for r in parsed if isinstance(r, dict))
                    continue
                # A plain result dict already shaped like a search result.
                if "url" in block or "title" in block:
                    results.append(block)

        return results

    def _extract_raw_event(self, result: dict) -> Optional[dict]:
        """Extract a raw event dict from a single Web Search result.

        Uses only facts explicitly present in the snippet text and its source URL. Any
        required field (title, start_datetime, city, event_url) that cannot be reliably
        extracted causes the result to be excluded (returns ``None``). No inference or
        substitution is performed (Requirements 3.3, 9.1).
        """
        if not isinstance(result, dict):
            return None

        event_url = self._as_str(result.get("url"))
        title = self._as_str(result.get("title"))
        snippet = self._as_str(result.get("snippet")) or ""

        # event_url and title are required and come directly from the search result.
        if not event_url or not title:
            return None

        # Extract the event start date/time from the snippet text only.
        start_datetime = self._extract_start_datetime(snippet)
        if not start_datetime:
            return None

        # City must be explicitly present in the evidence (snippet). v0.1 targets Austin.
        city = self._extract_city(snippet)
        if not city:
            return None

        raw_event: dict = {
            "title": title,
            "start_datetime": start_datetime,
            "city": city,
            "event_url": event_url,
        }

        # Optional fields — included only when present in the evidence.
        description = snippet.strip()
        if description:
            raw_event["description"] = description

        location_name = self._extract_location_name(snippet)
        if location_name:
            raw_event["location_name"] = location_name

        end_datetime = self._extract_end_datetime(snippet)
        if end_datetime:
            raw_event["end_datetime"] = end_datetime

        return raw_event

    # ------------------------------------------------------------------ #
    # Extraction helpers (evidence-only; no fabrication)
    # ------------------------------------------------------------------ #
    # Matches common date expressions in snippet text, e.g.
    #   "2025-09-15T18:00:00Z", "2025-09-15 18:00", "2025-09-15",
    #   "September 15, 2025 at 6:00 PM"
    _ISO_DATETIME_RE = re.compile(
        r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
    )
    _LONG_DATE_RE = re.compile(
        r"\b(?:January|February|March|April|May|June|July|August|September|October|"
        r"November|December)\s+\d{1,2},\s+\d{4}(?:\s+at\s+\d{1,2}:\d{2}\s*(?:AM|PM))?\b",
        re.IGNORECASE,
    )

    def _extract_start_datetime(self, snippet: str) -> Optional[str]:
        """Extract the first date/datetime expression present in the snippet, if any.

        Returns the raw matched string; :func:`normalize_event` converts it to ISO 8601
        UTC. Returns ``None`` when no date is present so the record is excluded.
        """
        if not snippet:
            return None
        iso_match = self._ISO_DATETIME_RE.search(snippet)
        if iso_match:
            return iso_match.group(0)
        long_match = self._LONG_DATE_RE.search(snippet)
        if long_match:
            return long_match.group(0)
        return None

    def _extract_end_datetime(self, snippet: str) -> Optional[str]:
        """Extract a second date expression as the end datetime, when clearly present."""
        if not snippet:
            return None
        matches = self._ISO_DATETIME_RE.findall(snippet)
        if len(matches) >= 2:
            return matches[1]
        return None

    def _extract_city(self, snippet: str) -> Optional[str]:
        """Extract the city from the snippet, when explicitly present.

        v0.1 targets Austin, Texas; recognize an explicit Austin mention in the evidence.
        Returns ``None`` when no city is stated, so the record is excluded rather than
        assuming a location (Requirement 9.1).
        """
        if not snippet:
            return None
        if re.search(r"\bAustin\b", snippet, re.IGNORECASE):
            return self._LOCATION
        return None

    @staticmethod
    def _extract_location_name(snippet: str) -> Optional[str]:
        """Extract an explicit venue/location name from the snippet when present.

        Recognizes an explicit "Online" designation. Other venue extraction is left to
        richer extractors; absence yields ``None`` (optional field).
        """
        if not snippet:
            return None
        if re.search(r"\bOnline\b", snippet, re.IGNORECASE):
            return "Online"
        return None

    def _derive_platform(self, event_url: str) -> Optional[str]:
        """Derive the canonical platform name from the event URL domain.

        Returns "Meetup" for meetup.com and "Luma" for lu.ma. Returns ``None`` for any
        domain not in the configured filter so unexpected sources are excluded.
        """
        if not event_url:
            return None
        try:
            host = (urlparse(event_url).hostname or "").lower()
        except ValueError:
            return None
        if not host:
            return None
        for domain, platform in self._PLATFORM_BY_DOMAIN.items():
            if host == domain or host.endswith("." + domain):
                return platform
        return None

    @staticmethod
    def _extract_confidence(result: dict) -> float:
        """Extract a match confidence in [0.0, 1.0] from the result, defaulting to 0.0."""
        raw = result.get("match_confidence", result.get("score"))
        if isinstance(raw, (int, float)):
            value = float(raw)
            if 0.0 <= value <= 1.0:
                return value
        return 0.0

    @staticmethod
    def _is_within_window(start_datetime: str, start_date: str, end_date: str) -> bool:
        """Return True if the ISO 8601 UTC ``start_datetime`` date is in [start, end]."""
        # start_datetime is guaranteed ISO 8601 UTC (YYYY-MM-DDTHH:MM:SSZ) by normalize.
        event_date = start_datetime[:10]
        return start_date <= event_date <= end_date

    @staticmethod
    def _as_str(value: Any) -> Optional[str]:
        """Return a stripped string if ``value`` is a non-empty string, else ``None``."""
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None


# Module-level singleton reused across invocations.
_event_source = AgentCoreWebSearchEventSource()


@tool
def agentcore_web_search_event_source(
    topics: list[str],
    start_date: str,
    end_date: str,
) -> "list[CommonEvent] | ToolError":
    """
    Search meetup.com and lu.ma for Austin, Texas tech events matching the given topics.

    This is the AgentCore-invokable entry point for the v0.1 Event_Source_Tool. The
    Location is fixed to Austin, Texas and is not a parameter.

    Args:
        topics: 1–10 topic strings (each 1–200 chars), e.g. ["Agentic AI", "AWS"].
        start_date: Inclusive time-window start as an ISO 8601 date (YYYY-MM-DD).
        end_date: Inclusive time-window end as an ISO 8601 date (YYYY-MM-DD).

    Returns:
        A list of normalized ``CommonEvent`` records whose start date falls within the
        time window, or a ``ToolError`` if the Web Search Tool fails or times out.
    """
    return _event_source(topics, start_date, end_date)
