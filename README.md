# Bedrock Community Scout

An Agentic AI application for discovering technology community opportunities using Amazon Bedrock and AWS. 

The project is designed to help users discover:

- Technology events
- Meetups
- Calls for Papers (CFPs)
- Speaker opportunities
- Hackathons
- Judging opportunities
- Community programs
- Location-based opportunities

## Goal

The goal is to build an AI community scout that can search multiple sources, understand user interests and location, rank relevant opportunities, and provide direct links for taking action.

## High-Level Architecture

```text
User
  |
  v
Opportunity Request
  |
  v
AI Agent
  |
  +--> Event Sources
  +--> CFP Sources
  +--> Meetup Sources
  +--> Community Sources
  |
  v
Filter + Rank
  |
  v
Relevant Opportunities
  |
  v
Registration / Application Links
```

## Example Queries

```text
Find AI conferences looking for speakers in Texas.

Find Kubernetes meetups near Austin.

Find hackathons where I can volunteer as a judge.

Find open CFPs for Agentic AI talks.

Find cloud and AI events in Houston this month.
```

## Planned Capabilities

- Amazon Bedrock
- Agentic workflows
- Location-aware search
- Opportunity classification
- CFP discovery
- Event ranking
- Deduplication
- Source citations
- Registration links
- User preferences
- Community opportunity recommendations

## Roadmap

- v0.1 - Project Foundation
- v0.2 - Event Discovery
- v0.3 - CFP Discovery
- v0.4 - Opportunity Ranking
- v0.5 - Location-Aware Search
- v0.6 - Multi-Source Agent
- v0.7 - Web Interface
- v1.0 - Community Scout

## Author

**Hastimal Jangid**

Cloud, Data & AI Architect | Researcher | IEEE Senior Member

Website: https://hastimal.github.io  
GitHub: https://github.com/hastimal
