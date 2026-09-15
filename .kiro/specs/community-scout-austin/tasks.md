# Implementation Plan: Community Scout Austin

## Overview

Implement the AI Community Scout as an agentic Python application hosted on Amazon Bedrock AgentCore Runtime. The build proceeds in layers: data models first, then the AgentCoreWebSearchEventSource tool (which uses the AgentCore Web Search Tool MCP connector), then the pipeline components (Aggregator, Deduplicator, Ranker, Response Generator), and finally the Scout Orchestrator that wires everything together under the AgentCore entry point.

## Tasks

- [x] 1. Scaffold project structure and configuration
  - Create the top-level directory layout: `src/`, `src/models/`, `src/tools/`, `src/pipeline/`, `tests/unit/`, `tests/property/`, `tests/integration/`
  - Add `requirements.txt` pinning `boto3`, `strands-agents`, `bedrock-agentcore`, `requests`, `hypothesis`, `pytest`, `pytest-asyncio`
  - Create `.env.example` documenting the only environment variable: `BEDROCK_MODEL_ID` — note explicitly: "No API keys or OAuth credentials needed. Both meetup.com and lu.ma are accessed via the AgentCore Web Search Tool."
  - Create `src/config.py` that reads `BEDROCK_MODEL_ID` (default `us.anthropic.claude-sonnet-4-20250514`) from the environment; expose `get_model_id()` helper; no credential helpers needed
  - _Requirements: 10.1, 10.3_

- [x] 2. Implement data models
  - [x] 2.1 Define `CommonEvent`, `RankedEvent`, `ToolError`, and `QueryContext` dataclasses in `src/models/events.py` with all required and optional fields, type annotations, and docstrings matching the design specification
    - Implement `CommonEvent` required fields: title, start_datetime, city, source_platform, event_url (all non-empty strings)
    - Implement `CommonEvent` optional fields: description, end_datetime, location_name (string or null)
    - Implement `CommonEvent` invariant: required string fields must be non-empty; `start_datetime` / `end_datetime` (when non-null) accepted only as ISO 8601 UTC strings ending in `Z`
    - _Requirements: 5.1, 5.6_

  - [ ]* 2.2 Write unit tests for `CommonEvent` field validation
    - Verify that instantiation with an empty required field raises `ValueError`
    - Verify optional `end_datetime` accepts `None`
    - _Requirements: 5.1, 5.3_

- [x] 3. Implement the Event Source Tool interface and normalizer
  - [x] 3.1 Define the `EventSourceTool` `Protocol` in `src/tools/interface.py` with the typed call signature `(topics, start_date, end_date) -> list[CommonEvent] | ToolError`
    - Austin, Texas is fixed internally for v0.1 and must not be passed as a caller-supplied parameter
    - _Requirements: 2.1_

  - [x] 3.2 Implement `normalize_event()` in `src/tools/normalizer.py`
    - Accept a raw platform dict and a `source_platform` string
    - Verify presence of all required fields (title, start_datetime, city, source_platform, event_url); if any required field is missing or empty, return `None` (exclude the event)
    - For optional fields (description, end_datetime, location_name), set to `None` when absent from the source
    - Convert all datetime strings to ISO 8601 UTC ending in `Z`; preserve event_url byte-for-byte unchanged
    - Permit deterministic normalization: whitespace stripping, ISO 8601 conversion, source platform name derivation from URL domain
    - Log skipped records with `{platform, event_identifier, missing_required_field}`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [x] 3.3 Write property test for `normalize_event()` — Property 6
    - **Property 6: Normalization Preserves and Validates Required Fields**
    - **Validates: Requirements 5.1, 5.2, 5.5**
    - `# Feature: community-scout-austin, Property 6: normalization preserves required fields and excludes incomplete records`

  - [ ]* 3.4 Write property test for `normalize_event()` — Property 7
    - **Property 7: ISO 8601 UTC Datetime Format**
    - **Validates: Requirements 5.6**
    - `# Feature: community-scout-austin, Property 7: datetimes are ISO 8601 UTC`

- [ ] 4. Deferred to future version: MeetupEventSource
  - Reserved. MeetupEventSource will implement the EventSourceTool Protocol when Meetup API access is available. No code in v0.1.

