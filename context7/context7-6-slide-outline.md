# Context7 Architecture Review: 6-Slide Outline

## Slide 1: What Context7 Is

### Title
Context7 Architecture Overview

### Core bullets
- Context7 delivers up-to-date, version-specific docs and code snippets to AI coding assistants.
- Open-source repository scope is integration and delivery, not full backend internals.
- Core interaction pattern: resolve library ID, then query docs context.

### Speaker note (optional)
Set expectations early: this review explains the open-source architecture and clearly separates what is inside this repo versus private backend services.

---

## Slide 2: System Boundaries (Open vs Private)

### Title
Architecture Boundary and Ownership

### Core bullets
- Open-source in repo: MCP server, CLI, SDK, AI SDK wrappers, pi extension, plugins/rules/skills assets.
- Private (not in repo): parsing engine, crawling pipeline, indexing/ranking internals.
- Hosted API is the stable contract between open clients and private retrieval engines.

### Suggested visual
Simple 3-layer diagram:
- Layer 1: AI clients and integrations (open)
- Layer 2: Context7 hosted API (public contract)
- Layer 3: backend ingestion/ranking systems (private)

### Speaker note (optional)
This boundary explains why client behavior is inspectable while backend retrieval quality logic is mostly inferred from API behavior and docs.

---

## Slide 3: Runtime Request Flow

### Title
End-to-End Query Lifecycle

### Core bullets
- Step 1: agent calls resolve-library-id with user intent and library name.
- Step 2: integration calls GET /api/v2/libs/search and ranks candidate library IDs.
- Step 3: agent calls query-docs using chosen library ID.
- Step 4: integration calls GET /api/v2/context and returns reranked docs/snippets.
- Result: grounded answer with current API semantics.

### Suggested visual
Sequence: User -> Agent -> Context7 surface (MCP/CLI/SDK) -> Hosted API -> Agent -> User.

### Speaker note (optional)
Emphasize that this two-step contract is reused across every client surface, reducing behavioral drift across tools.

---

## Slide 4: Package Architecture Map

### Title
Monorepo Component Design

### Core bullets
- packages/mcp: protocol gateway for MCP clients (stdio + HTTP modes).
- packages/cli: setup/remove automation, docs lookup commands, auth bootstrapping.
- packages/sdk: typed HTTP client abstraction for product integrations.
- packages/tools-ai-sdk: Vercel AI SDK-native tools and preconfigured workflow agent.
- packages/pi: pi.dev-native tool adapter with MCP-parity semantics.
- plugins + rules + skills: distribution assets for cross-agent onboarding.

### Speaker note (optional)
Design pattern: small focused packages that converge on the same API contracts, instead of one monolithic binary.

---

## Slide 5: Operational and Security Design

### Title
Operations, Auth, and Reliability Signals

### Core bullets
- MCP supports both stdio and streamable HTTP for deployment flexibility.
- HTTP mode includes explicit session lifecycle with Redis-backed session records.
- Authentication supports API keys and JWT validation flows (issuer-aware checks).
- Request context carries transport/client metadata; client IP forwarding is encrypted.
- CLI setup handles heterogeneous config formats (JSON/JSONC/TOML) for multiple clients.

### Speaker note (optional)
Highlight practical engineering maturity: protocol handling, auth pathways, and setup ergonomics are treated as first-class concerns.

---

## Slide 6: Strategic Takeaways and Recommendations

### Title
What This Means for Adoption

### Core bullets
- Strength: consistent resolve-then-query contract across MCP, CLI, SDK, and wrappers.
- Strength: strong client ecosystem coverage through automated setup and plugin artifacts.
- Constraint: backend ingestion/ranking internals are private, so deep retrieval tuning visibility is limited.
- Recommendation for teams:
  - IDE-first teams: CLI setup + MCP runtime
  - Product teams: TypeScript SDK (add tools-ai-sdk for rapid agent workflows)
  - Platform teams: standardize rollout with CLI rules/skills and config automation

### Closing line
Context7 is architected as an integration plane around a centralized docs retrieval API, optimized for multi-agent compatibility and fast developer adoption.

---

## Optional Appendix (If You Have 2 Extra Minutes)

### A. One-slide risk snapshot
- Dependency on hosted API availability
- Private backend internals reduce inspectability
- Multi-client config drift risk mitigated by installer automation

### B. One-slide decision matrix
- Fastest onboarding: CLI + MCP
- Most control: TypeScript SDK
- Fastest agent workflow prototyping: tools-ai-sdk
