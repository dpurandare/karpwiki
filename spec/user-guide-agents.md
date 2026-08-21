# Agent Guide (MCP)

How an AI agent (or anything integrating one) uses karpwiki through MCP — the second of `01` §2's
two Common Gateway protocol adapters, alongside REST (`spec/user-guide-admins.md`,
`06-api-mcp-and-scaling.md` §2). Every tool below calls the exact same underlying logic the REST
endpoints do — `wiki_search`/`wiki_resolve_review_item` literally call `api.run_search`/
`api.run_resolve_review_item` directly, and the rest apply the identical role checks REST does —
so nothing here behaves differently just because it's MCP instead of curl.

## 1. Connecting

**`stdio`** — the shape a local agent or IDE integration uses (one process, one caller, no
network):

```bash
python -m karpwiki.mcp_server
```

Identity resolves once, lazily, from environment variables (§2 below) — there are no per-call
headers on this transport at all.

**Streamable HTTP** — a real per-request server, for a remote or multi-caller setup:

```python
from karpwiki.mcp_server import create_mcp_server
create_mcp_server().run(transport="streamable-http")  # 127.0.0.1:8000/mcp by default
```

Identity resolves per request from real headers, the same `Authenticator` (`06` §3) every REST
call already goes through — `TrustedHeaderAuthenticator` or `OidcAuthenticator` depending on how
the deployment is configured (`deployment-guide.md` §5).

## 2. Identity

| Transport | How the caller is identified |
|---|---|
| `stdio` | `KARPWIKI_MCP_TOKEN` (a real bearer token, if `OidcAuthenticator` is configured) takes precedence; otherwise `KARPWIKI_MCP_USER` + optional `KARPWIKI_MCP_GROUPS`, mirroring `TrustedHeaderAuthenticator`'s header shape. Read once per process, at first tool call. |
| Streamable HTTP | Real per-request headers — `Authorization: Bearer <jwt>` once OIDC is configured, or `X-Karpwiki-User`/`X-Karpwiki-Groups` against the trusted-header default. |

A call with no resolvable identity on either transport fails with `McpAuthError`, the same
"no authenticated principal" a REST call without valid auth gets (as a tool-call error — MCP has no
HTTP status codes to carry it).

## 3. Tool reference

All 11 tools `06` §2 names. Every one enforces the same role requirements the equivalent REST
endpoint does — nothing here is more or less permissive than curl-ing the same operation.

**Search and read:**

| Tool | Maps to | Notes |
|---|---|---|
| `wiki_search(q, workspace_id=None, page_type=None, tags=None, date_from=None, date_to=None, include_drafts=False, limit=...)` | `GET /search` | `workspace_id`/`page_type`/`tags` are lists; omit `workspace_id` to search everywhere the caller can access |
| `wiki_get_page(page_id)` | `GET /pages/{id}` | Includes the same resolved, AuthZ-checked `links` field `GET /pages/{id}` returns — a link the caller can't see is simply absent, not flagged |
| `wiki_list_pages(workspace_id, page_type=None, tags=None, date_from=None, date_to=None, status=None, limit=..., cursor=None)` | `GET /pages` | `status="draft"` needs `contributor`, not just `reader` |
| `wiki_list_workspaces(limit=...)` | `GET /workspaces` | Only workspaces the caller can access at all |
| `wiki_get_page_versions(page_id, limit=..., cursor=None)` | `GET /pages/{id}/versions` | Admin-only, same as the REST endpoint |

**Submitting and search feedback:**

| Tool | Maps to | Notes |
|---|---|---|
| `wiki_submit(text, acting_as=None)` | `POST /sources` | Pasted text only — no file/URL upload over MCP (§4 below explains why) |
| `wiki_get_source_status(source_id)` | `GET /sources/{id}` | Submitter-only — even the agent that made a delegated submission can't poll it (§4) |
| `wiki_submit_search_feedback(query_id, page_id, rating)` | `POST /search/{id}/feedback` | `rating` is `"up"` or `"down"`; must be the same principal who ran that search |

**Admin (review queue and rollback — same admin-only bar as REST):**

| Tool | Maps to | Notes |
|---|---|---|
| `wiki_list_review_items(workspace_id=None, kind=None, status="open", severity=None, limit=..., cursor=None)` | `GET /review-items` | |
| `wiki_resolve_review_item(review_id, action, note=None)` | `POST /review-items/{id}/resolve` | Valid `action` values depend on the item's `kind` — see `user-guide-admins.md` §2's full table |
| `wiki_rollback_page(page_id, target_version_id, change_summary=None)` | `POST /pages/{id}/rollback` | |

No MCP tool exists for workspace/schema/access-policy/connector management or the `/metrics/*`/
`/analytics/*` dashboards — those stay REST-only by design (`user-guide-admins.md`'s own intro
explains why: admin governance configuration, not an agent-facing operation).

## 4. On-behalf-of submission (`wiki_submit`'s `acting_as`)

The one delegated operation `06` §2 names — an agent submitting *as* a specific end user, not as
itself:

```python
await session.call_tool("wiki_submit", {"text": "...", "acting_as": "user:casey"})
```

AuthZ requires **both** the calling agent and the represented user (`casey`) to independently hold
`contributor` somewhere in common — whichever is more restrictive applies, so an agent can't use
its own broader access to submit "as" a user who couldn't have submitted there themselves. On
success, `submitted_by` (and the resulting page's `author`) record the represented user, not the
agent — the agent's own identity is recorded separately, in the `ingestion_log` entry's `detail`,
for audit. Omit `acting_as` for an ordinary submission as the calling identity itself.

**A real, known gap, not a silent omission**: the calling agent cannot poll `wiki_get_source_status`
on a submission it made on someone else's behalf — that tool's submitter-only check matches the
literal `submitted_by`, which for a delegated submission is the represented user. The represented
user (who holds a real, independent `contributor` credential by the AuthZ rule above) can always
check it themselves.

## 5. A worked example: search, read, cite

The common shape most agent interactions take — find something, read it, cite it:

```python
import json
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.session import ClientSession

params = StdioServerParameters(
    command="python", args=["-m", "karpwiki.mcp_server"],
    env={"KARPWIKI_MCP_USER": "your-agent-id", **your_env},
)
async with stdio_client(params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()

        result = await session.call_tool("wiki_search", {"q": "retry with backoff"})
        found = json.loads(result.content[0].text)  # {"query_id": "...", "items": [...]}
        top = found["items"][0]

        result = await session.call_tool("wiki_get_page", {"page_id": top["page_id"]})
        page = json.loads(result.content[0].text)  # real, resolved citations and links — ready to quote directly

        await session.call_tool(
            "wiki_submit_search_feedback",
            {"query_id": found["query_id"], "page_id": top["page_id"], "rating": "up"},
        )
```

Rating the result you actually used isn't just courtesy — `wiki_submit_search_feedback` is a real
input to the Maintenance Advisor's staleness detector (`07` §4, `phase3-tasklist.md` step 68): a
page with enough ratings and a high enough down-vote ratio (defaults: at least 3 ratings, at least
60% down) gets flagged for re-review.

---
Previous: [08-implementation-stack.md](08-implementation-stack.md) · Back to: [00-overview.md](00-overview.md)
