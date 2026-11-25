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
# The digests of the portable modules are pinned to this compiler host. The
# amd64 artifact job supplies these modules and both native Wasmtime artifacts.
ARG WASM_BUILD_PLATFORM=linux/amd64

FROM ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv

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
COPY scripts/build-oligo-wasm.sh /src/scripts/build-oligo-wasm.sh
# rustup checks the target against the release's manifest as it installs it.
# A module with every time zone missed .cargo's filter: it fails here, not in browsers.
RUN rustup target add wasm32-unknown-unknown \
 && cargo build --locked -p iki-wasm --target wasm32-unknown-unknown --release \
 && ! grep -q Europe/London target/wasm32-unknown-unknown/release/iki_wasm.wasm \
 && /src/scripts/build-oligo-wasm.sh --with-notice /src/worker/oligo-measurement.wasm
# And the plasmid plugin's own module, which places primers and cuts DNA (ADR 0200). No
# .wasm is tracked in git, so this stage is where a deployment's copy comes from; the
# script checks what it built against the digest git tracks on the one platform where that
# comparison holds, Linux x86-64, which is what this stage and every deployment are.
COPY scripts/build-plasmid-tools-wasm.sh scripts/build-oligo-checks-wasm.sh /src/scripts/
# --build-only: this stage is where the digest's bytes come from, so it compiles and
# nothing more. verified-wasm checks the bytes against the tracked digests, as it does
# the server's, and the deployment CLI checks them again before they reach a worker.
RUN /src/scripts/build-plasmid-tools-wasm.sh --build-only \
 && /src/scripts/build-oligo-checks-wasm.sh --build-only
# Both worker modules use the standard library notice from the pinned Rust image.
COPY --from=dna-annotation /src/worker/dna-annotation.wasm ./

# The amd64 artifact job cross-compiles both targets. Plain local builds run
# the Wasmtime compiler on their build platform too, including arm64 hosts.
FROM --platform=$BUILDPLATFORM ${PYTHON_IMAGE} AS server-aot
ARG APT_RETRY="-o Acquire::Retries=8 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30"
RUN apt-get $APT_RETRY update \
 && apt-get $APT_RETRY install -y --no-install-recommends curl xz-utils \
 && rm -rf /var/lib/apt/lists/*
COPY scripts/build-server-aot.sh /src/scripts/
COPY server/iki_wasm.wasm.sha256 /src/server/
COPY --from=wasm /src/worker/target/wasm32-unknown-unknown/release/iki_wasm.wasm /input/iki_wasm.wasm
RUN /src/scripts/build-server-aot.sh /input/iki_wasm.wasm /artifacts

# --- Shared payload for both native image builds --------------------------
# CI exports this target once, then replaces it with the downloaded named
# context in both platform builds. Plain Docker builds use the stages above.
FROM scratch AS server-wasm-artifacts
COPY --from=wasm /src/worker/target/wasm32-unknown-unknown/release/iki_wasm.wasm /
COPY --from=wasm /src/worker/dna-annotation.wasm /src/worker/oligo-measurement.wasm /src/worker/rust-standard-library.html /
COPY --from=wasm /src/plugins/plasmid/data/plasmid-tools.wasm /src/plugins/dna/data/oligo-checks.wasm /
COPY --from=server-aot /artifacts/ /

FROM ${PYTHON_IMAGE} AS verified-wasm
COPY --from=server-wasm-artifacts /iki_wasm.wasm /dna-annotation.wasm /oligo-measurement.wasm /plasmid-tools.wasm /oligo-checks.wasm /rust-standard-library.html /artifacts/
COPY --from=server-wasm-artifacts /amd64 /artifacts/amd64
COPY --from=server-wasm-artifacts /arm64 /artifacts/arm64
COPY scripts/verify-server-wasm.sh scripts/verify-server-aot.py /src/scripts/
COPY server/iki_wasm.wasm.sha256 server/iki_wasm.amd64.cwasm.sha256 server/iki_wasm.arm64.cwasm.sha256 /src/server/
COPY plugins/dna/data/oligo-measurement.sha256 plugins/dna/data/oligo-checks.wasm.sha256 /src/plugins/dna/data/
COPY plugins/plasmid/data/dna-annotation.wasm.sha256 plugins/plasmid/data/plasmid-tools.wasm.sha256 /src/plugins/plasmid/data/
COPY uv.lock /src/
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
COPY --from=verified-wasm /artifacts/iki_wasm.wasm web/packages/science/wasm/
# Writes server/src/iki_server/web/static/app and, for the structure viewer's frame, viewers.
# The parsers are built above, so this builds the rest (`build` would build them too).
RUN pnpm --filter @ikisci/app build:web \
 && test -f /src/server/src/iki_server/web/static/app/manifest.json \
 && test -f /src/server/src/iki_server/web/static/viewers/manifest.json

# --- Build: resolve the locked dependencies into a virtualenv ----------------
FROM ${PYTHON_IMAGE} AS build
ARG TARGETARCH
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
COPY --from=verified-wasm /artifacts/iki_wasm.wasm /artifacts/dna-annotation.wasm /artifacts/oligo-measurement.wasm /artifacts/rust-standard-library.html spec/src/iki_spec/
# And the modules first-party plugins' actions call (ADR 0200). iki-spec carries them
# because `iki deploy build` exports a plugin with `git archive`, which has each module's
# tracked digest but not its bytes; the CLI takes them from here and checks them again.
COPY --from=verified-wasm /artifacts/plasmid-tools.wasm /artifacts/oligo-checks.wasm spec/src/iki_spec/modules/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package iki-server --extra c --no-editable \
 && /opt/iki/venv/bin/python -c "from iki_spec import wasm; assert wasm.WASM.is_file(), wasm.WASM" \
 && /opt/iki/venv/bin/python -c "from iki_spec import modules; assert all(modules.module(m) for m in ('plasmid-tools.wasm', 'oligo-checks.wasm')), modules.BUILT"
# Keep native code outside the portable Python wheel. The server verifies this
# image-owned pair before it loads the module to evaluate a guard.
COPY --from=verified-wasm --chmod=0444 /artifacts/${TARGETARCH}/iki_wasm.cwasm /artifacts/${TARGETARCH}/iki_wasm.json /opt/iki/wasm/
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
COPY --from=build /opt/iki/wasm /opt/iki/wasm
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
