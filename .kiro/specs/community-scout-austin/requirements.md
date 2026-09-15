# Requirements Document

## Introduction

The AI Community Scout is an agentic AI application that helps technology professionals discover upcoming technology community events — including meetups, tech talks, and community gatherings — within a specified geographic area and time window.

For the v0.1 MVP, the Scout accepts a natural-language query from a user, interprets the topics of interest, and uses the Amazon Bedrock AgentCore Web Search Tool — with domain filters scoped to meetup.com and lu.ma — to retrieve publicly indexed event pages for Austin, Texas. The LLM extracts event data from the returned snippets, which is then normalized into a common event model, deduplicated, ranked by relevance, and returned as a list with links to the original source pages. No API keys, OAuth credentials, or third-party integrations are required. The architecture is extensible: additional sources can be added by expanding the domain filter list.

The system is constrained to facts returned by the event-source tools. It must never fabricate events, dates, locations, or URLs.

---

## Glossary

- **Scout**: The top-level agentic AI system that orchestrates the end-to-end community event discovery workflow.
- **User**: A technology professional who submits a natural-language query to the Scout.
- **Query**: A natural-language request that specifies one or more topics of interest and an optional time window. For v0.1, the location is fixed to Austin, Texas and need not be specified by the User.
- **Event_Source_Tool**: A modular, independently invokable tool that fetches event data and returns results conforming to the Common_Event_Model. In v0.1 the only active source is `AgentCoreWebSearchEventSource`; the interface is designed to accommodate additional tools in future versions.
- **AgentCoreWebSearchEventSource**: The single Event_Source_Tool implementation for v0.1. It invokes the Amazon Bedrock AgentCore Web Search Tool with a domain filter of `{include: ['meetup.com', 'lu.ma']}`, targeting publicly indexed event pages on both platforms. All domain configuration is isolated inside this component.
- **AgentCore_Web_Search_Tool**: The fully managed, MCP-compliant web search capability built into Amazon Bedrock AgentCore. Returns ranked snippets, source URLs, titles, and publication dates from Amazon's web index. No external API key or credential is required.
- **Common_Event_Model**: The normalized data structure that every Event_Source_Tool must populate and that the Scout uses for ranking and response generation.
- **Ranker**: The Scout sub-component that scores and orders events by their relevance to the User's Query.
- **Response_Generator**: The Scout sub-component that formats the ranked event list into a human-readable reply.
- **Bedrock_Agent**: The Amazon Bedrock AgentCore-hosted runtime that executes the Scout's agentic workflow.
- **Topic**: A technology subject or keyword extracted from the Query (e.g., "Agentic AI", "AWS", "Kubernetes").
- **Location**: A city or metropolitan area. For v0.1, the Location is fixed to Austin, Texas.
- **Time_Window**: The future date range extracted from the Query, defaulting to 90 days from the current date when not explicitly stated.
- **Relevance_Score**: A numeric value computed by the Ranker in the range 0–100 that reflects how closely an event matches the Topics in the Query and how soon it occurs. Location is not scored in v0.1 because every accepted event is already in Austin, Texas.
- **Event_URL**: The canonical, unmodified URL of an event as returned by the originating Event_Source_Tool.
- **Deduplication**: The process of identifying and removing duplicate events that appear in results from more than one Event_Source_Tool.

---

## Requirements

### Requirement 1: Natural-Language Query Interpretation

**User Story:** As a technology professional, I want to describe what I am looking for in plain language, so that I do not need to learn a structured query syntax.

#### Acceptance Criteria

1. WHEN a User submits a Query, THE Scout SHALL extract one or more Topics from the Query.
2. WHEN a User submits a Query, THE Scout SHALL use Austin, Texas as the fixed Location for v0.1; the Scout SHALL NOT require the User to specify a location.
3. WHEN a User submits a Query that contains an explicit time range — whether expressed as absolute dates or relative expressions such as "next 30 days" or "next month" — THE Scout SHALL use that range as the Time_Window, provided the range does not exceed 5 years from the current date.
4. WHEN a User submits a Query that does not contain an explicit time range, THE Scout SHALL default the Time_Window to 90 calendar days starting from the current date.
5. IF the Scout cannot extract at least one Topic from a Query, THEN THE Scout SHALL return a clarification message identifying that a topic is needed.
6. IF a User submits a Query that contains no extractable tokens (e.g., an empty string or only whitespace), THEN THE Scout SHALL return an error message before attempting any extraction.

