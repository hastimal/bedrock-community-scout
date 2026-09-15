# Implementation Plan: Community Scout Austin

## Overview

Implement the AI Community Scout as an agentic Python application hosted on Amazon Bedrock AgentCore Runtime. The build proceeds in layers: data models first, then independent tools (Meetup, Luma), then the pipeline components (Aggregator, Deduplicator, Ranker, Response Generator), and finally the Scout Orchestrator that wires everything together under the AgentCore entry point.

## Tasks

- [ ] 1. Scaffold project structure and configuration
  - Create the top-level directory layout: `src/`, `src/models/`, `src/tools/`, `src/pipeline/`, `tests/unit/`, `tests/property/`, `tests/integration/`
  - Add `requirements.txt` pinning `boto3`, `strands-agents`, `bedrock-agentcore`, `requests`, `hypothesis`, `pytest`, `pytest-asyncio`
  - Create `.env.example` documenting all required environment variables: `BEDROCK_MODEL_ID`, `MEETUP_API_KEY`, `LUMA_API_KEY`
  - Create `src/config.py` that reads `BEDROCK_MODEL_ID` (default `us.anthropic.claude-sonnet-4-20250514`), `MEETUP_API_KEY`, and `LUMA_API_KEY` from the environment; expose `get_model_id()`, `get_meetup_api_key()`, and `get_luma_api_key()` helpers; raise `EnvironmentError` at startup if a required key is absent
  - _Requirements: 10.1, 10.3_

- [ ] 2. Implement data models
  - [ ] 2.1 Define `CommonEvent`, `RankedEvent`, `ToolError`, and `QueryContext` dataclasses in `src/models/events.py` with all required and optional fields, type annotations, and docstrings matching the design specification
    - Implement `CommonEvent` invariant: required string fields must be non-empty; `start_datetime` / `end_datetime` accepted only as ISO 8601 UTC strings
    - _Requirements: 5.1, 5.6_

  - [ ]* 2.2 Write unit tests for `CommonEvent` field validation
    - Verify that instantiation with an empty required field raises `ValueError`
    - Verify optional `end_datetime` accepts `None`
    - _Requirements: 5.1, 5.3_

- [ ] 3. Implement the Event Source Tool interface and normalizer
  - [ ] 3.1 Define the `EventSourceTool` `Protocol` in `src/tools/interface.py` with the typed call signature `(topics, location, start_date, end_date) -> list[CommonEvent] | ToolError`
    - _Requirements: 2.1_

  - [ ] 3.2 Implement `normalize_event()` in `src/tools/normalizer.py`
    - Accept a raw platform dict and a `source_platform` string
    - Return a valid `CommonEvent` if all required fields are present and non-empty, `None` otherwise
    - Convert all datetime strings to ISO 8601 UTC ending in `Z`; set optional `end_datetime` to `None` when absent
    - Log skipped records with `{platform, event_identifier, missing_field}`
    - _Requirements: 5.2, 5.3, 5.4, 5.5, 5.6_

  - [ ]* 3.3 Write property test for `normalize_event()` — Property 6
    - **Property 6: Normalization Preserves and Validates Required Fields**
    - **Validates: Requirements 5.1, 5.2, 5.5**
    - `# Feature: community-scout-austin, Property 6: normalization preserves required fields and excludes incomplete records`

  - [ ]* 3.4 Write property test for `normalize_event()` — Property 7
    - **Property 7: ISO 8601 UTC Datetime Format**
    - **Validates: Requirements 5.6**
    - `# Feature: community-scout-austin, Property 7: datetimes are ISO 8601 UTC`

- [ ] 4. Implement the Meetup Tool
  - [ ] 4.1 Create `src/tools/meetup_tool.py`
    - Read the Meetup OAuth 2 bearer token from the `MEETUP_API_KEY` environment variable via `config.get_meetup_api_key()` at tool initialization; raise `EnvironmentError` if the variable is not set

    - Send the GraphQL `keywordSearch` query to `POST https://api.meetup.com/gql-ext` with the topic, city, and date range variables
    - Post-filter results in Python: include only events whose city matches (case-insensitive) the `location` parameter and whose start date falls within `[start_date, end_date]`
    - Pass each raw event node through `normalize_event()` and accumulate valid `CommonEvent` records
    - Return empty list when the platform returns no matching events
    - Return `ToolError(tool_name="MeetupTool", reason=...)` on network timeout or HTTP error; never return partial records
    - Decorate the function with `@tool` (Strands) so AgentCore can invoke it
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 2.1, 2.2_

  - [ ]* 4.2 Write unit tests for `meetup_tool`
    - Mock HTTP responses: valid multi-event response, empty response, HTTP 503 error, network timeout
    - Assert city and date post-filter correctness
    - Assert `ToolError` returned on failure with no partial records
    - _Requirements: 3.2, 3.3, 3.5, 3.6_

