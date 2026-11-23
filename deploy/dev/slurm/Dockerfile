# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
#
# A whole Slurm cluster in one container, for testing Iki's Slurm worker (ADR 0133):
# slurmctld, slurmd as its one compute node, slurmdbd with MariaDB for accounting,
# munge, and Apptainer. The container is its own login node: the worker runs in it as
# the `iki` service account, with /scratch/iki as the shared work directory.
#
#   make check-slurm      # builds this, then runs server/tests/test_slurm_e2e.py
#
# Apptainer ships amd64 packages only, so the image is linux/amd64 (emulated on Arm).
# It runs privileged, for Apptainer's mounts. Development and CI only.

ARG DEBIAN_IMAGE=debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

FROM ${DEBIAN_IMAGE}

# Apptainer's own Debian 13 packages, from its release, checked by digest. The setuid
# starter mounts images without user namespaces, which CI runners restrict.
ARG APPTAINER_VERSION=1.5.5
ARG APPTAINER_SHA256=82a6e5ead21d3e19de522a9e0bbeb59958323e316dc9d019a128b0cc8e99d863
ARG APPTAINER_SUID_SHA256=5b7bac9b2ff93f74e7423b9ba8afd2f99d044968e1f030e4776cdcf7af61a16b

ARG APT_RETRY="-o Acquire::Retries=8 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30"
RUN apt-get $APT_RETRY update \
 && apt-get $APT_RETRY install -y --no-install-recommends \
      ca-certificates curl munge mariadb-server \
      slurm-wlm slurmdbd slurm-client \
 && base="https://github.com/apptainer/apptainer/releases/download/v${APPTAINER_VERSION}" \
 && curl -fsSL -o /tmp/apptainer.deb "$base/apptainer_${APPTAINER_VERSION}-trixie+_amd64.deb" \
 && curl -fsSL -o /tmp/apptainer-suid.deb "$base/apptainer-suid_${APPTAINER_VERSION}-trixie+_amd64.deb" \
 && echo "${APPTAINER_SHA256}  /tmp/apptainer.deb" | sha256sum -c - \
 && echo "${APPTAINER_SUID_SHA256}  /tmp/apptainer-suid.deb" | sha256sum -c - \
 && apt-get $APT_RETRY install -y --no-install-recommends /tmp/apptainer.deb /tmp/apptainer-suid.deb \
 && rm -rf /tmp/*.deb /var/lib/apt/lists/* \
 && groupadd --gid 10001 iki \
 && useradd --uid 10001 --gid iki --create-home --shell /bin/bash iki \
 && install -d -m 0700 -o iki -g iki /scratch/iki \
 && install -d -o slurm -g slurm /var/spool/slurmctld /var/spool/slurmd /var/log/slurm

COPY --chmod=0644 slurm.conf cgroup.conf /etc/slurm/
COPY --chmod=0600 --chown=slurm:slurm slurmdbd.conf /etc/slurm/
COPY --chmod=0755 start.sh /usr/local/bin/start-slurm

ENTRYPOINT ["/usr/local/bin/start-slurm"]