- [x] 5. Implement AgentCoreWebSearchEventSource
  - [x] 5.1 Create `src/tools/agentcore_web_search_event_source.py`
    - Implement `AgentCoreWebSearchEventSource` as a class satisfying the `EventSourceTool` Protocol defined in `src/tools/interface.py`
    - Connect to the AgentCore Gateway Web Search MCP connector using `MCPClient` from Strands
    - Construct the search query from the `topics` parameter plus "events in Austin Texas"
    - Pass `domainFilter: {"include": ["meetup.com", "lu.ma"]}` in every Web Search Tool call
    - Embed the time window in the search query string (e.g., 'events in Austin Texas {start_date} to {end_date}'); do NOT use `publishedDateFilter` — the page publication date is not the same as the event date
    - After extraction, post-filter all extracted `CommonEvent` records by verified event start date, keeping only events whose start date falls within `[start_date, end_date]`
    - Extract required fields (title, start_datetime, city, source_platform, event_url) from each returned snippet using the LLM or regex; use only data present in the snippet and its URL — never infer or fabricate
    - Extract optional fields (description, end_datetime, location_name) when available; omit if not present in the evidence
    - Pass each extracted record through `normalize_event()` and accumulate valid `CommonEvent` records
    - Return empty list when no results match
    - Return `ToolError(tool_name='AgentCoreWebSearchEventSource', reason=...)` on Web Search Tool error or timeout; never return partial records
    - Decorate with `@tool` (Strands) so AgentCore can invoke it
    - Isolate the gateway URL, domain filter list, query template, and date filter construction entirely within this class
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 2.1, 2.2_

  - [ ]* 5.2 Write unit tests for `AgentCoreWebSearchEventSource`
    - Mock the Web Search Tool MCP response: valid multi-event snippets, empty response, error response, timeout
    - Assert correct domain filter (`{"include": ["meetup.com", "lu.ma"]}`) is passed in every call
    - Assert event extraction from snippets produces correct `CommonEvent` fields
    - Assert `ToolError` returned on failure with no partial records
    - _Requirements: 3.1, 3.2, 3.5, 3.6, 3.7_

- [x] 6. Checkpoint — core tools ready
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement the Deduplicator
  - [x] 7.1 Create `src/pipeline/deduplicator.py`
    - Implement `deduplicate(events: list[CommonEvent]) -> list[CommonEvent]`
    - Normalize keys: strip + lowercase `title` and `city`; use date portion only of `start_datetime`
    - Group events by `(normalized_title, start_date, normalized_city)`, sort each group by `match_confidence DESC, source_invocation_order ASC`, and retain the first record
    - Preserve `event_url` byte-for-byte; do not modify any other field
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

  - [ ]* 7.2 Write property test for `deduplicate()` — Property 3
    - **Property 3: Deduplication Retains Exactly One Record Per Group**
    - **Validates: Requirements 6.1**
    - `# Feature: community-scout-austin, Property 3: exactly one record per dedup group`

  - [ ]* 7.3 Write property test for `deduplicate()` — Property 4
    - **Property 4: Deduplication Retains Highest-Confidence Record**
    - **Validates: Requirements 6.2, 6.3, 6.4**
    - `# Feature: community-scout-austin, Property 4: dedup retains highest-confidence record`

  - [ ]* 7.4 Write unit tests for `deduplicate()`
    - Two identical events from different sources → retain higher-confidence record
    - Equal confidence → retain record from earlier-invoked tool
    - No duplicates → output equals input
    - Case-insensitive, whitespace-stripped title comparison
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

- [x] 8. Implement the Ranker
  - [x] 8.1 Create `src/pipeline/ranker.py`
    - Implement `rank(events: list[CommonEvent], context: QueryContext) -> list[RankedEvent]`
    - Compute `topic_score = (matched_topics / total_topics) * 80` using case-insensitive substring match of each topic against event title and description
    - Compute `recency_score = max(0, 20 * (1 - days_until_event / 365))`, returning 0 for events >365 days away
    - Do not compute a location score — every accepted event is already in Austin, Texas
    - Clamp final score to [0, 100]
    - When no topics are extracted, assign score 0 to all events
    - Sort output by `relevance_score DESC`, then `start_datetime ASC` on ties
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [ ]* 8.2 Write property test for `rank()` — Property 1
    - **Property 1: Relevance Score Formula and Bounds**
    - **Validates: Requirements 7.1, 7.2**
    - `# Feature: community-scout-austin, Property 1: score = clamped(topic_score + recency_score), topic in [0,80], recency in [0,20], result in [0,100]`

  - [ ]* 8.3 Write property test for `rank()` — Property 2
    - **Property 2: Topic Score Monotonicity**
    - **Validates: Requirements 7.2, 7.3, 11.3**
    - `# Feature: community-scout-austin, Property 2: matching more topics gives strictly higher score`

  - [ ]* 8.4 Write property test for `rank()` — Property 8
    - **Property 8: Ranked Output is Sorted Descending by Score then Start Date**
    - **Validates: Requirements 7.4, 7.5**
    - `# Feature: community-scout-austin, Property 8: ranked output sorted by score desc, start date asc on tie`

  - [ ]* 8.5 Write unit tests for `rank()`
    - Event matching all topics, starting tomorrow → score near 100
    - Event matching no topics, starting in 400 days → score = 0
    - Event matching 1 of 2 topics, 30 days away → verify computed value
    - Zero topics extracted → all events score 0
    - Two equal-score events → ordered by ascending start date
    - _Requirements: 7.1, 7.2, 7.4, 7.5_

