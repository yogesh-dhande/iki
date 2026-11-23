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
ARG NODE_IMAGE=node:24-trixie-slim@sha256:0820eaa85e07d0490446dd01d14707e17e6108a36988cf374899cd057eb3812d
# The worker's Rust image (worker/Dockerfile), for the web app's parsers.
ARG RUST_IMAGE=rust:1.93.0-slim-trixie@sha256:760ad1d638d70ebbd0c61e06210e1289cbe45ff6425e3ea6e01241de3e14d08e
# Both Wasm digests are pinned to this compiler host. Published images import
# the artifacts built on an AMD64 runner, so neither compiler stage runs in them.
ARG WASM_BUILD_PLATFORM=linux/amd64

FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv

# --- Build the digest-pinned worker artifact on its canonical compiler host ------
FROM --platform=$WASM_BUILD_PLATFORM ${RUST_IMAGE} AS dna-annotation
WORKDIR /src/worker
COPY worker/Cargo.toml worker/Cargo.lock worker/rust-toolchain.toml ./
COPY worker/crates crates
COPY scripts/build-dna-annotation-wasm.sh /src/scripts/build-dna-annotation-wasm.sh
RUN /src/scripts/build-dna-annotation-wasm.sh --build-here /src/worker

# --- The web app's parsers: iki-wasm, Rust built for browsers (ADR 0088) ----
FROM --platform=$WASM_BUILD_PLATFORM ${RUST_IMAGE} AS wasm
WORKDIR /src/worker
COPY worker/Cargo.toml worker/Cargo.lock worker/rust-toolchain.toml ./
COPY worker/crates crates
# Its build settings: rules' CEL keeps only UTC's time zone (ADR 0127).
COPY worker/.cargo .cargo
# rustup checks the target against the release's manifest as it installs it.
# A module with every time zone missed .cargo's filter: it fails here, not in browsers.
RUN rustup target add wasm32-unknown-unknown \
 && cargo build --locked -p iki-wasm --target wasm32-unknown-unknown --release \
 && ! grep -q Europe/London target/wasm32-unknown-unknown/release/iki_wasm.wasm
COPY --from=dna-annotation /src/worker/dna-annotation.wasm /src/worker/rust-standard-library.html ./

# --- Portable payload for both native image builds ------------------------
# CI exports this target once, then replaces it with the downloaded named
# context in both platform builds. Plain Docker builds use the stages above.
FROM scratch AS server-wasm-artifacts
COPY --from=wasm /src/worker/target/wasm32-unknown-unknown/release/iki_wasm.wasm /
COPY --from=wasm /src/worker/dna-annotation.wasm /src/worker/rust-standard-library.html /

FROM ${PYTHON_IMAGE} AS verified-wasm
COPY --from=server-wasm-artifacts /iki_wasm.wasm /dna-annotation.wasm /rust-standard-library.html /artifacts/
COPY scripts/verify-server-wasm.sh /src/scripts/
COPY server/iki_wasm.wasm.sha256 /src/server/
COPY plugins/plasmid/data/dna-annotation.wasm.sha256 /src/plugins/plasmid/data/
RUN /src/scripts/verify-server-wasm.sh /artifacts

# --- Web app: the React app the server serves (web/app) ----------------------
FROM ${NODE_IMAGE} AS web
# Corepack fetches the pnpm that package.json names and checks its hash.
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    PNPM_HOME=/pnpm \
    CI=true
RUN corepack enable
WORKDIR /src
# Dependencies first, so source changes don't invalidate this layer.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./
COPY web/app/package.json web/app/
COPY web/packages/sdk/package.json web/packages/sdk/
COPY web/packages/component/package.json web/packages/component/
COPY web/packages/science/package.json web/packages/science/
COPY web/packages/ui/package.json web/packages/ui/
RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    pnpm install --frozen-lockfile --filter "@ikisci/app..."
COPY web web
# The header's navigation, which the server's pages render too (iki_server.web.navigation).
COPY server/src/iki_server/web/navigation.json server/src/iki_server/web/
COPY --from=verified-wasm /artifacts/iki_wasm.wasm web/app/wasm/
# Writes server/src/iki_server/web/static/app and, for the structure viewer's frame, viewers.
# The parsers are built above, so this builds the rest (`build` would build them too).
RUN pnpm --filter @ikisci/app build:web \
 && test -f /src/server/src/iki_server/web/static/app/manifest.json \
 && test -f /src/server/src/iki_server/web/static/viewers/manifest.json

# --- Build: resolve the locked dependencies into a virtualenv ----------------
FROM ${PYTHON_IMAGE} AS build
ARG APT_RETRY="-o Acquire::Retries=8 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30"
RUN apt-get $APT_RETRY update \
 && apt-get $APT_RETRY install -y --no-install-recommends gcc libc6-dev libpq-dev \
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
COPY spec/pyproject.toml spec/README.md spec/LICENSE spec/NOTICE spec/
# iki-spec's build backend, which builds its wheel only with iki-wasm in it.
COPY spec/backend spec/backend
COPY codegen/pyproject.toml codegen/README.md codegen/LICENSE codegen/NOTICE codegen/
COPY sdk/python/pyproject.toml sdk/python/README.md sdk/python/LICENSE sdk/python/NOTICE sdk/python/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package iki-server --extra c --no-install-workspace
COPY server/src server/src
# The server reads plugin manifests and units with iki-spec.
COPY spec/src spec/src
COPY --from=web /src/server/src/iki_server/web/static/app server/src/iki_server/web/static/app
COPY --from=web /src/server/src/iki_server/web/static/viewers server/src/iki_server/web/static/viewers
# The server checks apps' expressions, and plugins' rules, with the module browsers run
# them with (ADR 0090, ADR 0127), through iki-spec.
COPY --from=verified-wasm /artifacts/iki_wasm.wasm /artifacts/dna-annotation.wasm /artifacts/rust-standard-library.html spec/src/iki_spec/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package iki-server --extra c --no-editable \
 && /opt/iki/venv/bin/python -c "from iki_spec import wasm; assert wasm.WASM.is_file(), wasm.WASM"
# Deployments add their plugins at /opt/iki/plugins (deploy/plugins.Dockerfile),
# which this puts on the server's import path.
RUN /opt/iki/venv/bin/python -c "import pathlib, sysconfig; pathlib.Path(sysconfig.get_path('purelib'), 'iki-plugins.pth').write_text('/opt/iki/plugins\n')"
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
ARG APT_RETRY="-o Acquire::Retries=8 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30"
RUN apt-get $APT_RETRY update \
 && apt-get $APT_RETRY upgrade -y --no-install-recommends \
 && apt-get $APT_RETRY install -y --no-install-recommends libpq5 \
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
      org.opencontainers.image.source="https://github.com/yogesh-dhande/iki" \
      org.opencontainers.image.licenses="Apache-2.0"
# Numeric UID so Kubernetes can verify runAsNonRoot.
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["iki-server"]
CMD ["serve"]
