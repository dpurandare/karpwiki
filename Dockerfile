# Shared image for every process group (phase2-tasklist.md step 31, extended to the
# `gateway` service in step 49) — docker-compose overrides `command:` per service, one
# process per queue (06 §4's worker pools: classification, curation, indexing,
# maintenance) plus the Common Gateway itself (`uvicorn karpwiki.api:app`). Matches the
# Python version the dev venv is tested on (README).
FROM python:3.14-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN useradd --create-home --shell /bin/false karpwiki
USER karpwiki

CMD ["celery", "-A", "karpwiki.tasks", "worker", "--loglevel=info"]
