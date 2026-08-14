"""Async layer — Celery app with one queue per job type (01 §1, 06 §4, 08 §2).

The pools are separated here because they scale on different signals: classification
and curation are LLM-bound, indexing and the maintenance advisor are compute-bound.
Phase 1 defines the queues; the tasks that fill them arrive in 1b and 1c.
"""

from celery import Celery

from .config import CELERY_BROKER_URL

QUEUES = ("classification", "curation", "indexing", "maintenance")

app = Celery("karpwiki", broker=CELERY_BROKER_URL)
app.conf.task_default_queue = "curation"
app.conf.task_routes = {
    "karpwiki.classification.*": {"queue": "classification"},
    "karpwiki.curation.*": {"queue": "curation"},
    "karpwiki.indexing.*": {"queue": "indexing"},
    "karpwiki.maintenance.*": {"queue": "maintenance"},
}


@app.task(name="karpwiki.curation.ping")
def ping() -> str:
    """Smoke-test task — proves the broker round-trips before 1b adds real work."""
    return "pong"
