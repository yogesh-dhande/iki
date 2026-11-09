# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
#
# Iki server image. Build from the repository root:
#   docker build -f server/Dockerfile -t iki-server .
#
# Every base image is pinned by digest; Dependabot proposes updates. The
# runtime stage also applies Debian security updates at build time, because
# official base images can lag days behind Debian security fixes. The SBOM and
# provenance published with each image record exactly what it contains.

ARG PYTHON_IMAGE=python:3.14-slim-trixie@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

# --- Build: resolve the locked dependencies into a virtualenv ----------------
FROM ${PYTHON_IMAGE} AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev libpq-dev \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/opt/iki/venv
WORKDIR /src
# Dependencies first, so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock LICENSE NOTICE ./
COPY server/pyproject.toml server/README.md server/LICENSE server/NOTICE server/
COPY cli/pyproject.toml cli/README.md cli/LICENSE cli/NOTICE cli/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package iki-server --extra c --no-install-workspace
COPY server/src server/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package iki-server --extra c --no-editable
# Static files, content-hashed and compressed. Settings need their required
# values to load; collectstatic touches neither the database nor the keys,
# so throwaway values are generated for this one command.
ENV IKI_STATIC_ROOT=/opt/iki/static
RUN key() { /opt/iki/venv/bin/python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; } \
 && IKI_SECRET_KEY="$(key)" IKI_ENCRYPTION_KEYS="$(key)" IKI_ALLOWED_HOSTS=localhost \
    IKI_ENVIRONMENT=build IKI_DATABASE_URL=postgres://build@localhost/build \
    /opt/iki/venv/bin/iki-server collectstatic --noinput --verbosity 0

# --- Runtime ------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 iki \
 && useradd --system --uid 10001 --gid iki --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin iki
COPY --from=build /opt/iki/venv /opt/iki/venv
COPY --from=build /opt/iki/static /opt/iki/static
ENV PATH=/opt/iki/venv/bin:$PATH \
    IKI_STATIC_ROOT=/opt/iki/static \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PSYCOPG_IMPL=c
LABEL org.opencontainers.image.title="iki-server" \
      org.opencontainers.image.source="https://github.com/ikisci/iki" \
      org.opencontainers.image.licenses="Apache-2.0"
# Numeric UID so Kubernetes can verify runAsNonRoot.
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["iki-server"]
CMD ["serve"]
