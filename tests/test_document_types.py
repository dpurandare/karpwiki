"""Document-type taxonomy CRUD (02 §3, 05 §7) — phase2-tasklist.md step 22."""

import pytest

from karpwiki import document_types
from karpwiki.models import DocumentType


async def test_create_and_list_for_workspace(session, workspace):
    await document_types.create(session, type_code="eng.postmortem", workspace_id=workspace.workspace_id)
    types = await document_types.type_codes_for_workspace(session, workspace_id=workspace.workspace_id)
    assert set(types) == {"eng.design-doc", "eng.runbook", "eng.postmortem"}


async def test_create_rejects_a_duplicate_type_code(session, workspace, other_workspace):
    with pytest.raises(document_types.DuplicateTypeCodeError):
        await document_types.create(
            session, type_code="eng.runbook", workspace_id=other_workspace.workspace_id
        )


async def test_type_codes_are_scoped_to_their_own_workspace(session, workspace, other_workspace):
    eng_types = await document_types.type_codes_for_workspace(session, workspace_id=workspace.workspace_id)
    policy_types = await document_types.type_codes_for_workspace(
        session, workspace_id=other_workspace.workspace_id
    )
    assert set(eng_types) == {"eng.design-doc", "eng.runbook"}
    assert policy_types == ["policy.hr"]


async def test_list_for_workspaces_spans_a_set(session, workspace, other_workspace):
    all_types = await document_types.list_for_workspaces(
        session, workspace_ids=[workspace.workspace_id, other_workspace.workspace_id]
    )
    assert {t.type_code for t in all_types} == {"eng.design-doc", "eng.runbook", "policy.hr"}

    assert await document_types.list_for_workspaces(session, workspace_ids=[]) == []


# --- Deliberately capped, not cursor-paginated (09 §14, phase3-tasklist.md step 66) ------


async def test_list_for_workspace_respects_limit(session, workspace):
    types = await document_types.list_for_workspace(session, workspace_id=workspace.workspace_id, limit=1)
    assert len(types) == 1


async def test_list_for_workspaces_respects_limit(session, workspace, other_workspace):
    types = await document_types.list_for_workspaces(
        session, workspace_ids=[workspace.workspace_id, other_workspace.workspace_id], limit=1
    )
    assert len(types) == 1


async def test_update_renames_a_type_code(session, workspace):
    doc_type = await session.get(DocumentType, "eng.runbook")
    updated = await document_types.update(session, doc_type=doc_type, new_type_code="eng.oncall-runbook")
    assert updated.type_code == "eng.oncall-runbook"
    assert await session.get(DocumentType, "eng.runbook") is None
    assert await session.get(DocumentType, "eng.oncall-runbook") is not None


async def test_update_rejects_renaming_onto_an_existing_code(session, workspace):
    doc_type = await session.get(DocumentType, "eng.runbook")
    with pytest.raises(document_types.DuplicateTypeCodeError):
        await document_types.update(session, doc_type=doc_type, new_type_code="eng.design-doc")


async def test_update_reassigns_workspace_affects_routing_only(session, workspace, other_workspace):
    """05 §7: reassignment is a routing-forward change — it does not touch any already-
    ingested content, so there's nothing else for this function to do beyond the column."""
    doc_type = await session.get(DocumentType, "eng.runbook")
    updated = await document_types.update(session, doc_type=doc_type, workspace_id=other_workspace.workspace_id)
    assert updated.workspace_id == other_workspace.workspace_id

    eng_types = await document_types.type_codes_for_workspace(session, workspace_id=workspace.workspace_id)
    policy_types = await document_types.type_codes_for_workspace(
        session, workspace_id=other_workspace.workspace_id
    )
    assert "eng.runbook" not in eng_types
    assert "eng.runbook" in policy_types


async def test_update_sets_description(session, workspace):
    doc_type = await session.get(DocumentType, "eng.runbook")
    updated = await document_types.update(session, doc_type=doc_type, description="Ops runbooks.")
    assert updated.description == "Ops runbooks."


async def test_delete_removes_the_type(session, workspace):
    doc_type = await session.get(DocumentType, "eng.runbook")
    await document_types.delete(session, doc_type=doc_type)
    assert await session.get(DocumentType, "eng.runbook") is None
    types = await document_types.type_codes_for_workspace(session, workspace_id=workspace.workspace_id)
    assert types == ["eng.design-doc"]