- [ ] 5. Implement the Luma Tool
  - [ ] 5.1 Create `src/tools/luma_tool.py`
    - Read the optional Luma API key from `LUMA_API_KEY` environment variable via `config.get_luma_api_key()`; if set, include it as the `x-luma-api-key` header on all requests
    - Issue `GET https://api.lu.ma/discover/events` with `city`, `query` (topic), `start_date`, and `end_date` query parameters
    - Post-filter results in Python: include only events whose city matches (case-insensitive) and whose start date falls within the requested window
    - Pass each raw event through `normalize_event()` and accumulate valid `CommonEvent` records
    - Return empty list when the platform returns no matching events
    - Return `ToolError(tool_name="LumaTool", reason=...)` on network timeout or HTTP error; never return partial records
    - Decorate the function with `@tool` (Strands)
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 2.1, 2.3_

  - [ ]* 5.2 Write unit tests for `luma_tool`
    - Mock HTTP responses: valid multi-event response, empty response, HTTP 503 error, network timeout
    - Assert city and date post-filter correctness
    - Assert `ToolError` returned on failure with no partial records
    - _Requirements: 4.2, 4.3, 4.5, 4.6_

- [ ] 6. Checkpoint — core tools ready
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 7. Implement the Deduplicator
  - [ ] 7.1 Create `src/pipeline/deduplicator.py`
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

- [ ] 8. Implement the Ranker
  - [ ] 8.1 Create `src/pipeline/ranker.py`
    - Implement `rank(events: list[CommonEvent], context: QueryContext) -> list[RankedEvent]`
    - Compute `topic_score = (matched_topics / total_topics) * 60` using case-insensitive substring match of each topic against event title and description
    - Compute `location_score = 20 if event.city in query_locations (case-insensitive) else 0`
    - Compute `recency_score = max(0, 20 * (1 - days_until_event / 365))`, returning 0 for events >365 days away
    - Clamp final score to [0, 100]
    - When no topics and no location were extracted, assign score 0 to all events
    - Sort output by `relevance_score DESC`, then `start_datetime ASC` on ties
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [ ]* 8.2 Write property test for `rank()` — Property 1
    - **Property 1: Relevance Score Formula and Bounds**
    - **Validates: Requirements 7.1, 7.2**
    - `# Feature: community-scout-austin, Property 1: score = clamped(topic_score + location_score + recency_score), result in [0,100]`

  - [ ]* 8.3 Write property test for `rank()` — Property 2
    - **Property 2: Topic Score Monotonicity**
    - **Validates: Requirements 7.2, 7.3, 11.3**
    - `# Feature: community-scout-austin, Property 2: matching more topics gives strictly higher score`

  - [ ]* 8.4 Write property test for `rank()` — Property 8
    - **Property 8: Ranked Output is Sorted Descending by Score then Start Date**
    - **Validates: Requirements 7.4, 7.5**
    - `# Feature: community-scout-austin, Property 8: ranked output sorted by score desc, start date asc on tie`

  - [ ]* 8.5 Write unit tests for `rank()`
    - Event matching all topics, correct city, starting tomorrow → score near 100
    - Event matching no topics, wrong city, starting in 400 days → score = 0
    - Event matching 1 of 2 topics, correct city, 30 days away → verify computed value
    - Zero topics and zero locations extracted → all events score 0
    - Two equal-score events → ordered by ascending start date
    - _Requirements: 7.1, 7.2, 7.4, 7.5_

- [ ] 9. Implement the Event Aggregator
  - [ ] 9.1 Create `src/pipeline/aggregator.py`
    - Implement `aggregate(tool_results: list[list[CommonEvent] | ToolError]) -> tuple[list[CommonEvent], list[ToolError]]`
    - Flatten all `list[CommonEvent]` results into a single list (preserving `source_invocation_order`)
    - Collect all `ToolError` entries into a separate list
    - _Requirements: 2.4, 11.1_

  - [ ]* 9.2 Write property test for `aggregate()` — Property 11
    - **Property 11: Multi-Location Aggregation Completeness**
    - **Validates: Requirements 11.1**
    - `# Feature: community-scout-austin, Property 11: all events from all locations are aggregated`

- [ ] 10. Implement the Response Generator
  - [ ] 10.1 Create `src/pipeline/response_generator.py`
    - Implement `format_response(ranked_events: list[RankedEvent], context: QueryContext, errors: list[ToolError]) -> str`
    - Emit total event count (post-deduplication) at the top
    - For single-location queries, list events sequentially; for multi-location queries, group events under labeled location sections
    - For each event render: title, source platform, city, start date as `YYYY-MM-DD`, and `event_url` as a markdown hyperlink `[title](url)`
    - Render "N/A" for any missing required display field
    - Append a footer listing failed tools and their reasons when `errors` is non-empty
    - When `ranked_events` is empty, return a "no events found" message naming the topics, locations, and time window, and suggest broadening the search
    - Never alter, truncate, or omit any `event_url`
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 11.4_

  - [ ]* 10.2 Write unit tests for `format_response()`
    - Missing display field → "N/A"
    - Multi-location query → output grouped by location with labels
    - Zero events → user-facing "no events found" message with suggestions
    - Event URLs appear unmodified as markdown hyperlinks
    - Non-empty errors → failure footer present
    - _Requirements: 8.1, 8.3, 8.4, 8.5_

