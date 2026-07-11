# backend/Dockerfile
# Tool API image for Contoso MSME RM Assist. The synthetic CSV pack and the
# knowledge-base rule files are baked in so the container is fully self-contained
# and loads its data into memory at startup (no external DB for the POC).

FROM mcr.microsoft.com/devcontainers/python:3.12 AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# App code
COPY backend/app /app/app

# Baked synthetic data (deterministic CSV pack + KB rules) — fully self-contained.
COPY data/csv /app/data/csv
COPY data/knowledge_base /app/data/knowledge_base
# Indian-banking SOP corpus surfaced by the Raw Data explorer (read-only proof pack).
COPY docs/sop /app/data/sop

RUN useradd --create-home --shell /bin/bash --uid 10001 apprunner && chown -R apprunner /app
USER apprunner

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