---

### Requirement 2: Modular Event-Source Tool Architecture

**User Story:** As a developer, I want each event source implemented as a self-contained tool, so that new sources can be added in future versions without modifying the core Scout logic.

#### Acceptance Criteria

1. THE Scout SHALL invoke each Event_Source_Tool independently via a defined tool interface that accepts one or more Topics (each 1–200 characters) and a Time_Window (1–90 days), and returns a list of zero or more Common_Event_Model records. The Location is fixed to Austin, Texas in v0.1 and is not a caller-supplied parameter.
2. THE AgentCoreWebSearchEventSource SHALL implement the Event_Source_Tool interface for v0.1, using the AgentCore Web Search Tool with domain filters scoped to meetup.com and lu.ma.
3. WHEN a new Event_Source_Tool is registered with the Scout, THE Scout SHALL invoke it using the same tool interface as existing Event_Source_Tools and include its returned results in the aggregated event set alongside results from other tools.
4. IF an Event_Source_Tool does not respond within 10 seconds or returns an error response, THEN THE Scout SHALL record an error entry containing the tool name and the failure reason, exclude that tool's results from the response, and continue processing results from the remaining Event_Source_Tools.

---

### Requirement 3: AgentCore Web Search Event Retrieval

**User Story:** As a User, I want events from meetup.com and lu.ma included in my results, so that I can discover tech events in Austin from multiple platforms without any API credentials or platform-specific integrations.

#### Acceptance Criteria

1. WHEN invoked with one or more Topics and a Time_Window, THE AgentCoreWebSearchEventSource SHALL invoke the Amazon Bedrock AgentCore Web Search Tool with a query constructed from the Topics and Austin, Texas, and a domain filter set to `{"include": ["meetup.com", "lu.ma"]}`.
2. THE AgentCoreWebSearchEventSource SHALL embed the Time_Window in the search query text (e.g., 'events in Austin Texas between {start_date} and {end_date}') so that the Web Search Tool can return relevant pages; THE AgentCoreWebSearchEventSource SHALL then post-filter the extracted results by the verified event start date, retaining only events whose start date falls within the Time_Window. The page publication date SHALL NOT be used as a substitute for the event start date.
3. THE AgentCoreWebSearchEventSource SHALL extract event title, event date, city, source platform name, and Event_URL from each snippet returned by the Web Search Tool using only facts explicitly present in the snippet text and its source URL. IF any required field (title, event date, city, or Event_URL) cannot be reliably extracted from the evidence, THEN THE AgentCoreWebSearchEventSource SHALL exclude that result and not fabricate, infer, or substitute the missing field.
4. THE AgentCoreWebSearchEventSource SHALL populate the Event_URL field of each Common_Event_Model record with the unmodified source URL returned by the Web Search Tool.
5. THE AgentCoreWebSearchEventSource SHALL return only events whose extracted start date falls within the Time_Window.
6. IF the Web Search Tool returns no results for the given query and domain filter, THEN THE AgentCoreWebSearchEventSource SHALL return an empty list.
7. IF the Web Search Tool returns an error or is unavailable, THEN THE AgentCoreWebSearchEventSource SHALL return a ToolError to the Scout without returning any partial event records.
8. THE AgentCoreWebSearchEventSource SHALL isolate the Web Search Tool endpoint, domain filter configuration, query construction, and snippet parsing logic within the AgentCoreWebSearchEventSource class so that they can be changed (e.g., adding new domains) without modifying any other component.

---

### Requirement 4: Future Event Source Integration

**User Story:** As a developer, I want to add new event sources in future versions, so that users can discover events from additional platforms without requiring changes to the core Scout logic.

#### Acceptance Criteria

