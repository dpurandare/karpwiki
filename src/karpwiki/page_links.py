"""Markdown cross-reference parsing into `page_link` rows (02 §3, 01 §6) —
phase2-tasklist.md step 28.

01 §6: "Cross-references use standard markdown links; links that target another workspace
are written as fully-qualified workspace-relative paths so the gateway can resolve and
AuthZ-check them." A same-workspace target is matched against `wiki_page.path` directly
(e.g. `concepts/foo.md`); a cross-workspace target is `/{workspace_id}/{path}` — the same
`/{workspace_id}/...` convention `objectstore.py` already uses for source/diff paths, the
only concrete precedent "fully-qualified workspace-relative" has in this codebase.

02 §3: "`page_link` rows are (re)written by the Wiki Service whenever a page's
cross-references are parsed during a write" — `sync` runs synchronously inside
`versioning.create_page`/`write_version`, the same way `_mark_stale` does, not as a
separate explicit-call lifecycle like reindexing (09 §18) — parsing is cheap (a regex plus
a handful of lookups), unlike reindexing's LLM-adjacent cost.

Read-time link resolution (01 §3's table: "Gateway re-checks the caller's AuthZ against
the *target* workspace before resolving a link") has no caller yet — `pages/{id}` get isn't
built (06 §1); this module only maintains the `page_link` rows themselves.
"""

import re
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import LinkType, PageLink, WikiPage

# Inline markdown links only, `[text](target)` — not reference-style `[text][ref]`, and not
# image embeds: the negative lookbehind excludes a leading `!` so `![alt](src)` is skipped.
_LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


def extract_link_targets(body: str) -> list[str]:
    return _LINK_PATTERN.findall(body)


async def _resolve(
    session: AsyncSession, *, from_workspace_id: str, target: str
) -> tuple[uuid.UUID, LinkType] | None:
    if target.startswith("/"):
        parts = target[1:].split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None  # malformed fully-qualified path, e.g. "/foo" with no page path
        workspace_id, path = parts
        link_type = LinkType.cross_workspace
    else:
        workspace_id, path = from_workspace_id, target
        link_type = LinkType.cross_reference

    result = await session.execute(
        select(WikiPage.page_id).where(
            WikiPage.workspace_id == workspace_id, WikiPage.path == path
        )
    )
    page_id = result.scalar_one_or_none()
    if page_id is None:
        return None
    return page_id, link_type


async def sync(session: AsyncSession, *, page: WikiPage, body: str) -> None:
    """Re-parses `body` and replaces `page`'s outbound `page_link` rows to match — mirrors
    `search.index_page`'s delete-then-insert pattern for the same reason: the current
    version's links fully replace whatever the previous version pointed to."""
    resolved: dict[uuid.UUID, LinkType] = {}
    for target in extract_link_targets(body):
        # External URLs (http(s)://...) and citation footnotes never resolve — both are
        # legitimate content, not links this table tracks — so a plain lookup miss is the
        # correct outcome, not an error.
        hit = await _resolve(session, from_workspace_id=page.workspace_id, target=target)
        if hit is not None and hit[0] != page.page_id:  # no self-links
            resolved[hit[0]] = hit[1]

    await session.execute(delete(PageLink).where(PageLink.from_page_id == page.page_id))
    session.add_all(
        PageLink(from_page_id=page.page_id, to_page_id=to_page_id, link_type=link_type)
        for to_page_id, link_type in resolved.items()
    )
    await session.flush()
