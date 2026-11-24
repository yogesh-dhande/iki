# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
#
# The antibody plugin's numbering-tools image, for its container-tier actions:
# ANARCI (BSD-3-Clause), which numbers antibody variable domains and finds
# their closest germlines with HMMER's hmmscan (BSD-3-Clause, from Debian), and
# the IMGT germlines its wheel carries (IMGT/GENE-DB, CC BY 4.0). Iki's publish
# workflow builds it from the plugin's directory, scans it, signs it and
# attests it like Iki's own images:
#   docker build -f plugins/antibody/images/numbering-tools/Dockerfile plugins/antibody
# and checks it with check.py (see there).
#
# Iki's worker is copied in when a run starts (ADR 0061): the image needs
# nothing of Iki's, and runs as any user.

FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# hmmscan, and Debian's security updates: official base images can lag days
# behind them. The same source, 3.4+dfsg-2, on both: Debian rebuilt it for arm64
# alone (+b2), so each architecture pins its own build.
ARG TARGETARCH
ARG APT_RETRY="-o Acquire::Retries=8 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30"
RUN case "${TARGETARCH}" in \
      amd64) hmmer=3.4+dfsg-2 ;; \
      arm64) hmmer=3.4+dfsg-2+b2 ;; \
      *) echo "no HMMER pinned for ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
 && apt-get $APT_RETRY update \
 && apt-get $APT_RETRY upgrade -y --no-install-recommends \
 && apt-get $APT_RETRY install -y --no-install-recommends "hmmer=${hmmer}" \
 && rm -rf /var/lib/apt/lists/*

# The same pinned, hashed packages as the plugin's backend environment. pip
# goes once they're in: runs install nothing.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt \
 && python -m pip uninstall -y pip

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
LABEL org.opencontainers.image.title="iki-antibody-numbering-tools" \
      org.opencontainers.image.description="The Iki antibody plugin's numbering tools: ANARCI, HMMER and IMGT's germlines" \
      org.opencontainers.image.source="https://github.com/yogesh-dhande/iki" \
      org.opencontainers.image.licenses="Apache-2.0 AND BSD-3-Clause AND CC-BY-4.0"
