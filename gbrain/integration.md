# GBrain Integration Guide

## What GBrain is (for RAG)

GBrain is a Postgres-native personal/team knowledge brain with a CLI and MCP server.
Its retrieval pipeline combines vector search (HNSW/pgvector) + BM25 keyword search via
reciprocal-rank fusion, graph-signal boosting from a self-wiring entity graph, and optional
query rewriting — making it a full RAG backend you can drop into any MCP-compatible
application without building retrieval yourself.

---

## Setup

### Install

```bash
git clone https://github.com/garrytan/gbrain.git
cd gbrain && bun install && bun link
```

### Initialise a brain

```bash
# Local dev — PGLite (embedded Postgres WASM, zero-config, up to ~50K pages)
gbrain init --pglite

# Team / production — Postgres + pgvector (Supabase or self-hosted)
gbrain init --postgres --connection-string "postgresql://..."
```

### Migrate between engines later

```bash
gbrain migrate --to supabase    # lossless; keeps pages, chunks, embeddings, links
gbrain migrate --to pglite
```

---

## Configuring the Embedding Provider

GBrain auto-detects from env vars, or you can be explicit:

```bash
# Auto-detect (reads OPENAI_API_KEY, VOYAGE_API_KEY, GOOGLE_API_KEY, etc.)
gbrain init --pglite

# Explicit
gbrain init --pglite --embedding-model openai:text-embedding-3-small
gbrain init --pglite --embedding-model voyage:voyage-3
gbrain init --pglite --embedding-model ollama:nomic-embed-text   # local
gbrain init --pglite --embedding-model litellm:my-proxy/model    # LiteLLM proxy
```

Supported providers: OpenAI, ZeroEntropy, Voyage, OpenRouter, Google Gemini,
Azure OpenAI, MiniMax, Alibaba DashScope, Zhipu, local Ollama, llama.cpp `llama-server`.

---

## Document Ingestion

### Native format: Markdown

GBrain's source of truth is a **git repo of markdown files**. The import command
bulk-loads a directory, chunks each file, and auto-embeds in one pass (~30 s for
text, 10–15 min for embedding large corpora):

```bash
gbrain import /path/to/markdown/wiki/
gbrain import ~/notes/ --no-embed   # defer embedding to later
gbrain sync                          # re-sync after edits (content-hash idempotent)
```

Single-file / stdin capture:

```bash
gbrain capture "Quick thought to remember"
gbrain capture --file ./notes/today.md
echo "Pipe content in" | gbrain capture --stdin
```

---

### Ingesting PDF, DOCX, CSV, TXT, XLSX, MD

GBrain's core understands Markdown. Binary and structured formats need a
**pre-processing step** that converts them to markdown before `gbrain import` or
`gbrain capture --file`.

#### Option A — GBrain media-ingest skill (PDF, books, YouTube, screenshots)

The built-in `media-ingest` skill handles PDFs and media files via LLM extraction.
Invoke through any connected MCP client (Claude Code, Cursor, etc.):

```
"ingest this PDF: /path/to/report.pdf"
"process this book: /path/to/book.pdf"
"save this article: https://example.com/article"
```

The skill routes to `skills/media-ingest/SKILL.md`, calls the LLM with the document
content, writes a structured markdown page, and auto-links it into the knowledge graph.

#### Option B — doc-ingestor MCP (recommended for DOCX, XLSX, CSV, PPTX, images)

