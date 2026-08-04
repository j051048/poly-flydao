# syntax=docker/dockerfile:1.7

FROM python:3.12.13-slim-trixie AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install "uv==0.11.21"

WORKDIR /app

# Resolve third-party dependencies before copying source so Docker can reuse the
# expensive layer when application code changes. --frozen makes uv.lock the
# deployment source of truth.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM python:3.12.13-slim-trixie AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:$PATH \
    PORT=8080 \
    SERVICE_ROLE=api \
    POLYBOT_COMPONENT=api \
    WEB_CONCURRENCY=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 polybot \
    && useradd --system --uid 10001 --gid polybot --home-dir /nonexistent --shell /usr/sbin/nologin polybot

WORKDIR /app
COPY --from=builder --chown=polybot:polybot /app /app

USER 10001:10001
EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]

# The safe default remains the signer-free multi-tenant API. `personal` runs one
# API process whose lifespan owns exactly one single-account worker; this keeps
# one Zeabur service simple without permitting multiple signer replicas.
CMD ["sh", "-c", "umask 077; ulimit -c 0 2>/dev/null || true; case \"${SERVICE_ROLE:-api}\" in api) export POLYBOT_COMPONENT=api; python -m polybot.config_check || exit $?; exec uvicorn polybot.api:app --host 0.0.0.0 --port \"${PORT:-8080}\" --workers \"${WEB_CONCURRENCY:-1}\" --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\" ;; personal) export POLYBOT_COMPONENT=all; export POLYBOT_WORKER_EXECUTION_MODEL=single_account; export POLYBOT_PERSONAL_MODE=true; python -m polybot.config_check || exit $?; exec uvicorn polybot.api:app --host 0.0.0.0 --port \"${PORT:-8080}\" --workers 1 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\" ;; worker) export POLYBOT_COMPONENT=worker; export POLYBOT_WORKER_EXECUTION_MODEL=tenant_queue; python -m polybot.config_check || exit $?; exec python -m polybot.worker ;; *) echo \"Invalid SERVICE_ROLE=${SERVICE_ROLE}; expected api, personal, or worker\" >&2; exit 64 ;; esac"]
