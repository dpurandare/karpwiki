"""Classifier routing across the full active-workspace set (03 §3) —
phase2-tasklist.md step 24."""

import hashlib
import uuid

from sqlalchemy import select

from karpwiki import document_types, ingestion, objectstore
from karpwiki.classify import ClassificationResult
from karpwiki.models import PipelineState, RawSource, ReviewItem, ReviewKind, WorkspaceStatus


async def _submitted(session, *, filename="doc.md", payload=b"content"):
    source_id = uuid.uuid4()
    key = f"/_inbox/{source_id}/{filename}"
    objectstore.write_bytes(key, payload)
    source = RawSource(
        source_id=source_id,
        object_key=key,
        filename=filename,
        content_hash=hashlib.sha256(payload).hexdigest(),
        submitted_by="user:deepak",
        pipeline_state=PipelineState.submitted,
    )
    session.add(source)
    await session.flush()
    return source


def _returns(label, confidence=0.9, summary="A doc."):
    async def _call(**_kwargs):
        return ClassificationResult(summary=summary, document_type=label, confidence=confidence)

    return _call


async def test_classify_source_takes_no_workspace_and_still_routes(session, workspace, other_workspace):
    """The whole point of this step: no caller pre-selects a workspace."""
    source = await _submitted(session)
    state = await ingestion.classify_source(session, source=source, call=_returns("policy.hr"))
    await session.commit()

    assert state is PipelineState.classified
    assert source.workspace_id == other_workspace.workspace_id  # not `workspace`


async def test_routing_reaches_either_workspace_from_one_taxonomy(session, workspace, other_workspace):
    eng_source = await _submitted(session, filename="eng.md")
    await ingestion.classify_source(session, source=eng_source, call=_returns("eng.runbook"))
    policy_source = await _submitted(session, filename="policy.md")
    await ingestion.classify_source(session, source=policy_source, call=_returns("policy.hr"))
    await session.commit()

    assert eng_source.workspace_id == workspace.workspace_id
    assert policy_source.workspace_id == other_workspace.workspace_id


async def test_a_label_outside_the_full_taxonomy_is_still_refused(session, workspace, other_workspace):
    source = await _submitted(session)
    state = await ingestion.classify_source(
        session, source=source, call=_returns("nonexistent.type")
    )
    assert state is PipelineState.pending_review
    assert source.workspace_id is None


async def test_archived_workspaces_are_excluded_from_routing(session, workspace, other_workspace):
    """01 §3: an archived workspace is "excluded from default search/ingestion routing.\""""
    other_workspace.status = WorkspaceStatus.archived
    await session.flush()

    source = await _submitted(session)
    state = await ingestion.classify_source(session, source=source, call=_returns("policy.hr"))
    await session.commit()

    # policy.hr's own workspace is archived, so the gate can't find it in the active
    # taxonomy at all -- refused the same way an unknown label would be.
    assert state is PipelineState.pending_review
    assert source.workspace_id is None


async def test_resolve_classification_routes_to_the_types_own_workspace(session, workspace, other_workspace):
    """An admin resolving a classification item is routed by the same taxonomy table
    classify_source itself uses -- picking a type from `other_workspace` routes there,
    with no workspace supplied at all."""
    source = await _submitted(session)
    await ingestion.classify_source(session, source=source, call=_returns("nonexistent.type"))
    await session.commit()

    item = (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.subject_ref == str(source.source_id),
                ReviewItem.kind == ReviewKind.classification,
            )
        )
    ).scalar_one()

    state = await ingestion.resolve_classification(
        session, item=item, document_type="policy.hr", actor="user:admin"
    )
    await session.commit()

    assert state is PipelineState.classified
    assert source.workspace_id == other_workspace.workspace_id


async def test_lexical_match_scores_against_the_full_taxonomy(session, workspace, other_workspace):
    """03 §3 step 3: the lexical pre-step runs against the *central* taxonomy, not one
    workspace's slice -- confirmed by a filename that only matches a label owned by
    `other_workspace`, with no workspace passed to classify_source at all."""
    source = await _submitted(session, filename="policy-hr-handbook.md")
    await ingestion.classify_source(session, source=source, call=_returns("policy.hr", confidence=0.99))
    await session.commit()

    assert source.pipeline_state is PipelineState.classified
    assert source.workspace_id == other_workspace.workspace_id


async def test_document_types_list_active_excludes_archived_workspaces(session, workspace, other_workspace):
    other_workspace.status = WorkspaceStatus.archived
    await session.flush()
    types = [dt.type_code for dt in await document_types.list_active(session)]
    assert "policy.hr" not in types
    assert "eng.runbook" in types
