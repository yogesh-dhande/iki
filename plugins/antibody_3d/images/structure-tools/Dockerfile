# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
#
# The antibody_3d plugin's structure-tools image (named iki-antibody-structure-tools, as it
# was when the antibody plugin carried it), for predicting antibodies'
# structures on the container tier: ImmuneBuilder 1.2 (BSD-3-Clause), whose
# ABodyBuilder2 and NanoBodyBuilder2 model a heavy and a light chain's variable
# domains or a single domain's, with its trained weights (Zenodo record
# 7258553, CC BY 4.0); PyTorch's CPU build (BSD-3-Clause and others, in its
# wheel's licenses/); OpenMM (MIT; its OpenCL platform, which its wheel carries
# and these runs don't use, LGPL-3.0) and PDBFixer (MIT), which refine the
# model; and ANARCI and HMMER (BSD-3-Clause), which number the chains first. Iki's publish workflow builds
# it from the plugin's directory, scans it, signs it and attests it:
#   docker build -f plugins/antibody_3d/images/structure-tools/Dockerfile plugins/antibody_3d
# and checks it with check.py (see there).
#
# Iki's worker is copied in when a run starts (ADR 0061): the image needs
# nothing of Iki's, and runs as any user, with no network.

FROM python:3.12-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# hmmscan, and Debian's security updates. Debian rebuilt HMMER for arm64 alone
# (+b2), so each architecture pins its own build of the same source.
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

# Pinned and hashed, the same versions on both architectures: uv.lock beside
# this file, exported to requirements.txt. PyTorch's CPU build is on its own
# index; the hashes say which files are wanted, wherever they're found. pip goes
# once they're in: runs install nothing.
COPY images/structure-tools/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes \
      --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cpu \
      -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt \
 && python -m pip uninstall -y pip

# The separately published scratch image supplies the weights. Verify the
# complete set of copied files against the original Zenodo hashes.
RUN mkdir -p -m 0755 /opt/immunebuilder/weights /usr/share/licenses/openmm
COPY --from=ghcr.io/yogesh-dhande/iki-immunebuilder-weights@sha256:fc6fb1b804ceeee08eed3f20237d33a32d4797a95267411b6d3682f2632fc761 --chmod=0444 \
    /weights/antibody_model_[1234] \
    /weights/nanobody_model_[1234] \
    /opt/immunebuilder/weights/
COPY images/structure-tools/SHA256SUMS images/structure-tools/WEIGHTS-NOTICE.txt /usr/share/licenses/immunebuilder-weights/
RUN --network=none cd /opt/immunebuilder/weights \
 && sha256sum antibody_model_* nanobody_model_* \
    | diff -u /usr/share/licenses/immunebuilder-weights/SHA256SUMS -

# OpenMM's licences, which its wheel doesn't carry: its own, the LGPL's and the
# GPL's that it incorporates, and the notices of what it bundles, at the commit
# its wheel was built from (openmm/version.py names it).
ARG OPENMM_LICENSES=https://raw.githubusercontent.com/openmm/openmm/b399af4725573963b46d6c1083fdcf7a37615857/docs-source/licenses
ADD --chmod=0444 --checksum=sha256:437b7168cc997abea3b5f2a9e0fb6894f96de77b9c69be428ccfcfe9bed58293 ${OPENMM_LICENSES}/Licenses.txt /usr/share/licenses/openmm/Licenses.txt
ADD --chmod=0444 --checksum=sha256:9ccf26cfe845e0eb8bb58053e47366e7ab6b697ae010f7650978d4b71b7d1fc1 ${OPENMM_LICENSES}/LGPL.txt /usr/share/licenses/openmm/LGPL.txt
ADD --chmod=0444 --checksum=sha256:8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903 ${OPENMM_LICENSES}/GPL.txt /usr/share/licenses/openmm/GPL.txt

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 IMMUNEBUILDER_WEIGHTS=/opt/immunebuilder/weights
USER 65532:65532
LABEL org.opencontainers.image.title="iki-antibody-structure-tools" \
      org.opencontainers.image.description="The Iki antibody plugin's structure tools: ImmuneBuilder with its weights, PyTorch (CPU), OpenMM, PDBFixer, ANARCI and HMMER" \
      org.opencontainers.image.source="https://github.com/yogesh-dhande/iki" \
      org.opencontainers.image.licenses="Apache-2.0 AND BSD-3-Clause AND CC-BY-4.0 AND MIT AND LGPL-3.0-only"