[doc-ingestor](https://github.com/saleemh/doc-ingestor) uses Docling to convert
binary formats to clean Markdown, then you pipe the output into GBrain.

**Install:**

```bash
git clone https://github.com/saleemh/doc-ingestor
cd doc-ingestor
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install docling docling[mlx]     # mlx acceleration on Apple Silicon
# Optional OCR: pip install easyocr && brew install tesseract
```

**Convert and ingest:**

```bash
# Convert to markdown
python -m doc_ingestor_mcp convert /path/to/report.pdf   > output/report.md
python -m doc_ingestor_mcp convert /path/to/data.xlsx    > output/data.md
python -m doc_ingestor_mcp convert /path/to/brief.docx   > output/brief.md
python -m doc_ingestor_mcp convert /path/to/data.csv     > output/data.md

# Then import the markdown folder into GBrain
gbrain import ./output/
```

**Or run doc-ingestor as an MCP server** alongside GBrain — your agent can call
`doc-ingestor` to convert a file, then call `gbrain capture --file` on the result:

```json
{
  "mcpServers": {
    "doc-ingestor": {
      "command": "python",
      "args": ["-m", "doc_ingestor_mcp"],
      "cwd": "/path/to/doc-ingestor"
    },
    "gbrain": {
      "command": "gbrain",
      "args": ["serve"]
    }
  }
}
```

doc-ingestor supported formats:

| Format | Notes |
|--------|-------|
| `.pdf` | Born-digital and scanned (VLM pipeline for complex layouts) |
| `.docx` | Word documents |
| `.xlsx` / `.pptx` | Excel and PowerPoint |
| `.csv` | Converted to markdown table |
| `.txt` / `.md` | Pass-through |
| `.png` / `.jpg` etc. | OCR + VLM |
| `.mp3` / `.wav` | Transcription (ASR pipeline) |

#### Option C — Manual pre-processing scripts

For a self-contained pipeline without a second MCP server:

```python
# requirements: markitdown, pandas, openpyxl, python-docx
from markitdown import MarkItDown
import pandas as pd, pathlib

md = MarkItDown()

def convert(src: str) -> str:
    p = pathlib.Path(src)
    if p.suffix in {".pdf", ".docx", ".pptx", ".xlsx"}:
        return md.convert(src).text_content
    if p.suffix == ".csv":
        return pd.read_csv(src).to_markdown(index=False)
    return p.read_text()

# Write markdown and import
for f in pathlib.Path("./docs").iterdir():
    out = pathlib.Path("./converted") / (f.stem + ".md")
    out.write_text(convert(str(f)))

# Then: gbrain import ./converted/
```

#### Format summary

| File type | Recommended path |
|-----------|-----------------|
| `.md` / `.txt` | `gbrain import <dir>` directly |
| `.pdf` | media-ingest skill OR doc-ingestor → `gbrain import` |
| `.docx` | doc-ingestor → `gbrain import` |
| `.xlsx` | doc-ingestor or pandas `.to_markdown()` → `gbrain import` |
| `.csv` | pandas `.to_markdown()` → `gbrain import` |

---

## Integrating GBrain as a RAG Backend

### Pattern A — Local / dev (stdio MCP)

Best for single-machine, solo development. Zero network overhead.

```bash
gbrain serve    # starts stdio MCP server
```

Wire into your agent framework:

```json
{
  "mcpServers": {
    "gbrain": {
      "command": "gbrain",
      "args": ["serve"]
    }
  }
}
```

### Pattern B — Remote / production (HTTP MCP with OAuth 2.1)

Best for teams, multi-device, or multi-agent setups. Requires Postgres (not PGLite).

```bash
gbrain serve --http    # HTTP MCP + OAuth 2.1 + admin dashboard at /admin
```

Clients authenticate with Bearer tokens scoped to `read`, `write`, or `admin`.

```json
{
  "mcpServers": {
    "gbrain": {
      "command": "mcp-client",
      "args": ["https://your-brain-host/mcp"],
      "env": { "GBRAIN_TOKEN": "your-bearer-token" }
    }
  }
}
```

### Using the BrainEngine TypeScript API directly (programmatic)

For Node/Bun applications that want to embed GBrain without a subprocess:

```typescript
import { BrainEngine } from "@garrytan/gbrain/core/engine";
import { PGLiteEngine } from "@garrytan/gbrain/engines/pglite";

const engine: BrainEngine = new PGLiteEngine({ path: "~/.gbrain/brain.db" });
await engine.init();

// Ingest a page
await engine.put_page({
  title: "Meeting notes 2026-06-16",
  content: markdownString,
  source: "my-app",
  type: "note",
});

// Hybrid retrieval — returns ranked chunks with citations
const results = await engine.search({
  query: "What did we decide about the API design?",
  mode: "balanced",   // "conservative" | "balanced" | "tokenmax"
  limit: 10,
});

// Multi-hop graph query
const graph = await engine.graph_query({
  seed: "Alice",
  relation: "works_at",
  depth: 2,
});
```

Core `BrainEngine` operations (~47, defined in `src/core/engine.ts`):

- `put_page` / `upsertChunks` — write with auto-linking
- `search` — hybrid HNSW + BM25 + RRF with graph boosts
- `graph_query` — typed-edge graph traversal
- `addLinksBatch` / `addTimelineEntriesBatch` — bulk writes
- `extract_facts` — zero-LLM entity extraction

---

## Retrieval Modes

| Mode | Behaviour |
|------|-----------|
| `conservative` | High precision, BM25-weighted |
| `balanced` | Default; RRF fusion of vector + keyword + graph |
| `tokenmax` | Maximises recall; uses ZeroEntropy reranker |

---

## Diagnostics

```bash
gbrain doctor       # auto-detect and repair config issues
gbrain verify       # health check — embedding, DB, sources
gbrain status       # show brain/source summary
```

---

## Key Architecture References

- `src/core/engine.ts` — BrainEngine contract (~47 ops)
- `src/core/operations.ts` — ~90 shared ops powering CLI + MCP
- `src/mcp/server.ts` — MCP server (stdio + HTTP)
- `skills/media-ingest/SKILL.md` — PDF/media ingestion skill
- `docs/ENGINES.md` — PGLite vs Postgres decision guide
- `docs/architecture/topologies.md` — single/remote/split-engine topologies
- `gbrainarch.md` — local architecture summary (this repo)

---

## Sources

- [garrytan/gbrain — GitHub](https://github.com/garrytan/gbrain)
- [GBrain Review — Vectorize.io](https://vectorize.io/articles/gbrain-review)
- [GBrain DeepWiki](https://deepwiki.com/garrytan/gbrain)
- [GBrain Getting Started — DeepWiki](https://deepwiki.com/garrytan/gbrain/1.1-getting-started-and-installation)
- [GBrain GBRAIN_V0.md docs](https://github.com/garrytan/gbrain/blob/master/docs/GBRAIN_V0.md)
- [saleemh/doc-ingestor — GitHub](https://github.com/saleemh/doc-ingestor)
- [GBrain for Claude Code — mdskills.ai](https://www.mdskills.ai/tools/autoplan)
