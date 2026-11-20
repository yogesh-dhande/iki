"""What every machine Iki starts for runs gives its worker (ADR 0061, ADR 0074), whatever
starts it: a Kubernetes pod or an EC2 instance. The worker runs in the environment's image,
registers with a token made for that machine alone, and gets the template's capacity."""

from __future__ import annotations

import re

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from iki_spec.compute import Template

__all__ = [
    "BIN",
    "IDLE_EXIT",
    "RUN_LABELS",
    "SCRATCH",
    "UID",
    "public_certificates",
    "worker_settings",
]

#: Where iki-worker is in a run's container, and the scratch space a run gets.
BIN = "/iki/bin"
SCRATCH = "/iki/scratch"
#: The user runs run as, as Iki's own images do.
UID = 10001
#: What a machine is labelled or tagged with while it's a run's (ADR 0070), for tools that
#: put a cloud's costs down to projects, as Iki's own records do.
RUN_LABELS = ("iki.ikisci.org/run", "iki.ikisci.org/project", "iki.ikisci.org/team")
#: Seconds an ephemeral machine's worker waits for a job before it stops.
IDLE_EXIT = 300


def worker_settings(
    template: Template,
    environment: str,
    image: str,
    *,
    server_url: str,
    allow_http: bool,
    once: bool,
) -> dict[str, str]:
    """The worker's environment in a run's container, but its name and token."""
    have = template.capacity
    values = {
        "IKI_URL": server_url,
        "IKI_WORKER_CPU": str(have.cpu),
        "IKI_WORKER_MEMORY": str(have.memory),
        "IKI_WORKER_GPUS": str(have.gpu_count),
        "IKI_WORKER_GPU_MEMORY": str(have.gpu_memory) if have.gpu_memory else None,
        "IKI_WORKER_SCRATCH_DISK": str(have.scratch_disk) if have.scratch_disk else None,
        "IKI_WORKER_ENVIRONMENT": environment,
        "IKI_WORKER_IMAGE": image,
        "IKI_WORKER_CACHE": f"{SCRATCH}/cache",
        "IKI_WORK_DIR": f"{SCRATCH}/jobs",
        "IKI_WORKER_AUTO_UPDATE": "0",
        # The root filesystem is read-only: the worker's own temporary files go in the
        # scratch volume. Each run's command gets a home and temporary directory of its own.
        "HOME": SCRATCH,
        "TMPDIR": SCRATCH,
        # A machine for one run that another took, or that was cancelled, stops rather than
        # waits. A persistent one waits until the provisioner stops it (ADR 0062).
        "IKI_WORKER_IDLE_EXIT": str(IDLE_EXIT) if once else None,
        # It runs one job after another: the scratch volume, plugin code and all, is
        # emptied after each, so the next starts clean.
        "IKI_WORKER_CLEAN_DIR": None if once else SCRATCH,
        "IKI_ALLOW_HTTP": "1" if allow_http else None,
    }
    return {name: value for name, value in values.items() if value is not None}


#: A PEM block: its label, and what's up to the END line of the same label, which holds no
#: other block's lines.
_PEM = re.compile(rb"-----BEGIN ([^-\r\n]*)-----(?:(?!-----).)*-----END \1-----", re.DOTALL)


def public_certificates(pem: bytes) -> str:
    """The X.509 certificates in `pem`, written again as PEM, one after another, and
    nothing else of it: what machines' workers trust besides their own roots (ADR 0160).
    Each machine's boot script carries them, and every run's container can read them, so a
    file with anything but certificates in PEM blocks (a private key beside them, say) is
    refused, as is a block that isn't whole or a certificate that can't be read. Text
    between the blocks (openssl's descriptions of them) is left out. ValueError says why."""
    blocks = list(_PEM.finditer(pem))
    others = sorted({block[1].decode("ascii", "replace") for block in blocks} - {"CERTIFICATE"})
    if others:
        raise ValueError(
            f"holds {', '.join(others)} besides certificates: give the CA's certificates "
            "alone, never a key, since every machine and run can read them"
        )
    if b"-----" in _PEM.sub(b"", pem):
        raise ValueError("holds a PEM block that isn't whole")
    if not blocks:
        raise ValueError("holds no PEM certificate")
    certificates = []
    for number, block in enumerate(blocks, 1):
        try:
            certificates.append(x509.load_pem_x509_certificate(block[0]))
        except ValueError as error:
            raise ValueError(f"certificate {number} can't be read: {error}") from None
    return "".join(c.public_bytes(Encoding.PEM).decode("ascii") for c in certificates)
