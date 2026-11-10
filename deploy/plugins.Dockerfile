# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
# check=skip=InvalidDefaultArgInFrom
#
# A deployment's image: Iki's server image with the plugins its lockfile pins.
# From the deployment's config repository:
#
#   iki deploy build deployments/prod --output build/plugins
#   docker build -f plugins.Dockerfile \
#     --build-arg IKI_SERVER_IMAGE=ghcr.io/ikisci/iki-server:0.1.0@sha256:... build
#
# Use the server image of the Iki version that ran `iki deploy build`, pinned
# by digest: the server refuses plugins another version built.

# Required, and deliberately without a default: say which server image, by digest.
ARG IKI_SERVER_IMAGE
FROM ${IKI_SERVER_IMAGE}
USER 0
COPY plugins /opt/iki/plugins
# Owned by root and read-only for the server. Compiled ahead, as the rest of
# the image is, with hashes rather than timestamps so the build is repeatable.
RUN chown -R 0:0 /opt/iki/plugins \
 && chmod -R u=rwX,go=rX /opt/iki/plugins \
 && python -m compileall -q -j 0 --invalidation-mode unchecked-hash /opt/iki/plugins
USER 10001:10001
