# Knowledge Vault HTTP services (kb-mcp / kb-rest / kb-web) — one image, command selects the app.
# The worker/consolidator stay on the host (systemd) — they need ~/kb and the GPU.
FROM python:3.12-slim

# uv for fast, reproducible installs (matches the host toolchain).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# age provides `age-keygen`, which the owner-only web backup-setup surface runs
# in-container to generate the recovery keypair (kbcore.offsite_config). The
# encryption itself runs HOST-side in scripts/offsite.sh. Placed before the deps
# copy so it stays layer-cached across source changes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends age \
    && rm -rf /var/lib/apt/lists/*

# Install deps first (layer-cached), then the source. --no-dev: no pytest/ruff in the image.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY PLAN.md ./
RUN uv sync --frozen --no-dev

# Pre-fetch the tiktoken vocab at build time (has network); runtime containers
# have no external DNS, and kbcore.chunk loads cl100k_base at import.
ENV TIKTOKEN_CACHE_DIR=/app/.tiktoken_cache
RUN mkdir -p "$TIKTOKEN_CACHE_DIR" \
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"

# Container-private bind is deliberate and safe (only Traefik on kbnet + published
# loopback ports reach it); gate it behind the explicit override so the default
# host posture still refuses 0.0.0.0.
ENV KB_BIND=0.0.0.0 \
    KB_ALLOW_ANY_BIND=true

# Overridden per-service in docker-compose (kbmcp.server / kbrest.server / kbweb.server).
CMD ["python", "-m", "kbmcp.server"]
