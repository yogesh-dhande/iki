# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
#
# The iki-worker image: the worker and what runs commands, without any plugin.
# Build from the repository root:
#   docker build -f worker/Dockerfile -t iki-worker .
#
# A deployment builds its own image on top, with its plugins' code and their
# environments (deploy/worker.Dockerfile). Every base image is pinned by
# digest; the runtime stage applies Debian security updates at build time.

ARG RUST_IMAGE=rust:1.93.0-slim-trixie@sha256:760ad1d638d70ebbd0c61e06210e1289cbe45ff6425e3ea6e01241de3e14d08e
ARG DEBIAN_IMAGE=debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

# micromamba runs commands in conda environments, so their activation scripts run.
FROM mambaorg/micromamba:2.3.3@sha256:800e7ade3ffe29c9a9ac2026163131495f8197c3852e572c5835beb4e8a33cd6 AS micromamba

# --- Build ----------------------------------------------------------------------
FROM ${RUST_IMAGE} AS build
WORKDIR /src
COPY worker/Cargo.toml worker/Cargo.lock worker/rust-toolchain.toml ./
COPY worker/crates crates
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    cargo build --release --locked -p iki-worker \
 && install -m 0755 target/release/iki-worker /usr/local/bin/iki-worker

# --- Runtime --------------------------------------------------------------------
FROM ${DEBIAN_IMAGE} AS runtime
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 iki \
 && useradd --system --uid 10001 --gid iki --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin iki \
 && mkdir -p /opt/iki/plugins /opt/iki/envs /opt/iki/python
COPY --from=build /usr/local/bin/iki-worker /usr/local/bin/iki-worker
COPY --from=micromamba /usr/bin/micromamba /usr/local/bin/micromamba
# Plugin code and environments are root's and read-only to the worker; each
# job writes only in its own directory under IKI_WORK_DIR.
ENV IKI_PLUGINS_DIR=/opt/iki/plugins \
    IKI_WORK_DIR=/tmp/iki-worker \
    MAMBA_ROOT_PREFIX=/opt/iki/mamba
LABEL org.opencontainers.image.title="iki-worker" \
      org.opencontainers.image.source="https://github.com/yogesh-dhande/iki" \
      org.opencontainers.image.licenses="Apache-2.0"
# Numeric UID so Kubernetes can verify runAsNonRoot.
USER 10001:10001
ENTRYPOINT ["iki-worker"]
CMD ["start"]
