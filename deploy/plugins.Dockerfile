# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
# check=skip=InvalidDefaultArgInFrom
#
# A deployment's image: Iki's server image with the plugins its lockfile pins,
# and their writers' modules, built from source and checked (ADR 0230).
# From the deployment's config repository:
#
#   iki deploy build deployments/prod --output build/plugins
#   docker build -f plugins.Dockerfile \
#     --build-arg IKI_SERVER_IMAGE=ghcr.io/yogesh-dhande/iki-server:0.1.0@sha256:... build
#
# Use the server image of the Iki version that ran `iki deploy build`, pinned
# by digest: the server refuses plugins another version built.

# Required, and deliberately without a default: say which server image, by digest.
ARG IKI_SERVER_IMAGE
ARG IKI_WRITER_BUILDER=rust:1.93.0-slim-trixie@sha256:760ad1d638d70ebbd0c61e06210e1289cbe45ff6425e3ea6e01241de3e14d08e

# --- Plugins' writers (ADR 0230) ----------------------------------------------
# Each writer's Rust crate becomes its WebAssembly module here: on linux/amd64, the
# platform its tracked digest is of, offline, with no credentials and its paths
# remapped. Fetching the crates is the only step with a network. A site without
# crates.io sets IKI_CARGO_MIRROR to its registry mirror, or its plugins vendor
# their crates. `iki plugin writers --update` runs these same stages.
FROM --platform=linux/amd64 ${IKI_WRITER_BUILDER} AS writer-build
ARG IKI_CARGO_MIRROR=
COPY plugins/iki_installed_plugins/__init__.py plugins/iki_installed_plugins/writer[s] /src/writers/
RUN set -eu; mkdir -p /out; \
    if [ -s /src/writers/writers.txt ]; then \
      rustup target add wasm32-unknown-unknown; \
      while read -r crate; do \
        sh /src/writers/sdk/iki-writer/build-writer.sh fetch "/src/writers/$crate"; \
      done < /src/writers/writers.txt; \
    fi
RUN --network=none set -eu; \
    if [ -s /src/writers/writers.txt ]; then \
      while read -r crate; do \
        mkdir -p "/out/$(dirname "$crate")"; \
        sh /src/writers/sdk/iki-writer/build-writer.sh build           "/src/writers/$crate" "/out/$crate.wasm"; \
      done < /src/writers/writers.txt; \
    fi
FROM scratch AS writer-wasm
COPY --from=writer-build /out /

# Each module checked against the digest its plugin tracks, refused if it imports
# anything or breaks the ABI or a limit, then precompiled with this image's Wasmtime
# for the sandbox, which checks both digests again before it maps the code.
FROM ${IKI_SERVER_IMAGE} AS writers
USER 0
COPY --from=writer-wasm / /tmp/writer-wasm/
COPY plugins/iki_installed_plugins/__init__.py plugins/iki_installed_plugins/writer[s] /tmp/writer-src/
RUN /opt/iki/venv/bin/python -m iki_spec.writers.install /tmp/writer-src /tmp/writer-wasm /opt/iki/writers \
 && chown -R 0:0 /opt/iki/writers \
 && chmod -R u=rwX,go=rX /opt/iki/writers

FROM ${IKI_SERVER_IMAGE}
USER 0
# The writers' sources stay in the build: the image carries the checked modules.
COPY --exclude=iki_installed_plugins/writers plugins /opt/iki/plugins
COPY --from=writers /opt/iki/writers /opt/iki/writers
# Owned by root and read-only for the server. Compiled ahead, as the rest of
# the image is, with hashes rather than timestamps so the build is repeatable.
RUN chown -R 0:0 /opt/iki/plugins \
 && chmod -R u=rwX,go=rX /opt/iki/plugins \
 && python -m compileall -q -j 0 --invalidation-mode unchecked-hash /opt/iki/plugins
USER 10001:10001
