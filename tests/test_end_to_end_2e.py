"""Phase 2 step 56 — 2e verify: closes out the Connector Framework track.

Configures a real connector (`connectors.create`), runs exactly one poll cycle through the
real `"git"` adapter (`connectors_git.py`, step 54) against a real local git repository —
hermetic, no network, same `origin` fixture convention as `test_connectors_git.py` — and
confirms the resulting `raw_source` is genuinely indistinguishable from a manual upload:
`submitted_by=connector:<id>`, a normal `submission` review item, and (draining the real
dispatch chain, mocked LLM/no broker, same convention as `test_end_to_end_2b.py`/`2d.py`)
the exact same classification → dedup → curation → indexing path a user's own upload takes,
ending up searchable and resolvable through the real REST admin surface unchanged.
"""

import subprocess
import uuid

from sqlalchemy import select

from karpwiki import connectors, tasks
from karpwiki.classify import ClassificationResult
from karpwiki.curate import CuratedContent, CuratedPage
from karpwiki.models import AccessPolicy, PipelineState, RawSource, Role, WikiPage

# No explicit `connectors_git` import needed — the `client` fixture already imports
# `karpwiki.api` -> `karpwiki.tasks` -> `karpwiki.connectors_git`, which registers "git"
# into `connector_polling.ADAPTERS` as an import-time side effect (step 54).

ADMIN = {"X-Karpwiki-User": "avery"}


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _origin(tmp_path):
    repo = tmp_path / "origin"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "runbook.md").write_text("Runbook: rotate the on-call pager schedule weekly.")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    return repo


def _classifies_as(label, confidence=0.9, summary="A doc about on-call rotation."):
    async def _call(**_kwargs):
        return ClassificationResult(summary=summary, document_type=label, confidence=confidence)

    return _call


def _curates_as(*, title, body):
    content = CuratedContent(
        source_title=title,
        source_description=f"About {title}.",
        source_summary=body,
        source_key_points=[body],
        pages=[CuratedPage(page_type="concept", title=title, tags=["ops", "oncall"], body=body)],
    )

    async def _call(**_kwargs):
        return content

    return _call


async def test_2e_connector_poll_is_indistinguishable_from_a_manual_submission(
    client, session, workspace, tmp_path, dispatched, task_db
):
    session.add(AccessPolicy(workspace_id=workspace.workspace_id, principal="avery", role=Role.admin))
    origin = _origin(tmp_path)
    connector = await connectors.create(
        session, workspace_id=workspace.workspace_id, type="git", config={"repo_url": f"file://{origin}"}
    )
    await session.commit()

    # One poll cycle, exactly as a real worker runs it — `tasks._poll_connector` wraps
    # `connector_polling.poll_connector` and dispatches classify_source after its own
    # commit (step 32's "dispatch only after commit" discipline), same as any submission.
    await tasks._poll_connector(connector.connector_id)

    assert len(dispatched["classify_source"]) == 1
    source_id = dispatched["classify_source"][0]

    source = await session.get(RawSource, uuid.UUID(source_id))
    assert source.submitted_by == f"connector:{connector.connector_id}"
    assert source.pipeline_state is PipelineState.submitted

    # The always-on submission review item exists for a connector source exactly as it
    # would for a manual upload (03 §5) — visible through the real REST admin surface.
    r = await client.get("/review-items", headers=ADMIN, params={"kind": "submission"})
    assert r.status_code == 200
    [item] = [i for i in r.json()["items"] if i["subject_ref"] == source_id]

    # From here on, the connector-created source takes the exact same dispatch path any
    # submission does (step 32) — draining it is a stand-in for a real worker, not a
    # reimplementation of the pipeline.
    while dispatched["classify_source"]:
        sid = dispatched["classify_source"].pop(0)
        await tasks._classify(uuid.UUID(sid), call=_classifies_as("eng.runbook"))
    await session.commit()

    assert dispatched["curate_source"] == [source_id]
    while dispatched["curate_source"]:
        sid = dispatched["curate_source"].pop(0)
        await tasks._curate(
            uuid.UUID(sid), call=_curates_as(title="On-Call Rotation", body="Weekly pager handoff.")
        )
    await session.commit()

    await session.refresh(source)
    assert source.pipeline_state is PipelineState.ingested

    page = (
        await session.execute(select(WikiPage).where(WikiPage.path == "concepts/on-call-rotation.md"))
    ).scalar_one()
    assert str(page.page_id) in dispatched["reindex"]
    while dispatched["reindex"]:
        pid = dispatched["reindex"].pop(0)
        await tasks._reindex(uuid.UUID(pid))
    await session.commit()

    # Searchable through the normal REST surface — real FTS, real curated content.
    r = await client.get(
        "/search", headers=ADMIN, params={"q": "pager handoff", "workspace_id": workspace.workspace_id}
    )
    assert r.status_code == 200
    assert any(i["path"] == "concepts/on-call-rotation.md" for i in r.json()["items"])

    # Resolved through the normal REST admin surface — same endpoint, same action, no
    # connector-specific branch anywhere in that path.
    r = await client.post(f"/review-items/{item['review_id']}/resolve", headers=ADMIN, json={"action": "acknowledge"})
    assert r.status_code == 200
    assert r.json()["status"] == "resolved"
