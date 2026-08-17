# Worker image (phase2-tasklist.md step 31) — runs `celery -A karpwiki.tasks worker`, one
# process per queue via docker-compose's `command:` override per service (06 §4's worker
# pools: classification, curation, indexing, maintenance). Matches the Python version the
# dev venv is tested on (README).
FROM python:3.14-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN useradd --create-home --shell /bin/false karpwiki
USER karpwiki

CMD ["celery", "-A", "karpwiki.tasks", "worker", "--loglevel=info"]