- [ ] 11. Implement query validation helpers
  - [ ] 11.1 Create `src/validation.py`
    - Implement `validate_query(raw: str) -> None | str` returning an error message string when: input is empty or whitespace-only; extracted topics exceed 10; extracted locations exceed 5
    - Implement `validate_time_window(start: str, end: str) -> tuple[str, str]` that rejects windows exceeding 5 years and falls back to the default 90-day window with an informational note
    - _Requirements: 1.3, 1.4, 1.5, 1.6, 11.6_

  - [ ]* 11.2 Write unit tests for `validate_query()` and `validate_time_window()`
    - Empty string → error, no tool invocation
    - Whitespace-only string → error, no tool invocation
    - Query with no extractable topic → clarification message for missing topic
    - Query with no extractable location → clarification message for missing location
    - 6 locations → rejection error
    - 11 topics → rejection error
    - Time window > 5 years → fallback to 90-day window with informational message
    - _Requirements: 1.3, 1.4, 1.5, 1.6, 11.6_

- [ ] 12. Checkpoint — pipeline components ready
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 13. Implement the Scout Orchestrator and wire all components
  - [ ] 13.1 Create `src/scout.py`
    - Instantiate `BedrockAgentCoreApp` and decorate the handler with `@app.entrypoint`
    - Read `model_id` from `config.get_model_id()` at handler invocation time (not at import time)
    - Construct a Strands `Agent` with the system prompt, `model_id`, and `tools=[meetup_tool, luma_tool]`
    - System prompt instructs the LLM to: extract topics, locations, and time window; invoke tools per location; never fabricate event data; request clarification when topic or location cannot be extracted; reject oversized queries; apply default 90-day window when none is provided
    - After the LLM tool-use loop completes, pipe results through `aggregate()` → `deduplicate()` → `rank()` → `format_response()` and yield the final formatted string
    - Surface `ToolError` entries in the response footer without aborting the session
    - Return an error message (not partial results) if the AgentCore session fails to establish
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 11.1, 11.2, 11.5_

  - [ ]* 13.2 Write unit tests for Scout Orchestrator query validation paths
    - Empty query → error returned before any tool call
    - Missing topic → clarification message returned
    - Missing location → clarification message returned
    - > 5 locations → rejection error before tool invocation
    - > 10 topics → rejection error before tool invocation
    - _Requirements: 1.5, 1.6, 11.6_

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
    - Generate tool invocation parameters and raw platform responses with varied event dates; assert every `CommonEvent` in filtered output has `start_datetime.date` in `[start_date, end_date]`
    - **Property 9: Time Window Filtering**
    - **Validates: Requirements 3.2, 4.2**
    - `# Feature: community-scout-austin, Property 9: all output events are within the requested time window`

- [ ] 15. Implement integration tests
  - [ ] 15.1 Write end-to-end integration test with mocked tool responses
    - Submit a known query against mocked Meetup and Luma responses; assert ranked output contains correct events in expected order with correct structure
    - _Requirements: 10.4_

  - [ ] 15.2 Write integration test for AgentCore tool timeout
    - Mock a tool that sleeps >10 s; assert the Scout returns a partial result with an error note and does not raise an exception
    - _Requirements: 2.5, 10.5_

  - [ ] 15.3 Write integration test for multi-location partial failure
    - Mock one tool to fail for one location; assert results from the successful location are returned alongside a failure note for the failing location
    - _Requirements: 11.5_

  - [ ] 15.4 Write integration test for zero results
    - Mock both tools to return empty lists; assert the user receives the "no events found" message with suggestions and a count of 0
    - _Requirements: 8.4, 9.3_

- [ ] 16. Final checkpoint — all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for the demo MVP
- Each task references specific requirements for traceability
- Checkpoints at tasks 6, 12, and 16 provide incremental validation gates
- Property tests validate universal invariants (run 100+ iterations each); unit tests cover specific examples and edge cases — they are complementary
- All property tests must include the feature tag comment: `# Feature: community-scout-austin, Property N: ...`
- API credentials are loaded from environment variables (`MEETUP_API_KEY`, `LUMA_API_KEY`); copy `.env.example` to `.env` and populate before running locally — never commit `.env` to source control
- `BEDROCK_MODEL_ID` is read at handler invocation time so model changes take effect without restarting the runtime
- No AWS Secrets Manager or additional AWS infrastructure is required for the MVP

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["2.1", "3.1"] },
    { "id": 2, "tasks": ["3.2"] },
    { "id": 3, "tasks": ["4.1", "5.1"] },
    { "id": 4, "tasks": ["7.1", "8.1", "9.1"] },
    { "id": 5, "tasks": ["10.1", "11.1"] },
    { "id": 6, "tasks": ["13.1"] },
    { "id": 7, "tasks": ["14.1", "14.2", "14.3"] },
    { "id": 8, "tasks": ["15.1", "15.2", "15.3", "15.4"] }
  ]
}
```
