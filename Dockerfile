FROM ghcr.io/astral-sh/uv:0.10.10@sha256:cbe0a44ba994e327b8fe7ed72beef1aaa7d2c4c795fd406d1dbf328bacb2f1c5 AS uv
FROM python:3.12-slim-bookworm@sha256:6dd8b6dab6b3adc7812bab69eca5dbdc1a5cc690ad28306d234ffb4ef8ac9753 AS base
COPY --from=uv /uv /uvx /bin/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./

FROM base AS development
RUN uv sync --locked --no-install-project
COPY . .
CMD ["uv", "run", "--no-sync", "uvicorn", "scheduler.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]

FROM base AS runtime
RUN uv sync --locked --no-dev --no-install-project
COPY scheduler ./scheduler
COPY brain ./brain
COPY analytics ./analytics
COPY knowledge ./knowledge
COPY voice/catalog.json ./voice/catalog.json
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini LICENSE ./
RUN useradd --create-home --uid 10001 scheduler && mkdir -p /var/lib/knowledge && chown scheduler:scheduler /var/lib/knowledge && chmod 700 /var/lib/knowledge
USER scheduler
CMD ["uv", "run", "--no-sync", "uvicorn", "scheduler.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]


FROM base AS embedding
RUN uv sync --locked --no-dev --group embedding --no-install-project
COPY knowledge ./knowledge
COPY embedding_service ./embedding_service
COPY scripts/prepare_knowledge_assets.py ./scripts/prepare_knowledge_assets.py
RUN useradd --create-home --uid 10002 embedding
USER embedding
CMD ["uv", "run", "--no-sync", "uvicorn", "embedding_service.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]

FROM base AS knowledge-worker
RUN uv sync --locked --no-dev --group ingestion --no-install-project
COPY knowledge ./knowledge
COPY knowledge_worker ./knowledge_worker
COPY scheduler ./scheduler
RUN useradd --create-home --uid 10001 knowledge-worker
USER knowledge-worker
CMD ["uv", "run", "--no-sync", "python", "-m", "knowledge_worker.main"]


FROM runtime AS analytics-worker
COPY analytics_worker ./analytics_worker
CMD ["uv", "run", "--no-sync", "python", "-m", "analytics_worker.main"]
