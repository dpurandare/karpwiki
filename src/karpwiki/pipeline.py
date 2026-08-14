"""Ingestion pipeline state machine (03 §1, 09 §3).

`raw_source.pipeline_state` is a denormalized pointer at the current state; `ingestion_log`
is the append-only system of record for how the source reached it. The two are written in
the same transaction so the pointer can never disagree with the history.

Transitions are exactly those in 03 §1's state diagram — nothing is widened here. See
`ERROR_REACHABILITY` below for a gap that needs a spec decision rather than an invention.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import IngestionLog, PipelineState, RawSource

# 03 §1, read directly off the state diagram.
TRANSITIONS: dict[PipelineState, frozenset[PipelineState]] = {
    PipelineState.submitted: frozenset({PipelineState.classifying}),
    PipelineState.classifying: frozenset({PipelineState.classified, PipelineState.pending_review}),
    PipelineState.classified: frozenset({PipelineState.duplicate_check}),
    PipelineState.duplicate_check: frozenset(
        {PipelineState.ingesting, PipelineState.pending_review}
    ),
    PipelineState.pending_review: frozenset({PipelineState.ingesting, PipelineState.rejected}),
    PipelineState.ingesting: frozenset({PipelineState.ingested, PipelineState.error}),
    PipelineState.error: frozenset({PipelineState.pending_review}),
    PipelineState.ingested: frozenset(),
    PipelineState.rejected: frozenset(),
}

TERMINAL = frozenset({PipelineState.ingested, PipelineState.rejected})

# 03 §1 admits `error` only from `ingesting`, matching 03 §6's "on failure at any step",
# whose steps are the ingest operation's. That leaves a Classifier failure — an external
# API call, so failures are certain — with no representable state. Widening the machine
# here would put the code ahead of the spec, so it is not done; see the note raised
# alongside this module.
ERROR_REACHABILITY = frozenset({PipelineState.ingesting})


class IllegalTransition(ValueError):
    """The requested transition is not an edge in 03 §1's state machine."""


async def transition(
    session: AsyncSession,
    *,
    source: RawSource,
    to_state: PipelineState,
    actor: str,
    detail: dict | None = None,
) -> IngestionLog:
    """Move a source to `to_state`, appending the history entry in the same transaction."""
    from_state = source.pipeline_state
    if to_state not in TRANSITIONS[from_state]:
        allowed = ", ".join(sorted(s.value for s in TRANSITIONS[from_state])) or "none (terminal)"
        raise IllegalTransition(
            f"{from_state.value} -> {to_state.value} is not a legal transition; "
            f"allowed from {from_state.value}: {allowed}"
        )

    source.pipeline_state = to_state
    entry = IngestionLog(
        source_id=source.source_id,
        workspace_id=source.workspace_id,
        from_state=from_state,
        to_state=to_state,
        actor=actor,
        detail=detail or {},
    )
    session.add(entry)
    await session.flush()
    return entry


async def history(session: AsyncSession, source_id: uuid.UUID) -> list[IngestionLog]:
    """Every transition of one source, oldest first."""
    result = await session.execute(
        select(IngestionLog)
        .where(IngestionLog.source_id == source_id)
        .order_by(IngestionLog.created_at, IngestionLog.entry_id)
    )
    return list(result.scalars())