- [x] 9. Implement the Event Aggregator
  - [x] 9.1 Create `src/pipeline/aggregator.py`
    - Implement `aggregate(tool_results: list[list[CommonEvent] | ToolError]) -> tuple[list[CommonEvent], list[ToolError]]`
    - Flatten all `list[CommonEvent]` results into a single list (preserving `source_invocation_order`)
    - Collect all `ToolError` entries into a separate list
    - _Requirements: 2.3_

  - [ ]* 9.2 Write property test for `aggregate()` — Property 11
    - **Property 11: Aggregation Completeness**
    - **Validates: Requirements 2.3**
    - Verify that all CommonEvent records returned by successful EventSourceTools are included in the aggregated result
    - `# Feature: community-scout-austin, Property 11: all events from all successful tools are aggregated`

- [x] 10. Implement the Response Generator
  - [x] 10.1 Create `src/pipeline/response_generator.py`
    - Implement `format_response(ranked_events: list[RankedEvent], context: QueryContext, errors: list[ToolError]) -> str`
    - Emit total event count (post-deduplication) at the top
    - List all events sequentially in a single ranked list for Austin, Texas
    - For each event render: title, source platform, city, start date as `YYYY-MM-DD`, and `event_url` as a markdown hyperlink `[title](url)`
    - Render "N/A" for any missing required display field
    - Append a footer listing failed tools and their reasons when `errors` is non-empty
    - When `ranked_events` is empty, return a "no events found" message identifying requested topics, Austin Texas, and the requested time window; suggest broadening the search
    - Never alter, truncate, or omit any `event_url`
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

  - [ ]* 10.2 Write unit tests for `format_response()`
    - Missing display field → "N/A"
    - Zero events → user-facing "no events found" message identifying topics, Austin Texas, and time window, with suggestions
    - Event URLs appear unmodified as markdown hyperlinks
    - Non-empty errors → failure footer present
    - _Requirements: 8.1, 8.3, 8.4, 8.5_

- [x] 11. Implement query validation helpers
  - [x] 11.1 Create `src/validation.py`
    - Implement `validate_query(raw: str) -> None | str` returning an error message string when: input is empty or whitespace-only; extracted topics are missing; extracted topics exceed 10
    - Implement `validate_time_window(start: str, end: str) -> tuple[str, str]` that rejects windows exceeding 5 years and falls back to the default 90-day window with an informational note
    - _Requirements: 1.5, 1.6_

  - [ ]* 11.2 Write unit tests for `validate_query()` and `validate_time_window()`
    - Empty string → error, no tool invocation
    - Whitespace-only string → error, no tool invocation
    - Query with no extractable topic → clarification message for missing topic
    - 11 topics → rejection error before tool invocation
    - Time window > 5 years → fallback to 90-day window with informational message
    - _Requirements: 1.5, 1.6_

- [ ] 12. Checkpoint — pipeline components ready
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 13. Implement the Scout Orchestrator and wire all components
  - [ ] 13.1 Create `src/scout.py`
    - Instantiate `BedrockAgentCoreApp` and decorate the handler with `@app.entrypoint`
    - Read `model_id` from `config.get_model_id()` at handler invocation time (not at import time)
    - Construct a Strands `Agent` with the system prompt, `model_id`, and `tools=[agentcore_web_search_event_source]`
    - System prompt instructs the LLM to: extract topics and time window (location is fixed to Austin, Texas); invoke the AgentCoreWebSearchEventSource with domain-filtered Web Search Tool results; extract structured event data from returned snippets; never fabricate event data; request clarification when topic cannot be extracted; apply default 90-day window when none is provided
    - After the LLM tool-use loop completes, pipe results through `aggregate()` → `deduplicate()` → `rank()` → `format_response()` and yield the final formatted string
    - Surface `ToolError` entries in the response footer without aborting the session
    - Return an error message (not partial results) if the AgentCore session fails to establish
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.4, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6_

  - [ ]* 13.2 Write unit tests for Scout Orchestrator query validation paths
    - Empty query → error returned before any tool call
    - Missing topic → clarification message returned
    - > 10 topics → rejection error before tool invocation
    - _Requirements: 1.5, 1.6_

