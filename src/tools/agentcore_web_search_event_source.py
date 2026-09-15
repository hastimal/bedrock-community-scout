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
import json
import logging
from datetime import timedelta
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

    #: TEMPORARY diagnostic prefix. Every discovery-diagnostic INFO log line starts with
    #: this marker so it is easy to grep in the AgentCore Runtime CloudWatch logs. This
    #: is DIAGNOSTIC-ONLY and does not affect any search/extraction/filtering behavior.
    _DIAG_PREFIX = "[DISCOVERY_DIAGNOSTIC]"

    #: Max characters of evidence to log per raw result (diagnostic readability).
    _DIAG_EVIDENCE_CHARS = 300

    #: AgentCore Gateway Web Search MCP connector endpoint. Read from the environment so
    #: it can be overridden per deployment without code changes; falls back to the
    #: managed default gateway URL.
    _GATEWAY_URL_ENV_VAR = "AGENTCORE_WEB_SEARCH_GATEWAY_URL"
    _DEFAULT_GATEWAY_URL = "https://gateway.bedrock-agentcore.amazonaws.com/web-search/mcp"

    #: The MCP tool name exposed by the Web Search connector, as returned by the
    #: deployed AgentCore Gateway's tools/list. The gateway namespaces the connector
    #: tool with the target name: ``<target-name>___WebSearch``.
    _WEB_SEARCH_TOOL_NAME = "community-scout-web-search___WebSearch"

    #: Domain filter applied to every Web Search Tool call. Extend the include list to
    #: add new source platforms (Requirement 3.8).
    _DOMAIN_FILTER = {"include": ["meetup.com", "lu.ma"]}

    #: Source platforms searched INDEPENDENTLY, one Web Search call per (topic, source),
    #: each domain-scoped so one source cannot crowd the other out of the result set.
    #: Extend this list to add a new source platform.
    _SEARCH_SOURCES = ["meetup.com", "lu.ma"]

    #: Per-source, per-topic result cap requested from the Web Search Tool. Kept well
    #: above the final cap so the pipeline gathers more candidates than it returns.
    _PER_SOURCE_MAX_RESULTS = 15

    #: Maximum number of final events returned. This is a CAP, not a quota: fewer are
    #: returned when fewer verified relevant events exist; unrelated events are never
    #: added to reach it.
    _MAX_FINAL_RESULTS = 10

    #: Query text template. The time window is embedded here (Requirement 3.2); the page
    #: publication date is never used as a substitute for the event date.
    _QUERY_TEMPLATE = "{topics} events in {location} between {start_date} and {end_date}"

    #: Deterministic topic-relevance keyword sets used to gate a candidate event
    #: against the REQUESTED topic before it is accepted (precision over recall).
    #:
    #: Each key is a canonical topic; the value is a list of ``(pattern, is_regex)``
    #: signals. A signal matches when it is found in the event's own evidence (title +
    #: extracted description/snippet), using word-boundary matching so short acronyms
    #: (e.g. "AWS", "K8s", "EKS") do not match inside unrelated words. This gate rejects
    #: events that merely came from an Austin Meetup discovery page or whose topic term
    #: appears only in unrelated page metadata / a neighboring event.
    _TOPIC_KEYWORDS: dict[str, list[str]] = {
        "agentic ai": [
            r"agentic\s+ai",
            r"\bagentic\b",
            r"\bai\s+agents?\b",
            r"\bautonomous\s+agents?\b",
            r"\bllm\s+agents?\b",
            r"\bmulti[-\s]?agent\b",
            r"\bagent(?:ic)?\s+workflows?\b",
            r"\bgenerative\s+ai\b",
            r"\bgen\s?ai\b",
        ],
        "aws": [
            r"\baws\b",
            r"amazon\s+web\s+services",
            r"\bbedrock\b",
            r"\bagentcore\b",
            r"\blambda\b",
            r"\bec2\b",
            r"\beks\b",
            r"\bsagemaker\b",
            r"\bcloudformation\b",
        ],
        "kubernetes": [
            r"\bkubernetes\b",
            r"\bk8s\b",
            r"\bkubectl\b",
            r"\bkubecon\b",
            r"\beks\b",
            r"\bgke\b",
            r"\baks\b",
            r"\bkubevirt\b",
            r"\bhelm\b",
        ],
    }

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
        logger.info(
            "EventSource invocation: topics=%s start_date=%s end_date=%s",
            topics,
            start_date,
            end_date,
        )

        # Source-aware discovery: one domain-scoped Web Search call per (topic, source)
        # so Meetup results cannot crowd Luma out of the result set (and vice versa).
        # Each candidate carries the 0-based invocation order it came from, used for
        # deterministic tie-breaking in deduplication/ranking.
        try:
            candidates = self._discover_candidates(topics, start_date, end_date)
        except Exception as exc:  # noqa: BLE001 - errors must be encoded, not raised
            reason = f"Web Search Tool invocation failed: {exc}"
            logger.warning(
                "AgentCoreWebSearchEventSource error for topics=%s: %s", topics, reason
            )
            return ToolError(tool_name="AgentCoreWebSearchEventSource", reason=reason)

        # No results across any source -> empty list (Requirement 3.6).
        if not candidates:
            return []

        logger.info(
            "Source-aware discovery gathered %d raw candidate(s) across %d source(s)",
            len(candidates),
            len(self._SEARCH_SOURCES),
        )

        # TEMPORARY per-source diagnostic counters (behavior-neutral).
        topic_label = self._diag_topic_label(topics)
        diag_raw: dict[str, int] = {}
        diag_extracted: dict[str, int] = {}
        diag_accepted: dict[str, int] = {}
        diag_rejected: dict[str, int] = {}

        def _diag_source_for_order(order: int) -> str:
            if 0 <= order < len(self._SEARCH_SOURCES):
                return self._diag_source_name(self._SEARCH_SOURCES[order])
            return "unknown"

        events: list[CommonEvent] = []
        for result, invocation_order in candidates:
            diag_source = _diag_source_for_order(invocation_order)
            diag_raw[diag_source] = diag_raw.get(diag_source, 0) + 1
            diag_url = result.get("url") if isinstance(result, dict) else None
            diag_raw_title = result.get("title") if isinstance(result, dict) else None

            raw_event = self._extract_raw_event(result, start_date, end_date)
            if raw_event is None:
                # Required field missing/ambiguous in the evidence: exclude, never
                # fabricate (Requirements 3.3, 9.1).
                diag_rejected[diag_source] = diag_rejected.get(diag_source, 0) + 1
                self._diag_log(
                    source=diag_source, topic=topic_label, title=diag_raw_title,
                    url=diag_url, status="rejected", reason="extraction_failed",
                )
                continue

            # Candidate successfully extracted from the raw evidence.
            diag_extracted[diag_source] = diag_extracted.get(diag_source, 0) + 1
            self._diag_log(
                source=diag_source, topic=topic_label,
                candidate_title=raw_event.get("title"),
                candidate_date=raw_event.get("start_datetime"),
                candidate_city=raw_event.get("city"),
                candidate_url=raw_event.get("event_url"),
                status="extracted",
            )

            source_platform = self._derive_platform(raw_event.get("event_url", ""))
            if source_platform is None:
                # URL not from a configured domain -> cannot derive platform reliably.
                diag_rejected[diag_source] = diag_rejected.get(diag_source, 0) + 1
                self._diag_log(
                    source=diag_source, topic=topic_label,
                    title=raw_event.get("title"), url=raw_event.get("event_url"),
                    status="rejected", reason="extraction_failed",
                )
                continue

            match_confidence = self._extract_confidence(result)

            event = normalize_event(
                raw_event=raw_event,
                source_platform=source_platform,
                source_invocation_order=invocation_order,
                match_confidence=match_confidence,
                event_identifier=raw_event.get("title"),
            )
            if event is None:
                # normalize_event already logged the exclusion reason.
                diag_rejected[diag_source] = diag_rejected.get(diag_source, 0) + 1
                self._diag_log(
                    source=diag_source, topic=topic_label,
                    title=raw_event.get("title"), url=raw_event.get("event_url"),
                    status="rejected", reason="extraction_failed",
                )
                continue

            # Deterministic topic-relevance gate (precision over recall): the EVENT's
            # own evidence (title + extracted description) must contain credible
            # evidence for at least one REQUESTED topic. This rejects false positives
            # from generic Austin Meetup discovery pages (e.g. "Hong Kong Mahjong",
            # "Religious Studies: Demon Hunters") that were returned only because the
            # search term appeared elsewhere on the page. Never substitute unrelated
            # events; if nothing relevant exists, this yields zero events.
            if not self._event_matches_requested_topics(event, topics):
                logger.info(
                    "Topic gate rejected event '%s' — no requested-topic evidence "
                    "in its title/description (topics=%s)",
                    event.title,
                    topics,
                )
                diag_rejected[diag_source] = diag_rejected.get(diag_source, 0) + 1
                self._diag_log(
                    source=diag_source, topic=topic_label, title=event.title,
                    url=event.event_url, status="rejected", reason="topic_irrelevant",
                )
                continue

            # Post-filter by verified event start date (Requirements 3.2, 3.5). The page
            # publication date is never used as a substitute for the event start date.
            if self._is_within_window(event.start_datetime, start_date, end_date):
                events.append(event)
                diag_accepted[diag_source] = diag_accepted.get(diag_source, 0) + 1
                self._diag_log(
                    source=diag_source, topic=topic_label, title=event.title,
                    date=event.start_datetime, url=event.event_url, status="accepted",
                )
            else:
                diag_rejected[diag_source] = diag_rejected.get(diag_source, 0) + 1
                self._diag_log(
                    source=diag_source, topic=topic_label, title=event.title,
                    url=event.event_url, status="rejected", reason="outside_date_window",
                )

        # Per-source discovery summary (diagnostic).
        for diag_source in sorted(set(diag_raw) | set(self._diag_source_name(s2) for s2 in self._SEARCH_SOURCES)):
            self._diag_log(
                source=diag_source,
                topic=topic_label,
                raw_results=diag_raw.get(diag_source, 0),
                extracted_candidates=diag_extracted.get(diag_source, 0),
                accepted_events=diag_accepted.get(diag_source, 0),
                rejected_events=diag_rejected.get(diag_source, 0),
            )

        # Cross-source deduplication, then deterministic ranking, then a TOP-N cap.
        # Both Meetup and Luma events are eligible; dedup preserves event_url exactly.
        # The cap is a MAXIMUM, never a quota — fewer are returned when fewer verified
        # relevant events exist, and unrelated events are never added to reach it.
        return self._select_top_events(events, topics, start_date, end_date)

    # ------------------------------------------------------------------ #
    # Web Search Tool invocation (isolated configuration)
    # ------------------------------------------------------------------ #
    def _build_query(
        self,
        topics: list[str],
        start_date: str,
        end_date: str,
        source: Optional[str] = None,
    ) -> str:
        """Construct the search-query text embedding topics, location, and time window.

        When ``source`` is given, the query is made source-specific (e.g. by naming the
        platform and using a ``site:`` hint) so a single source's results do not crowd
        the other out of the Web Search result set.
        """
        topic_text = " ".join(t.strip() for t in topics if t and t.strip())
        base = self._QUERY_TEMPLATE.format(
            topics=topic_text,
            location=self._LOCATION,
            start_date=start_date,
            end_date=end_date,
        )
        if source:
            platform = self._PLATFORM_BY_DOMAIN.get(source, source)
            return f"{platform} {base} site:{source}"
        return base

    def _build_arguments(
        self,
        topics: list[str],
        start_date: str,
        end_date: str,
        source: Optional[str] = None,
    ) -> dict:
        """Assemble the MCP ``tools/call`` arguments, including the domain filter.

        The time window is embedded in the query text only. ``publishedDateFilter`` is
        intentionally NOT set, because a page's publication date is not the event date
        (Requirement 3.2).

        When ``source`` is given, the domain filter is scoped to that single source and
        the query is made source-specific, so each source is searched INDEPENDENTLY and
        one source cannot crowd the other out. When ``source`` is ``None``, the original
        combined domain filter is used (backward-compatible).
        """
        if source:
            domain_filter = {"include": [source]}
        else:
            domain_filter = self._DOMAIN_FILTER
        return {
            "query": self._build_query(topics, start_date, end_date, source=source),
            "maxResults": self._PER_SOURCE_MAX_RESULTS,
            "filters": {"domainFilter": domain_filter},
        }

    def _discover_candidates(
        self, topics: list[str], start_date: str, end_date: str
    ) -> list[tuple[dict, int]]:
        """Gather raw Web Search candidates across all sources, tagged with call order.

        Delegates to :meth:`_invoke_web_search`, which performs one domain-scoped Web
        Search call per source so both Meetup and Luma get an independent opportunity to
        contribute. Each raw result is paired with the 0-based invocation order it was
        discovered in (used for deterministic dedup/ranking tie-breaks). This gathers
        MORE candidates than the final cap so the pipeline has room to select the best.

        Raises:
            Exception: Any transport/tool error is allowed to propagate; the caller
                converts it into a ``ToolError``.
        """
        raw_results = self._invoke_web_search(topics, start_date, end_date)

        # ``_invoke_web_search`` may return either a flat list of result dicts (the
        # backward-compatible / mocked shape) or a list of ``(result, order)`` pairs
        # (the source-aware shape). Normalize to ``(result, order)`` pairs.
        candidates: list[tuple[dict, int]] = []
        for item in raw_results:
            if (
                isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[0], dict)
                and isinstance(item[1], int)
            ):
                candidates.append(item)
            elif isinstance(item, dict):
                candidates.append((item, 0))
        return candidates

    def _invoke_web_search(
        self, topics: list[str], start_date: str, end_date: str
    ):
        """Invoke the Web Search Tool over MCP, once per source, and return raw results.

        Performs one INDEPENDENT, domain-scoped Web Search call per source in
        :data:`_SEARCH_SOURCES` (e.g. meetup.com and lu.ma), so a single source cannot
        crowd the other out of the result set. Returns a list of ``(result, order)``
        pairs, where ``order`` is the 0-based index of the source-specific call the
        result came from.

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

        results: list[tuple[dict, int]] = []
        topic_label = self._diag_topic_label(topics)
        for order, source in enumerate(self._SEARCH_SOURCES):
            # Behavior-neutral diagnostics: reconstruct the exact arguments this call
            # will send so we can log the query and domain filter (without altering it).
            args = self._build_arguments(topics, start_date, end_date, source=source)
            source_results = self._invoke_web_search_for_source(
                topics, start_date, end_date, source
            )

            # (1) Per source-specific Web Search call: query, domain filter, raw count.
            self._diag_log(
                source=self._diag_source_name(source),
                topic=topic_label,
                query=args.get("query", ""),
                domain_filter=args.get("filters", {}).get("domainFilter", {}).get(
                    "include", []
                ),
                raw_results=len(source_results),
            )

            # (2) Each raw result: index, title, exact URL, first 300 chars of evidence.
            for idx, raw in enumerate(source_results, start=1):
                self._diag_log(
                    source=self._diag_source_name(source),
                    topic=topic_label,
                    result=idx,
                    title=(raw.get("title") if isinstance(raw, dict) else None),
                    url=(raw.get("url") if isinstance(raw, dict) else None),
                    evidence=self._diag_evidence(raw),
                )

            for raw in source_results:
                results.append((raw, order))
        return results

    def _invoke_web_search_for_source(
        self, topics: list[str], start_date: str, end_date: str, source: str
    ) -> list[dict]:
        """Invoke the Web Search Tool for a SINGLE source (domain-scoped) over MCP.

        Uses the existing AgentCore Gateway Web Search tool with a domain filter scoped
        to ``source`` and a source-specific query. No Meetup/Luma API and no scraping.

        Raises:
            Exception: Any transport/tool error is allowed to propagate; the caller
                converts it into a ``ToolError``.
        """
        arguments = self._build_arguments(topics, start_date, end_date, source=source)

        # The MCPClient manages the connection lifecycle to the AgentCore Gateway Web
        # Search connector. The client is used as a context manager so the session is
        # torn down after the call.
        client = MCPClient(lambda: self._create_mcp_transport())
        with client:
            response = client.call_tool_sync(
                tool_use_id=f"agentcore-web-search-{source}",
                name=self._WEB_SEARCH_TOOL_NAME,
                arguments=arguments,
                read_timeout_seconds=timedelta(seconds=_TOOL_TIMEOUT_SECONDS),
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
                # A plain result dict already shaped like a search result. Checked
                # before the serialized-JSON branch because a real Web Search result
                # also carries a "text" field (the evidence body, not JSON), so a block
                # with a url/title must be treated as a result rather than parsed.
                if "url" in block or "title" in block:
                    results.append(block)
                    continue
                # Content block containing serialized JSON in its "text" field. The
                # deployed AgentCore Gateway returns blocks shaped as {"text": "<JSON>"}
                # with NO "type" key, so a string "text" field alone is sufficient;
                # a legacy {"type": "text", "text": ...} block is still accepted.
                if isinstance(block.get("text"), str):
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

        return results

    def _extract_raw_event(
        self, result: dict, start_date: str, end_date: str
    ) -> Optional[dict]:
        """Extract a raw event dict from a single Web Search result.

        Uses only facts explicitly present in the snippet text and its source URL. Any
        required field (title, start_datetime, city, event_url) that cannot be reliably
        extracted causes the result to be excluded (returns ``None``). No inference or
        substitution is performed (Requirements 3.3, 9.1).

        A single Meetup group-page result may describe MULTIPLE events with different
        dates. Rather than blindly taking the first date, the event start date is chosen
        as the first candidate date in the evidence that can be resolved to a concrete
        date falling inside the requested ``[start_date, end_date]`` window. Meetup's
        abbreviated ``Thu, Sep 17 · 6:00 PM CDT`` format carries no year; the year is
        resolved deterministically from the window and is never invented. If no candidate
        date can be reliably associated and placed in-window, the record is excluded.
        """
        if not isinstance(result, dict):
            return None

        event_url = self._as_str(result.get("url"))
        title = self._as_str(result.get("title"))
        # The deployed AgentCore Web Search result carries the evidence body in the
        # "text" field. Prefer it, falling back to "snippet" for compatibility with
        # alternate/legacy response shapes. The evidence text is the ONLY source for
        # event facts (date, city); publishedDate is never used as the event date.
        snippet = (
            self._as_str(result.get("text"))
            or self._as_str(result.get("snippet"))
            or ""
        )

        # event_url and the outer result title are required. The outer title may be a
        # Meetup GROUP/PAGE title (not an event title) when the result is a group page.
        if not event_url or not title:
            return None

        # Select the event start date/time from the snippet text only, choosing a
        # candidate that resolves inside the requested window (handles group pages that
        # list several events and Meetup's yearless date format). The position lets us
        # associate the selected date with its adjacent event title, and the candidate
        # count distinguishes a single-event page from a multi-event group page.
        selection = self._select_event_datetime_with_pos(snippet, start_date, end_date)
        if not selection:
            return None
        start_datetime, date_pos, prev_date_end, total_candidates = selection

        # Determine the EVENT title. On a multi-event group page the outer result title
        # is the group/page title, so it must NOT be paired with an embedded event date.
        # Use the event title immediately associated with the selected date (searched in
        # the span between the previous event's date and this one); if no adjacent title
        # can be reliably isolated, exclude rather than mispair.
        if total_candidates >= 2:
            event_title = self._extract_event_title_near(
                snippet, date_pos, prev_date_end
            )
            if not event_title:
                return None
        else:
            # Single-event page: the outer result title is the event title.
            event_title = title

        # City must be explicitly present in the evidence (snippet). v0.1 targets Austin.
        city = self._extract_city(snippet)
        if not city:
            return None

        raw_event: dict = {
            "title": event_title,
            "start_datetime": start_datetime,
            "city": city,
            # Preserve the original Web Search URL byte-for-byte. If Web Search returned
            # only the group URL, that group URL is kept as-is — an event-specific URL is
            # never fabricated.
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
    #: Meetup's abbreviated event-date format, e.g. "Thu, Sep 17 \u00b7 6:00 PM CDT".
    #: The leading weekday and the trailing time+timezone are optional. NOTE: this
    #: format carries NO year, so the year must be resolved from the requested window.
    _MEETUP_DATE_RE = re.compile(
        r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,\s*)?"
        r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
        r"\s+(?P<day>\d{1,2})"
        r"(?:\s*[\u00b7\u2022\-]\s*(?P<time>\d{1,2}:\d{2}\s*(?:AM|PM)))?",
        re.IGNORECASE,
    )

    #: Meetup's EXPLICIT-YEAR event-date format, e.g. "Thu, Nov 20, 2025 \u00b7 6:00 PM CST"
    #: or "Nov 20, 2025 \u00b7 6:00 PM CST". The leading weekday and trailing time+timezone
    #: are optional; the month may be abbreviated or full. The YEAR here is AUTHORITATIVE
    #: and must never be reinterpreted from the requested window. Matched spans suppress
    #: any overlapping yearless :data:`_MEETUP_DATE_RE` match.
    _MEETUP_EXPLICIT_YEAR_RE = re.compile(
        r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,\s*)?"
        r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
        r"\s+(?P<day>\d{1,2}),\s*(?P<year>\d{4})"
        r"(?:\s*[\u00b7\u2022\-]\s*(?P<time>\d{1,2}:\d{2}\s*(?:AM|PM)))?",
        re.IGNORECASE,
    )

    #: Full month names indexed 1..12 (for reconstructing a normalizer-parsable string).
    _MONTH_NAMES = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]
    #: Abbreviation -> month number (1-based). "Sept" is accepted alongside "Sep".
    _MONTH_ABBR_TO_NUM = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    def _iter_date_candidates(self, snippet: str):
        """Yield (start, end, value) date candidates found in the snippet.

        Candidates are emitted in order of appearance. Each is a string that
        :func:`normalize_event`/``_convert_to_iso8601_utc`` can parse:
          * ISO datetimes/dates as-is (already carry a year);
          * "Month D, YYYY [at H:MM AM/PM]" as-is (already carry a year);
          * Meetup "Thu, Sep 17 \u00b7 6:00 PM CDT" resolved to a concrete year via
            :meth:`_resolve_meetup_year`. Yearless candidates that cannot be
            deterministically placed in the window are skipped, never invented.

        The ``start_date``/``end_date`` window is used only to resolve an otherwise
        missing year; it does not fabricate any other fact.
        """
        if not snippet:
            return

        for m in self._ISO_DATETIME_RE.finditer(snippet):
            yield (m.start(), m.end(), m.group(0))

        # Year-bearing dates carry an AUTHORITATIVE year. Record their spans so any
        # overlapping Meetup (yearless) match is suppressed — an explicit year in the
        # evidence must never be overridden by window-based year resolution.
        year_spans: list[tuple[int, int]] = []

        # (a) Full-month long dates ("September 15, 2025 [at 6:00 PM]").
        for m in self._LONG_DATE_RE.finditer(snippet):
            year_spans.append((m.start(), m.end()))
            yield (m.start(), m.end(), m.group(0))

        # (b) Meetup EXPLICIT-YEAR dates ("Thu, Nov 20, 2025 \u00b7 6:00 PM CST",
        #     "Nov 20, 2025 \u00b7 6:00 PM CST"). The abbreviated/weekday form is not
        #     matched by _LONG_DATE_RE, so handle it here. Emit a normalizer-parsable
        #     year-bearing string built from the explicit year (never the window year).
        for m in self._MEETUP_EXPLICIT_YEAR_RE.finditer(snippet):
            match_end = m.end()
            tz = re.match(r"\s*[A-Z]{2,4}\b", snippet[match_end:])
            if tz:
                match_end += tz.end()
            year_spans.append((m.start(), match_end))
            built = self._build_explicit_year_string(
                m.group("month"), m.group("day"), m.group("year"), m.group("time")
            )
            if built is not None:
                yield (m.start(), match_end, built)

        # Meetup yearless candidates are resolved lazily by the selector, which needs
        # the window; emit the raw match plus its parsed parts for resolution there.
        # Skip any that fall within a year-bearing span. The match end extends past any
        # trailing timezone token (e.g. "CDT") so a following title is clean.
        for m in self._MEETUP_DATE_RE.finditer(snippet):
            if any(start <= m.start() < end for start, end in year_spans):
                continue
            match_end = m.end()
            # Consume a trailing timezone abbreviation (e.g. " CDT", " PST") if present,
            # so it is not mistaken for part of the following event title.
            tz = re.match(r"\s*[A-Z]{2,4}\b", snippet[match_end:])
            if tz:
                match_end += tz.end()
            yield (
                m.start(),
                match_end,
                ("__MEETUP__", m.group("month"), m.group("day"), m.group("time")),
            )

    def _build_explicit_year_string(
        self, month: str, day: str, year: str, time_part: Optional[str]
    ) -> Optional[str]:
        """Build a normalizer-parsable string from an EXPLICIT-YEAR Meetup date.

        The provided ``year`` is authoritative and is used verbatim — it is never
        reinterpreted from the requested window. Returns ``None`` if the month token is
        not recognized. Example: ("Nov", "20", "2025", "6:00 PM") ->
        "November 20, 2025 at 6:00 PM".
        """
        month_num = self._MONTH_ABBR_TO_NUM.get(month.lower())
        if month_num is None:
            return None
        try:
            day_num = int(day)
            year_num = int(year)
        except (TypeError, ValueError):
            return None
        month_name = self._MONTH_NAMES[month_num - 1]
        if time_part:
            normalized_time = re.sub(r"\s+", " ", time_part.strip()).upper()
            return f"{month_name} {day_num}, {year_num} at {normalized_time}"
        return f"{month_name} {day_num}, {year_num}"

    def _resolve_meetup_year(
        self, month: str, day: str, time_part: Optional[str], start_date: str, end_date: str
    ) -> Optional[str]:
        """Resolve a yearless Meetup date to a normalizer-parsable string, or None.

        The year is chosen as the unique year within the requested window's year range
        for which ``month/day`` lands inside ``[start_date, end_date]``. If zero years
        qualify (date not in window) or more than one qualifies (ambiguous), returns
        ``None`` — the year is never invented (Requirement: do not fabricate a year).
        """
        month_num = self._MONTH_ABBR_TO_NUM.get(month.lower())
        if month_num is None:
            return None
        try:
            day_num = int(day)
        except (TypeError, ValueError):
            return None

        start_year = int(start_date[:4])
        end_year = int(end_date[:4])

        month_name = self._MONTH_NAMES[month_num - 1]
        matches: list[str] = []
        for year in range(start_year, end_year + 1):
            if time_part:
                # e.g. "September 17, 2026 at 6:00 PM" (normalizer-parsable).
                normalized_time = re.sub(r"\s+", " ", time_part.strip()).upper()
                candidate = f"{month_name} {day_num}, {year} at {normalized_time}"
            else:
                candidate = f"{month_name} {day_num}, {year}"
            # Validate it is a real calendar date (via the shared normalizer path) and
            # falls inside the requested window.
            iso = self._safe_iso_date(candidate)
            if iso is None:
                continue
            if start_date <= iso <= end_date:
                matches.append(candidate)

        # Deterministic only when exactly one year in the window qualifies.
        if len(matches) == 1:
            return matches[0]
        return None

    def _select_event_datetime(
        self, snippet: str, start_date: str, end_date: str
    ) -> Optional[str]:
        """Select an event start datetime string that resolves inside the window.

        Thin wrapper over :meth:`_select_event_datetime_with_pos` that returns only the
        datetime string (kept for callers/tests that do not need the match position).
        """
        selection = self._select_event_datetime_with_pos(snippet, start_date, end_date)
        return selection[0] if selection else None

    def _select_event_datetime_with_pos(
        self, snippet: str, start_date: str, end_date: str
    ):
        """Select the in-window event datetime and report where it was found.

        Walks candidate dates in order of appearance and picks the first that resolves
        to a concrete date inside ``[start_date, end_date]``. It also reports the total
        number of date candidates in the evidence (regardless of window) and the end
        position of the date candidate that immediately precedes the selected one. The
        preceding-date boundary lets the caller isolate the event title that belongs to
        the selected date on a multi-event group page.

        Returns:
            A tuple ``(candidate_str, match_start, prev_match_end, total_date_candidates)``
            for the selected date, or ``None`` when no candidate resolves in-window (the
            record is then excluded rather than fabricated). ``candidate_str`` is passed
            to :func:`normalize_event` for ISO 8601 UTC conversion, preserving the single
            normalization path.
        """
        if not snippet:
            return None

        candidates = sorted(self._iter_date_candidates(snippet), key=lambda c: c[0])
        total_candidates = len(candidates)

        prev_end = 0
        for start, end, cand in candidates:
            if isinstance(cand, tuple) and cand and cand[0] == "__MEETUP__":
                _tag, month, day, time_part = cand
                resolved = self._resolve_meetup_year(
                    month, day, time_part, start_date, end_date
                )
                candidate_str = resolved
            else:
                candidate_str = cand

            iso = self._safe_iso_date(candidate_str) if candidate_str else None
            if iso is not None and start_date <= iso <= end_date:
                return (candidate_str, start, prev_end, total_candidates)

            # Advance the "previous date" boundary past this candidate so the next
            # selected date's title search starts after this date's full extent.
            prev_end = max(prev_end, end)

        return None

    @staticmethod
    def _safe_iso_date(candidate_str: str) -> Optional[str]:
        """Return the YYYY-MM-DD date of a candidate string via the normalizer, or None.

        Uses the same conversion the normalizer uses so window checks agree with the
        eventual normalized value. Never raises.
        """
        from src.tools.normalizer import _convert_to_iso8601_utc

        try:
            return _convert_to_iso8601_utc(candidate_str)[:10]
        except (ValueError, TypeError):
            return None

    #: Separator characters/sequences that delimit an event title from its date line in
    #: Meetup group-page evidence (bullets, middots, dashes, pipes, newlines).
    _TITLE_DATE_SEPARATORS = re.compile(r"[\n\r\u00b7\u2022\|]+")

    def _extract_event_title_near(
        self, snippet: str, date_pos: int, region_start: int = 0
    ) -> Optional[str]:
        """Return the event title immediately associated with a date in the evidence.

        On a Meetup group page, each event title appears immediately BEFORE its date,
        e.g.::

            How to Build and Use Custom GPTs for Real Business Tasks
            Wed, Oct 7 \u00b7 6:30 PM CDT

        The title is searched ONLY within ``snippet[region_start:date_pos]`` — the span
        between the previous event's date (``region_start``) and the selected date — so
        the title of the preceding event never bleeds in. The trailing title-like
        segment of that region is returned. Returns ``None`` when no plausible adjacent
        title can be isolated, so the caller can exclude the record rather than pair the
        outer group-page title with an embedded event date.
        """
        if not snippet or date_pos <= 0:
            return None
        region_start = max(0, region_start)
        if region_start >= date_pos:
            return None

        preceding = snippet[region_start:date_pos]

        # Split on separators that divide a title from surrounding structure. The last
        # non-empty segment before the date is the adjacent event title.
        segments = [seg.strip() for seg in self._TITLE_DATE_SEPARATORS.split(preceding)]
        segments = [seg for seg in segments if seg]
        if not segments:
            return None

        candidate = segments[-1]

        # Strip a leading time / timezone remnant from the previous date line that may
        # precede the title within the region (e.g. "6:30 PM CDT How to ...").
        candidate = re.sub(
            r"^\s*(?:\d{1,2}:\d{2}\s*(?:AM|PM)?\s*)?[A-Z]{2,4}\b\s*",
            "",
            candidate,
        ).strip()
        # Strip a leading weekday token if a date weekday leaked in.
        candidate = re.sub(
            r"^\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s*",
            "",
            candidate,
            flags=re.IGNORECASE,
        ).strip()
        # Drop a trailing weekday token / colons / leading dashes.
        candidate = re.sub(
            r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s*$",
            "",
            candidate,
            flags=re.IGNORECASE,
        ).strip()
        candidate = candidate.strip(":\u2013- ").strip()

        # A plausible event title has real word content and is not itself a bare date.
        if len(candidate) < 3:
            return None
        if self._safe_iso_date(candidate) is not None:
            return None
        if not re.search(r"[A-Za-z]", candidate):
            return None

        return candidate

    def _extract_end_datetime(self, snippet: str) -> Optional[str]:
        """Extract a second ISO date expression as the end datetime, when clearly present.

        Only unambiguous ISO datetimes (which already carry a year) are used for the
        optional end time; yearless Meetup dates are not guessed here.
        """
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

    # ------------------------------------------------------------------ #
    # Topic-relevance gate (deterministic; precision over recall)
    # ------------------------------------------------------------------ #
    @classmethod
    def _canonical_topics(cls, topic: str) -> list[str]:
        """Map a requested topic string to the canonical keyword bucket(s) it implies.

        Matching is tolerant of surface variations (case, punctuation, extra words) so a
        requested topic such as "Agentic AI", "aws", or "Kubernetes / K8s" resolves to
        the right keyword set. A requested topic that matches no known bucket falls back
        to using its own trimmed text as a literal signal (so custom topics still gate
        on their own term rather than being accepted unconditionally).
        """
        normalized = re.sub(r"[^a-z0-9]+", " ", (topic or "").lower()).strip()
        matched: list[str] = []
        for canonical in cls._TOPIC_KEYWORDS:
            # e.g. requested "agentic ai" or "ai agents" -> "agentic ai" bucket;
            # requested "aws" -> "aws"; requested "kubernetes"/"k8s" -> "kubernetes".
            canon_tokens = canonical.split()
            if canonical in normalized or all(tok in normalized for tok in canon_tokens):
                matched.append(canonical)
        # Direct acronym/keyword hints in the requested topic (e.g. "k8s", "eks").
        for canonical, patterns in cls._TOPIC_KEYWORDS.items():
            if canonical in matched:
                continue
            for pattern in patterns:
                if re.search(pattern, normalized, re.IGNORECASE):
                    matched.append(canonical)
                    break
        return matched

    @classmethod
    def _evidence_matches_topic(cls, evidence: str, canonical_topic: str) -> bool:
        """Return True if ``evidence`` contains a credible signal for ``canonical_topic``.

        Uses the deterministic keyword patterns with word-boundary matching, so short
        acronyms do not match inside unrelated words.
        """
        patterns = cls._TOPIC_KEYWORDS.get(canonical_topic, [])
        for pattern in patterns:
            if re.search(pattern, evidence, re.IGNORECASE):
                return True
        return False

    def _event_matches_requested_topics(
        self, event: CommonEvent, topics: list[str]
    ) -> bool:
        """Gate an event against the REQUESTED topics using its own evidence.

        The event's evidence is its title plus any extracted description (both come only
        from the event itself, per the evidence-constrained extraction rules). The event
        is accepted only if that evidence contains a credible keyword signal for at least
        one requested topic. Page metadata, sibling events, and the fact that a result
        came from an Austin discovery page are NOT evidence and cannot pass the gate.

        If a requested topic maps to no known keyword bucket, its own trimmed term is
        used as a literal, word-boundary signal so custom topics still gate on-topic.
        """
        evidence_parts = [event.title or ""]
        if event.description:
            evidence_parts.append(event.description)
        evidence = " ".join(evidence_parts)
        if not evidence.strip():
            return False

        for topic in topics:
            canonicals = self._canonical_topics(topic)
            if canonicals:
                for canonical in canonicals:
                    if self._evidence_matches_topic(evidence, canonical):
                        return True
            else:
                # Unknown/custom topic: require its own term (word-boundary) in evidence.
                term = (topic or "").strip()
                if term and re.search(
                    r"\b" + re.escape(term) + r"\b", evidence, re.IGNORECASE
                ):
                    return True
        return False

    # ------------------------------------------------------------------ #
    # TEMPORARY discovery diagnostics (INFO-level, behavior-neutral)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _diag_source_name(domain_or_platform: str) -> str:
        """Map a domain/platform to the short diagnostic source label (meetup/luma)."""
        value = (domain_or_platform or "").lower()
        if "meetup" in value:
            return "meetup"
        if "lu.ma" in value or "luma" in value:
            return "luma"
        return value or "unknown"

    @classmethod
    def _diag_topic_label(cls, topics: list[str]) -> str:
        """Render the requested topics as a single diagnostic label.

        The application searches all requested topics together per source, so the
        diagnostic ``topic`` field reflects that combined request faithfully.
        """
        return ", ".join(t.strip() for t in topics if t and t.strip())

    @classmethod
    def _diag_evidence(cls, result: dict) -> str:
        """Return the evidence text using the SAME field selection as extraction.

        Prefers ``text`` then ``snippet`` (identical to ``_extract_raw_event``); the
        evidence is not altered — only truncated for readable logging.
        """
        if not isinstance(result, dict):
            return ""
        evidence = ""
        for key in ("text", "snippet"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                evidence = value
                break
        # Collapse newlines for one-line logging; do NOT alter content otherwise.
        collapsed = " ".join(evidence.split())
        return collapsed[: cls._DIAG_EVIDENCE_CHARS]

    def _diag_log(self, **fields) -> None:
        """Emit one INFO-level ``[DISCOVERY_DIAGNOSTIC]`` line with key=value fields.

        Writes to the module logger (stdout/stderr under AgentCore Runtime, so it lands
        in the existing CloudWatch log group). SECURITY: only discovery/search evidence
        is ever passed here — never credentials, headers, tokens, or signatures.
        """
        parts = [self._DIAG_PREFIX]
        for key, value in fields.items():
            parts.append(f"{key}={value!r}" if isinstance(value, str) else f"{key}={value}")
        logger.info(" ".join(parts))

    # ------------------------------------------------------------------ #
    # Cross-source selection: deduplicate -> rank -> top-N cap
    # ------------------------------------------------------------------ #
    def _select_top_events(
        self,
        events: list[CommonEvent],
        topics: list[str],
        start_date: str,
        end_date: str,
    ) -> list[CommonEvent]:
        """Deduplicate across sources, rank deterministically, and cap to the top N.

        Applies the existing pipeline steps in-source so the tool returns the best
        unique, relevant events regardless of which source (Meetup or Luma) they came
        from:

        1. :func:`src.pipeline.deduplicator.deduplicate` — cross-source dedup by
           (title, date, city); retains the highest-confidence record and preserves
           ``event_url`` byte-for-byte.
        2. :func:`src.pipeline.ranker.rank` — deterministic topic+recency ranking.
        3. Cap to :data:`_MAX_FINAL_RESULTS`. This is a MAXIMUM, not a quota: if fewer
           verified relevant events exist, fewer are returned; unrelated events are
           never added to reach the cap.

        The events are returned in ranked order (the downstream pipeline re-runs these
        same deterministic steps, producing the identical ordering).
        """
        # Imported locally to avoid any import-time coupling; both are pure functions.
        from src.pipeline.deduplicator import deduplicate
        from src.pipeline.ranker import rank
        from src.models.events import QueryContext

        if not events:
            return []

        deduplicated = deduplicate(events)

        context = QueryContext(
            topics=list(topics),
            location=self._LOCATION,
            start_date=start_date,
            end_date=end_date,
        )
        ranked = rank(deduplicated, context)

        top = [ranked_event.event for ranked_event in ranked[: self._MAX_FINAL_RESULTS]]
        logger.info(
            "Selection: %d relevant event(s) after dedup/rank; returning top %d "
            "(cap=%d)",
            len(deduplicated),
            len(top),
            self._MAX_FINAL_RESULTS,
        )
        return top


# Module-level singleton reused across invocations.
_event_source = AgentCoreWebSearchEventSource()


#: Keys of the JSON-serializable CommonEvent representation exposed at the Strands
#: tool boundary. Required fields are always emitted; optional fields are emitted
#: only when present (see :func:`_common_event_to_dict`).
_COMMON_EVENT_REQUIRED_FIELDS = (
    "title",
    "start_datetime",
    "city",
    "source_platform",
    "event_url",
)
#: Optional/derived CommonEvent fields, emitted only when set (not ``None``).
_COMMON_EVENT_OPTIONAL_FIELDS = (
    "description",
    "end_datetime",
    "location_name",
    "match_confidence",
    "source_invocation_order",
)


def _common_event_to_dict(event: CommonEvent) -> dict:
    """Serialize a ``CommonEvent`` to a JSON-serializable dict (Strands tool boundary).

    A pure, lossless field copy — no domain logic — used only to hand the Strands
    runtime structured data instead of a ``repr(CommonEvent)`` string. Required fields
    are always included; optional fields are included ONLY when present (non-``None``).
    ``event_url`` is copied verbatim.
    """
    payload: dict = {name: getattr(event, name) for name in _COMMON_EVENT_REQUIRED_FIELDS}
    for name in _COMMON_EVENT_OPTIONAL_FIELDS:
        value = getattr(event, name)
        if value is not None:
            payload[name] = value
    return payload


@tool
def agentcore_web_search_event_source(
    topics: list[str],
    start_date: str,
    end_date: str,
) -> str:
    """
    Search meetup.com and lu.ma for Austin, Texas tech events matching the given topics.

    This is the AgentCore-invokable (Strands) entry point for the v0.1
    Event_Source_Tool. The Location is fixed to Austin, Texas and is not a parameter.

    The underlying domain event source returns ``list[CommonEvent]`` on success or a
    ``ToolError`` on failure (its interface is unchanged). This wrapper serializes that
    result into a JSON STRING (via ``json.dumps``) so the Strands runtime carries valid
    JSON across the tool boundary — NOT a Python ``repr()`` of a dict (which uses
    single quotes and would fail ``json.loads`` in the orchestrator's collector). The
    orchestrator deserializes this JSON back into ``CommonEvent`` objects.

    Args:
        topics: 1–10 topic strings (each 1–200 chars), e.g. ["Agentic AI", "AWS"].
        start_date: Inclusive time-window start as an ISO 8601 date (YYYY-MM-DD).
        end_date: Inclusive time-window end as an ISO 8601 date (YYYY-MM-DD).

    Returns:
        A JSON string. On success::

            '{"status": "success", "events": [ {<CommonEvent fields>}, ... ]}'

        (``events`` is ``[]`` when no matching events are found.) On failure::

            '{"status": "error", "tool_name": "...", "reason": "..."}'
    """
    result = _event_source(topics, start_date, end_date)

    if isinstance(result, ToolError):
        payload = {
            "status": "error",
            "tool_name": result.tool_name,
            "reason": result.reason,
        }
    else:
        # Success: a (possibly empty) list of CommonEvent records.
        payload = {
            "status": "success",
            "events": [_common_event_to_dict(event) for event in result],
        }

    # Return a real JSON string so Strands places valid JSON (double-quoted) into
    # ``toolResult.content[].text`` rather than a Python repr of a dict.
    return json.dumps(payload)
