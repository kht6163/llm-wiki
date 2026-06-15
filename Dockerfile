# syntax=docker/dockerfile:1
#
# Single base for build + run so the venv's interpreter path stays valid.
# torch resolves to the CPU-only wheel via the pytorch-cpu index pinned in
# pyproject.toml, so the image stays slim (no CUDA).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    HF_HOME=/models \
    HOST=0.0.0.0 \
    DB_PATH=/data/llm_wiki.db \
    VAULT_PATH=/vault

# Install dependencies first (cached) using only the lockfile, then the project.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# DB, vault, and the (large) embedding-model cache persist on volumes.
VOLUME ["/data", "/vault", "/models"]
EXPOSE 8080 8081

# `serve` auto-creates the schema; create the first admin once with:
#   docker compose run --rm llm-wiki create-admin --username admin
ENTRYPOINT ["llm-wiki"]
CMD ["serve"]
