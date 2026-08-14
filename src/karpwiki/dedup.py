"""Duplicate detection at `duplicate_check` (03 §4).

Three checks in the order 03 §4 draws them, each cheaper and more certain than the next:
a same-artifact newer version, an exact content-hash match, then a lexical near-match
against the workspace's own pages. Every hit blocks — `pending_review` — regardless of the
workspace's ingestion policy; only the "no concerns" path is subject to `auto`/`gated`.

No embeddings and no LLM here. Lexical similarity surfaces *candidates*; 03 §4 leaves the
final near-duplicate judgement to the Curator when an admin resolves the review item.
"""

import uuid
from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import search
from .models import RawSource, RawSourceStatus

# 09 §6's SCHEMA.md default, calibrated against measured containment scores (09 §17):
# a light paraphrase of the same document scores ~0.67 and a merely same-topic document
# ~0.43, so the threshold sits in the gap between them.
DEFAULT_NEAR_DUPLICATE_SCORE = 0.60


class Verdict(Enum):
    none = "none"
    newer_version = "newer_version"
    exact = "exact"
    near = "near"


# 03 §4 assigns each kind a severity and a pre-filled proposal.
_PROPOSALS: dict[Verdict, tuple[str, str]] = {
    Verdict.newer_version: ("low", "supersede"),
    Verdict.exact: ("high", "reject"),
    Verdict.near: ("medium", "merge_or_supersede_or_keep_both"),
}


@dataclass(frozen=True)
class DuplicateFinding:
    verdict: Verdict
    severity: str | None = None
    proposed_action: str | None = None
    source_ids: tuple[uuid.UUID, ...] = ()
    page_hits: tuple[search.Hit, ...] = field(default=())

    @property
    def blocks(self) -> bool:
        return self.verdict is not Verdict.none


async def check(
    session: AsyncSession,
    *,
    source: RawSource,
    summary: str,
    near_duplicate_score: float | None = None,
) -> DuplicateFinding:
    """Run 03 §4's checks against the source's own workspace only."""
    if source.workspace_id is None:
        raise ValueError("duplicate_check runs only after a workspace is resolved (03 §4)")

    threshold = (
        DEFAULT_NEAR_DUPLICATE_SCORE if near_duplicate_score is None else near_duplicate_score
    )

    newer = await _same_artifact_older_version(session, source)
    if newer:
        return _finding(Verdict.newer_version, source_ids=newer)

    exact = await _exact_match(session, source)
    if exact:
        return _finding(Verdict.exact, source_ids=exact)

    # 03 §4 runs the *summary* as the near-match query, not the raw document: it is what
    # the Classifier already produced (03 §3), and comparing a summary against curated page
    # text is closer in shape than comparing a full source against it.
    hits = await search.find_similar(
        session, text_body=summary, workspace_id=source.workspace_id
    )
    above = tuple(h for h in hits if h.score >= threshold)
    if above:
        return _finding(Verdict.near, page_hits=above)

    return DuplicateFinding(Verdict.none)


def _finding(verdict: Verdict, **kwargs) -> DuplicateFinding:
    severity, proposal = _PROPOSALS[verdict]
    return DuplicateFinding(verdict, severity=severity, proposed_action=proposal, **kwargs)


async def _same_artifact_older_version(
    session: AsyncSession, source: RawSource
) -> tuple[uuid.UUID, ...]:
    """A new version of an artifact already ingested (03 §4, `structured_data` only).

    The expected case when a schema or config is re-ingested, which is why 03 §4 gives it
    the lowest severity and a pre-filled `supersede` — resolution should be one click.
    """
    if not source.artifact_identity:
        return ()

    result = await session.execute(
        select(RawSource).where(
            RawSource.workspace_id == source.workspace_id,
            RawSource.artifact_identity == source.artifact_identity,
            RawSource.source_id != source.source_id,
            RawSource.status == RawSourceStatus.active,
        )
    )
    existing = list(result.scalars())
    if not existing:
        return ()

    # Only *older* existing versions make this a supersede. A source arriving with an
    # older version than what is stored is not a newer version of it, and falls through to
    # the remaining checks rather than being mislabelled.
    older = [
        e
        for e in existing
        if _is_older(e.source_version, source.source_version)
        or _is_older_at(e.source_modified_at, source.source_modified_at)
    ]
    return tuple(e.source_id for e in older)


def _is_older(existing: str | None, incoming: str | None) -> bool:
    if existing is None or incoming is None:
        return False
    return _version_key(existing) < _version_key(incoming)


def _version_key(value: str) -> tuple:
    """Compare dotted versions numerically where possible, lexically otherwise, so 2.10
    sorts above 2.9 rather than below it."""
    parts = []
    for chunk in value.replace("-", ".").split("."):
        parts.append((0, int(chunk), "") if chunk.isdigit() else (1, 0, chunk))
    return tuple(parts)


def _is_older_at(existing, incoming) -> bool:
    return existing is not None and incoming is not None and existing < incoming


async def _exact_match(session: AsyncSession, source: RawSource) -> tuple[uuid.UUID, ...]:
    """Byte-identical content already in this workspace (03 §4). Always blocks."""
    result = await session.execute(
        select(RawSource.source_id).where(
            RawSource.workspace_id == source.workspace_id,
            RawSource.content_hash == source.content_hash,
            RawSource.source_id != source.source_id,
            RawSource.status != RawSourceStatus.rejected,
        )
    )
    return tuple(result.scalars())