1. WHEN a new Event_Source_Tool is implemented (e.g., MeetupEventSource, EventbriteEventSource), THE Scout SHALL invoke it using the same EventSourceTool Protocol interface defined in Requirement 2, without modifying the normalizer, deduplicator, ranker, or response generator.

---

### Requirement 5: Common Event Model Normalization

**User Story:** As a developer, I want all event data normalized into a single structure, so that the Scout can process results from multiple sources consistently.

#### Acceptance Criteria

1. THE Common_Event_Model SHALL contain the following fields:

   **Required fields:**
   - event title
   - event start date and time in UTC
   - city
   - source platform name
   - Event_URL

   **Optional fields:**
   - event description
   - event end date and time in UTC
   - location name

   IF a source platform does not provide a value for any required field, THE Event_Source_Tool SHALL exclude that event from the output. IF a source platform does not provide a value for an optional field, THE Event_Source_Tool SHALL set that field to null.
2. WHEN an Event_Source_Tool returns an event, THE Event_Source_Tool SHALL populate all required Common_Event_Model fields using only data returned by the source platform.
3. IF a source platform does not provide a value for an optional Common_Event_Model field, THEN THE Event_Source_Tool SHALL set that field to null.
4. THE Scout SHALL NOT modify the Event_URL field of any Common_Event_Model record after it has been populated by an Event_Source_Tool.
5. IF a source platform does not provide a value for a required Common_Event_Model field, THEN THE Event_Source_Tool SHALL exclude that event record from the normalized output and record an indication that the event was skipped due to missing required data.
6. THE Common_Event_Model SHALL represent event start date and time and event end date and time as ISO 8601 formatted strings in UTC (e.g., YYYY-MM-DDTHH:MM:SSZ), with no timezone offset other than Z.

---

### Requirement 6: Deduplication

**User Story:** As a User, I want duplicate events removed from my results, so that each event appears only once regardless of how many sources returned it.

#### Acceptance Criteria

1. WHEN the Scout collects results from all Event_Source_Tools, THE Scout SHALL identify duplicate events by comparing event title, start date, and city across all records using case-insensitive exact matching after stripping leading and trailing whitespace.
2. WHEN two records are identified as duplicates, THE Scout SHALL retain the record whose Event_URL originates from the Event_Source_Tool that returned the highest match confidence score for that result, and discard the other.
3. IF a duplicate record's Event_Source_Tool did not return a match confidence score, THEN THE Scout SHALL treat that record's match confidence as 0 when comparing against other duplicate records.
4. WHEN two duplicate records have equal match confidence scores, THE Scout SHALL retain the record from the Event_Source_Tool that was invoked earliest in the tool invocation sequence, and discard the other.

---

### Requirement 7: Relevance Ranking

**User Story:** As a User, I want results ordered by how closely they match my interests, so that the most relevant events appear first.

#### Acceptance Criteria

1. WHEN the Scout has collected and deduplicated event results, THE Ranker SHALL assign a Relevance_Score in the range 0 to 100 to each event.
2. THE Ranker SHALL compute the Relevance_Score as a weighted sum: topic match contributes up to 80 points based on the number of Topics extracted from the Query that appear in the event title or description divided by the total number of extracted Topics, and recency contributes up to 20 points inversely proportional to the number of days between the current date and the event start date, capped at 0 for events more than 365 days away. Location is not scored in v0.1 because every accepted event is already in Austin, Texas.
3. IF no Topics are extracted from the Query, THEN THE Ranker SHALL assign a Relevance_Score of 0 to all events.
4. THE Scout SHALL return events ordered from highest Relevance_Score to lowest Relevance_Score.
5. WHEN two events have equal Relevance_Scores, THE Scout SHALL order them by ascending start date.

---

### Requirement 8: Response Generation

**User Story:** As a User, I want a clear, readable list of results, so that I can quickly evaluate and act on each opportunity.

#### Acceptance Criteria

