# Kiro Prompts — Building Community Scout

These representative prompts show how Community Scout was built incrementally using
Kiro's spec-driven development workflow. They are reusable and copy/paste friendly.

The specifications in
[`.kiro/specs/community-scout-austin/`](../../.kiro/specs/community-scout-austin/)
remain the engineering source of truth; these prompts illustrate the build sequence that
produced and refined them.

Kiro helps build the application and the AWS infrastructure as code. Kiro is not part of
the deployed Runtime.

---

## Prompt 1 — Define the Application

```text
Create an AI "Community Scout" application, spec-first.

Goal: given a natural-language request such as
"Find Agentic AI, AWS, and Kubernetes community events in Austin during the next 90 days."
the agent discovers relevant technology community events.

Scope and constraints:
- MVP location is Austin, Texas.
- Default time window is the next 90 days.
- Topics of interest: Agentic AI, AWS, Kubernetes.
- Use ONLY public indexed web evidence for discovery.
- Target public event pages from Meetup (meetup.com) and Luma (lu.ma).
- Do NOT use private Meetup/Luma APIs and do NOT scrape those sites directly.
- Never fabricate an event title, date, location, or URL.
- Every accepted event must be supported by evidence; preserve source URLs byte-for-byte.
- Normalize all discovered events into a single CommonEvent model so multiple sources
  share one downstream pipeline.
- All filtering, deduplication, and ranking must be deterministic (pure Python), not
  model-generated ordering.

Deliverables for this stage:
- Create requirements.md, design.md, and tasks.md under
  .kiro/specs/community-scout-austin/.
- Do NOT implement application code yet — specifications only.
```

---

## Prompt 2 — Build the Evidence-Grounded Pipeline

```text
Implement the deterministic, evidence-grounded event pipeline defined in the spec.

Pipeline stages:
  CommonEvent
    → evidence extraction (title/date/city/url derived ONLY from evidence)
    → topic relevance validation (event's own evidence must match a requested topic)
    → Austin location validation
    → date-window validation (event start date within the requested window;
      a page publication date is never used as the event date)
    → normalization into CommonEvent
    → deterministic deduplication (normalized key; preserve event_url byte-for-byte)
    → deterministic ranking (topic + recency; pure Python, reproducible)
    → grounded response formatting (clickable original URLs)

Rules:
- No event fabrication; prefer returning zero results over unrelated results.
- Keep each stage independently testable.

Add unit and property-based tests covering extraction, relevance, location/date
validation, normalization, deduplication, URL preservation, and ranking determinism.
```

---

## Prompt 3 — Connect AgentCore Web Search

```text
Wire event discovery to Amazon Bedrock AgentCore Web Search through an AgentCore Gateway.

Requirements:
- Use the managed AgentCore Web Search tool exposed via an AgentCore Gateway.
- Authenticate to the Gateway with AWS IAM (SigV4). No API keys or OAuth.
- Discover ONLY public indexed web evidence (no private APIs, no direct scraping).
- Perform source-aware searches: query Meetup and Luma INDEPENDENTLY (one domain-scoped
  call per source) so one source does not crowd the other out of the result set.
- Scope each call with domainFilter.include for the target source (meetup.com / lu.ma).
- Embed the requested date range in the search query text.
- Apply deterministic post-filtering (relevance, Austin, date window) to the returned
  evidence.
- Document clearly that source support does NOT guarantee source representation:
  a given query may return results from one, both, or neither source, and indexed
  evidence changes over time.

Add tests for source-aware discovery, domain scoping, and deterministic post-filtering.
```

---

## Prompt 4 — Build the Strands + Amazon Bedrock Agent

```text
Build the agent layer that drives the tool-use loop.

Requirements:
- Use Strands Agents to orchestrate the loop.
- Use Amazon Bedrock with model id us.anthropic.claude-sonnet-4-6.
- The model performs reasoning and tool orchestration (query interpretation and tool
  invocation) only.
- The deterministic Python application pipeline remains the source of truth for accepted
  events — the model must not invent events or reorder final results.
- Suppress intermediate model/callback streaming output so it never reaches the user;
  only the deterministic grounded response is returned.

Add tests confirming that intermediate model text is suppressed while tool results are
still collected and formatted by the deterministic pipeline.
```

---

## Prompt 5 — Deploy to AgentCore Runtime

```text
Add AWS deployment for the agent using CloudFormation and direct-code Python deployment
to Amazon Bedrock AgentCore Runtime.

Requirements:
- CloudFormation templates for the Runtime (and its execution role).
- Direct-code Python deployment (Python 3.12), packaged from source — no container/ECR.
- Provide a root-level runtime_entrypoint.py that re-exposes the existing app.
- Provide scripts/package-runtime.sh to build the deployment ZIP.
- Provide scripts/deploy-runtime.sh to package, upload, and deploy.
- Least-privilege IAM execution role granting only:
    - Bedrock model invocation for the configured model
    - bedrock-agentcore:InvokeGateway on the specific gateway
    - scoped CloudWatch Logs
    - read of the specific S3 code artifact
  with confused-deputy protection (aws:SourceAccount / aws:SourceArn).
- Upload each build under a UNIQUE, IMMUTABLE S3 key:
    community-scout-runtime/<git-sha>-<utc-timestamp>.zip
  so CloudFormation detects the changed CodeConfiguration and creates a new Runtime
  version.
- Enable S3 bucket versioning and block public access on the artifact bucket.
- No credentials in source; resolve AWS credentials from the standard chain.

Validate the templates and confirm no account IDs/ARNs/credentials are committed.
```

---

## Prompt 6 — Add the Streamlit UI

```text
Add a minimal Streamlit demo UI.

Requirements:
- Create ui/app.py and requirements-ui.txt (kept OUT of the Runtime deployment artifact).
- Create scripts/run-ui.sh to launch it.
- Controls: topic selection for Agentic AI, AWS, and Kubernetes (all selected by
  default), location fixed to Austin, Texas, and a "Next 90 days" window.
- A "Find Opportunities" button invokes the DEPLOYED AgentCore Runtime via boto3
  (invoke_agent_runtime) using the standard AWS credential chain; configurable via
  AWS_REGION and AGENTCORE_RUNTIME_ARN.
- Safely decode the SSE-style Runtime response (data: "..." with JSON-escaped content)
  without eval; render the result as Markdown with clickable original URLs.
- The UI MUST NOT duplicate any search, filtering, ranking, or deduplication logic — it
  is a thin presentation layer only.

Add tests for the UI response decoding (SSE framing, JSON unescaping, URL preservation).
```
