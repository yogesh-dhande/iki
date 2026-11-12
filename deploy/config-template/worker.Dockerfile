# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
# check=skip=InvalidDefaultArgInFrom
#
# A deployment's worker image: Iki's worker image with the plugins its
# lockfile pins and the environments they run in, built ahead so a worker
# installs nothing when it starts. From the deployment's config repository:
#
#   iki deploy build deployments/prod --output build/plugins --workers build/worker
#   docker build -f worker.Dockerfile \
#     --build-arg IKI_WORKER_IMAGE=ghcr.io/yogesh-dhande/iki-worker:0.1.0@sha256:... build/worker
#
# Packages come from PyPI and conda-forge unless the build says otherwise:
# pass a mirror as a secret (`--secret id=uv_index,env=UV_INDEX_URL`), never
# as a build argument, which the image would keep.

# Required, and deliberately without a default: say which worker image, by digest.
ARG IKI_WORKER_IMAGE
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

# --- Build the environments ---------------------------------------------------------
FROM ${IKI_WORKER_IMAGE} AS build
USER 0
COPY --from=uv /uv /usr/local/bin/uv
# Python interpreters live in the image, where the environments find them.
ENV UV_PYTHON_INSTALL_DIR=/opt/iki/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
COPY . /tmp/worker
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/opt/iki/mamba/pkgs \
    --mount=type=secret,id=uv_index,env=UV_INDEX_URL \
    sh /tmp/worker/build-environments.sh

# --- The deployment's worker -----------------------------------------------------------
FROM ${IKI_WORKER_IMAGE}
COPY --from=build /opt/iki/python /opt/iki/python
COPY --from=build /opt/iki/envs /opt/iki/envs
COPY plugins /opt/iki/plugins
