# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
#
# The protein plugin's alignment-tools image, for its container-tier actions:
# MAFFT (7.505, from Debian, without its GPL-licensed extensions, which Debian
# leaves out) and the plugin's own Python packages. Iki's publish workflow
# builds it from the plugin's directory, scans it, signs it and attests it like
# Iki's own images:
#   docker build -f plugins/protein/images/alignment-tools/Dockerfile plugins/protein
# and checks it with check.py (see there).
#
# Iki's worker is copied in when a run starts (ADR 0061): the image needs
# nothing of Iki's, and runs as any user.

ARG BASE=python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

FROM ${BASE}

# MAFFT, and Debian's security updates: official base images can lag days behind them.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends mafft=7.505-1+b1 \
 && rm -rf /var/lib/apt/lists/*

# The same pinned, hashed packages as the plugin's backend environment. pip
# goes once they're in: runs install nothing.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt \
 && python -m pip uninstall -y pip

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
LABEL org.opencontainers.image.title="iki-protein-alignment-tools" \
      org.opencontainers.image.description="The Iki protein plugin's alignment tools: MAFFT and the plugin's Python packages" \
      org.opencontainers.image.source="https://github.com/yogesh-dhande/iki" \
      org.opencontainers.image.licenses="Apache-2.0 AND BSD-3-Clause"
