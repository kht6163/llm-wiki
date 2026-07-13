# syntax=docker/dockerfile:1.7

# Keep dependency resolution and compiler/build tooling out of the runtime image.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev


FROM python:3.14-slim-bookworm AS runtime

ARG APP_UID=1000
ARG APP_GID=1000
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" llmwiki \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home llmwiki

WORKDIR /app
COPY --from=builder --chown=llmwiki:llmwiki /app/.venv /app/.venv
COPY --from=builder --chown=llmwiki:llmwiki /app/src /app/src

RUN mkdir -p /data /vault /models \
    && chown -R llmwiki:llmwiki /data /vault /models

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models \
    XDG_CACHE_HOME=/models/.cache \
    TORCH_HOME=/models/torch \
    HOST=0.0.0.0 \
    DB_PATH=/data/llm_wiki.db \
    VAULT_PATH=/vault

VOLUME ["/data", "/vault", "/models"]
EXPOSE 8080 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz',timeout=3).status==200 else 1)"

USER llmwiki
ENTRYPOINT ["llm-wiki"]
CMD ["serve"]
