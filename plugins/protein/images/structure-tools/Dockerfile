# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
#
# The protein plugin's structure-tools image, for its container-tier actions:
# DSSP (mkdssp 4.4, from Debian), the Chemical Component Dictionary it needs,
# and the plugin's own Python packages. Iki's
# publish workflow builds it from the plugin's directory, scans it, signs it
# and attests it like Iki's own images:
#   docker build -f plugins/protein/images/structure-tools/Dockerfile plugins/protein
# and checks it with check.py (see there).
#
# Iki's worker is copied in when a run starts (ADR 0061): the image needs
# nothing of Iki's, and runs as any user.

ARG BASE=python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# The wwPDB's Chemical Component Dictionary (CC0 1.0), which mkdssp reads each
# residue's chemistry from: without it, it refuses any file that doesn't carry
# its own residue definitions (most PDB-format files), and any modified residue.
# This is the PDBj annual snapshot of 2026-01-01, so results don't change from
# one build to the next; the checksum is the file's on that snapshot.
FROM ${BASE} AS ccd
ADD --checksum=sha256:4a487a587dd146bbfcf0c32de9fb057aef9345df63c15800bc344f7ae5f8c0ae \
    https://snapshots.pdbj.org/20260101/pub/pdb/data/monomers/components.cif.gz /components.cif.gz
RUN gunzip /components.cif.gz && chmod 0644 /components.cif

FROM ${BASE}

# mkdssp, and the mmCIF dictionaries it checks files against. Debian's security
# updates too: official base images can lag days behind them.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends dssp=4.4.10-1+b1 libcifpp-data \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ccd /components.cif /usr/share/libcifpp/components.cif

# The same pinned, hashed packages as the plugin's backend environment. pip
# goes once they're in: runs install nothing.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt \
 && python -m pip uninstall -y pip

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
LABEL org.opencontainers.image.title="iki-protein-structure-tools" \
      org.opencontainers.image.description="The Iki protein plugin's structure tools: DSSP and the plugin's Python packages" \
      org.opencontainers.image.source="https://github.com/yogesh-dhande/iki" \
      org.opencontainers.image.licenses="Apache-2.0 AND BSD-2-Clause AND CC0-1.0"