- [ ] 14. Implement cross-pipeline property-based tests
  - [ ] 14.1 Write property test for URL immutability end-to-end — Property 5
    - Generate a list of `CommonEvent` records, run through `deduplicate()` then `rank()`; assert all `event_url` values are byte-for-byte identical to originals
    - **Property 5: CommonEvent URL Immutability**
    - **Validates: Requirements 5.4, 8.5, 9.2**
    - `# Feature: community-scout-austin, Property 5: event_url is unchanged through the pipeline`

  - [ ] 14.2 Write property test for tool failure containment — Property 10
    - Generate N mock tools where k randomly selected tools raise errors; assert Scout result contains events from (N − k) successful tools and exactly k error entries
    - **Property 10: Tool Failure Containment**
    - **Validates: Requirements 2.5, 11.5**
    - `# Feature: community-scout-austin, Property 10: failed tools are contained without aborting the session`

  - [ ] 14.3 Write property test for time window filtering — Property 9
    - Generate tool invocation parameters and mocked Web Search Tool responses with varied event dates
    - Verify that start_date and end_date are embedded in the Web Search query text
    - Verify that extracted events are deterministically post-filtered using their verified event start_datetime
    - Assert every `CommonEvent` in filtered output has `start_datetime.date` in `[start_date, end_date]`
    - **Property 9: Time Window Filtering**
    - **Validates: Requirements 3.2, 3.5**
    - `# Feature: community-scout-austin, Property 9: all output events are within the requested time window`

- [ ] 15. Implement integration tests
  - [ ] 15.1 Write end-to-end integration test with mocked tool responses
    - Submit a known query with mocked Web Search Tool responses containing meetup.com and lu.ma snippets; assert ranked output contains correct events in expected order with unmodified source URLs
    - _Requirements: 10.4_

  - [ ] 15.2 Write integration test for AgentCore tool timeout
    - Mock the Web Search Tool to time out; assert the Scout returns an error note without partial results and does not raise an exception
    - _Requirements: 2.4, 10.5_

  - [ ] 15.3 Reserved: multi-source partial failure test (applicable when a second EventSourceTool is added)

  - [ ] 15.4 Write integration test for zero results
    - Mock Web Search Tool to return empty results; assert user receives the "no events found" message with suggestions and a count of 0
    - _Requirements: 8.4, 9.3_

- [ ] 16. Final checkpoint — all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for the demo MVP
- Each task references specific requirements for traceability
- Checkpoints at tasks 6, 12, and 16 provide incremental validation gates
- Property tests validate universal invariants (run 100+ iterations each); unit tests cover specific examples and edge cases — they are complementary
- All property tests must include the feature tag comment: `# Feature: community-scout-austin, Property N: ...`
- No API keys or OAuth credentials are required. Both meetup.com and lu.ma are accessed through the Amazon Bedrock AgentCore Web Search Tool, a fully managed MCP connector backed by Amazon's web index. The Web Search Tool is priced at $7 per 1,000 queries; new AWS accounts receive Free Tier credits. Set `BEDROCK_MODEL_ID` in `.env` if you want a non-default model — that is the only environment variable needed. Never commit `.env` to source control.
- No AWS Secrets Manager or additional AWS infrastructure is required for the MVP

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["2.1", "3.1"] },
    { "id": 2, "tasks": ["3.2"] },
    { "id": 3, "tasks": ["5.1"] },
    { "id": 4, "tasks": ["7.1", "8.1", "9.1"] },
    { "id": 5, "tasks": ["10.1", "11.1"] },
    { "id": 6, "tasks": ["13.1"] },
    { "id": 7, "tasks": ["14.1", "14.2", "14.3"] },
    { "id": 8, "tasks": ["15.1", "15.2", "15.4"] }
  ]
}
```
