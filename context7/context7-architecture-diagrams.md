# Context7 Architecture Diagrams (Review Deck Format)

Repository analyzed: https://github.com/upstash/context7

## 1) Executive View

Context7 (open-source repo scope) is an integration layer that routes documentation questions from AI clients to a hosted API.

```mermaid
flowchart LR
  U[Developer] --> IDE[AI Client / IDE Agent]
  IDE --> INT[Context7 Integration Layer: MCP, CLI, SDK, Tools]
  INT --> API[(Context7 Hosted API)]
  API --> PRIV[Private Backend Engines: Crawler, Parser, Indexer]
  API --> RET[Ranked docs + code snippets]
  RET --> IDE
  IDE --> U
```

Key point: the private backend engines are not in this repo; this repo provides the access and delivery surfaces.

## 2) Monorepo Package Map

```mermaid
flowchart TB
  subgraph Repo[upstash/context7 monorepo]
    MCP[packages/mcp: MCP server]
    CLI[packages/cli: ctx7 setup docs remove]
    SDK[packages/sdk: TypeScript REST client]
    TOOLS[packages/tools-ai-sdk: AI tools and agent]
    PI[packages/pi: pi.dev extension]
    PLUGINS[plugins: client-specific plugin artifacts]
    RULES[rules and skills: agent instruction assets]
  end

  TOOLS --> SDK
  MCP --> API[(context7.com/api)]
  CLI --> API
  SDK --> API
  PI --> API
```

## 3) Primary Runtime Flow (Resolve -> Query)

```mermaid
sequenceDiagram
  participant User
  participant Agent as IDE Agent
  participant Surface as MCP/CLI/SDK Surface
  participant API as Context7 API

  User->>Agent: Ask docs/API question
  Agent->>Surface: resolve-library-id(query, libraryName)
  Surface->>API: GET /v2/libs/search
  API-->>Surface: Ranked library IDs
  Surface-->>Agent: Candidate IDs
  Agent->>Surface: query-docs(libraryId, query)
  Surface->>API: GET /v2/context
  API-->>Surface: Reranked docs + code snippets
  Surface-->>Agent: Text/JSON context
  Agent-->>User: Grounded answer
```

Design invariant: every interface converges on the same two-step retrieval contract.

## 4) MCP Server (HTTP Mode) Lifecycle

```mermaid
flowchart TD
  A[HTTP Request /mcp or /mcp/oauth] --> B{Auth required endpoint?}
  B -- no --> C[Proceed anonymous]
  B -- yes --> D[Extract key/token]
  D --> E{JWT?}
  E -- yes --> F[Validate issuer/audience/scope]
  E -- no --> C
  F --> C

  C --> G{Initialize request?}
  G -- yes --> H[Create session ID + persist in Redis]
  G -- no --> I[Refresh session TTL]
  I --> J{Session exists?}
  J -- no --> K[404 reinitialize]
  J -- yes --> L[Run MCP tool call]
  H --> L

  L --> M[Call Context7 API]
  M --> N[Return tool result]
```

Operational notes:
- GET on MCP endpoint is intentionally rejected (405).
- Session storage is fail-open for resilience.

## 5) CLI Setup/Provisioning Flow

```mermaid
flowchart TD
  S1[ctx7 setup] --> S2[Detect target agents]
  S2 --> S3[Choose mode: MCP or CLI+Skills]
  S3 --> S4[Resolve auth: OAuth or API key]
  S4 --> S5[Write MCP config per client schema]
  S5 --> S6[Install/patch rules]
  S6 --> S7[Install skills]
  S7 --> S8[Ready for auto docs lookup]
```

Client compatibility is encoded as data (per-agent config paths, JSON/TOML keys, and rule placement behavior).

## 6) SDK + Tools Layering

```mermaid
flowchart LR
  APP[App or Agent Runtime] --> TSDK[upstash context7 tools ai sdk]
  TSDK --> C7SDK[upstash context7 sdk]
  C7SDK --> REST[Context7 REST API]

  TSDK --> ORCH[Prompted workflow: resolve first, then query docs]
```

This keeps orchestration policy in tools/prompts while the SDK remains a small typed transport client.

## 7) Security and Identity Signals (Repo-Visible)

```mermaid
flowchart TB
  CLIENT[Client Request] --> HDR[Header extraction: Authorization and key aliases]
  HDR --> JWT{JWT token?}
  JWT -- yes --> ISS[Issuer-specific validation: Clerk or Entra]
  JWT -- no --> PASS[Continue]
  ISS --> PASS

  PASS --> CTX[Attach request context: IP, transport, client info, session]
  CTX --> ENC[Encrypt forwarded client IP]
  ENC --> API[Upstream API call]
```

Also present:
- OAuth discovery metadata endpoints
- Optional auth nudge signal propagation for anonymous users

## 8) Architecture Strengths

- Unified retrieval contract across all client surfaces
- Strong cross-client install automation in CLI
- Protocol flexibility (stdio + HTTP MCP)
- Resilience features for real-world agent/tool-call behavior

## 9) Gaps and Boundaries

- Parser/crawler/indexing internals are private (not reviewable in this repo)
- Full ranking pipeline and storage internals are inferred only from API contracts/docs

## 10) Slide-Ready Summary

- Context7 repo = integration and delivery plane.
- Hosted API = retrieval and ranking plane.
- Private engines = ingestion/indexing plane.
- Core pattern = resolve library ID, then query docs context.