1. WHEN the Scout has ranked events, THE Response_Generator SHALL produce a response that includes, for each event: the event title, the source platform name, the city, the start date formatted as YYYY-MM-DD, and the Event_URL rendered as a markdown hyperlink; if a required display field is absent from an event record, THE Response_Generator SHALL display "N/A" for that field.
2. THE Response_Generator SHALL present events in the ranked order determined by the Ranker.
3. THE Response_Generator SHALL include a count of total events returned, calculated after deduplication, at the top of the response.
4. IF the combined results from all Event_Source_Tools contain zero events after deduplication, THEN THE Response_Generator SHALL inform the User that no events were found for the specified Topics and Time_Window in Austin, Texas, and suggest broadening the search criteria.
5. THE Response_Generator SHALL NOT add, modify, or omit any Event_URL from the events returned by the Event_Source_Tools.

---

### Requirement 9: Factual Accuracy Constraint

**User Story:** As a User, I want to trust that every event in my results is real, so that I do not waste time following up on fabricated information.

#### Acceptance Criteria

1. THE Scout SHALL NOT generate, infer, or hallucinate any event title, event description, event date, event location, venue, or Event_URL that was not explicitly present in the evidence returned by an Event_Source_Tool. IF a required field cannot be reliably extracted from the evidence, THE Scout SHALL exclude that result rather than inferring or substituting a value.
2. WHEN an Event_Source_Tool returns an event, THE Scout SHALL preserve all event facts exactly as provided by the source platform, with the following deterministic normalizations permitted:
   - Convert event date and time to ISO 8601 UTC format
   - Normalize whitespace (leading/trailing whitespace removal, collapsing internal whitespace)
   - Derive source platform name from the source URL domain (e.g., "meetup.com" → "Meetup", "lu.ma" → "Luma")
   
   The event_url field SHALL always remain byte-for-byte unchanged.
3. IF no Event_Source_Tool returns results for a given Query, THEN THE Scout SHALL display a message to the User indicating that no events were found for that Query, without generating any placeholder or fabricated event records.

---

### Requirement 10: Amazon Bedrock AgentCore Runtime

**User Story:** As a developer, I want the Scout to run on Amazon Bedrock AgentCore, so that it benefits from a managed, production-grade agentic runtime without requiring custom orchestration infrastructure.

#### Acceptance Criteria

1. THE Bedrock_Agent SHALL execute the Scout's agentic workflow using the Amazon Bedrock AgentCore runtime such that all tool invocations and session management are handled by AgentCore infrastructure.
2. THE Bedrock_Agent SHALL invoke Event_Source_Tools as Bedrock AgentCore tool actions.
3. THE Bedrock_Agent SHALL use an Amazon Bedrock foundation model to perform Query interpretation and Response_Generator formatting, and SHALL use the Ranker to perform Relevance_Score computation.
4. WHEN the Bedrock_Agent receives a Query, THE Bedrock_Agent SHALL complete the full discovery workflow — tool invocation, normalization, deduplication, ranking, and response generation — within a single agentic session not exceeding 120 seconds.
5. IF the Amazon Bedrock AgentCore runtime returns an error for a tool invocation, THEN THE Bedrock_Agent SHALL follow the error handling behavior defined in Requirement 2, Acceptance Criterion 4.
6. IF the Amazon Bedrock AgentCore runtime fails to establish or maintain the agentic session, THEN THE Bedrock_Agent SHALL return an error message to the User indicating that the session could not be completed, without returning partial results.

---

### Requirement 11: Multi-Topic Query Support

**User Story:** As a User, I want to search across multiple topics in a single query, so that I can discover events covering any of my interests without submitting separate queries.

#### Acceptance Criteria

1. WHEN a Query contains more than one Topic, THE Scout SHALL pass all Topics to the Event_Source_Tool invocation so that results matching any of the Topics are included.
2. THE Ranker SHALL assign a higher Relevance_Score to an event for each additional Topic from the Query it matches, such that an event matching N Topics receives a strictly higher score than an equivalent event matching fewer than N Topics.
3. THE Response_Generator SHALL list events sequentially in a single result list for the Austin, Texas location.
4. IF the Event_Source_Tool invocation fails, THEN THE Scout SHALL indicate in the response that the source could not be retrieved.
5. IF a Query contains more than 10 Topics, THEN THE Scout SHALL reject the Query and return an error message indicating the exceeded limit before invoking any Event_Source_Tool.
