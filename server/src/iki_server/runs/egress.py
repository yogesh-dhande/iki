"""What the egress proxy lets a run reach, and what it keeps of it (ADR 0053).

A run's command gets ``HTTPS_PROXY`` with its attempt's job ID and lease token as
credentials. The proxy lets a connection through only while that attempt holds its
lease, only to a host its action declares under ``network``, and only on port 443. Each
host an attempt reached, or was refused, is kept with the attempt, for its run's page.
"""

import base64
import binascii
import uuid
from dataclasses import dataclass

from django.db import connection
from django.db.models import F
from django.utils import timezone

from iki_server.runs.models import Job, JobEgress, JobState

#: The states in which an attempt's command may be running.
HELD = (JobState.LEASED, JobState.STAGING, JobState.RUNNING, JobState.UPLOADING)
PORT = 443


@dataclass(frozen=True)
class Decision:
    """The proxy's answer to one CONNECT: ``refused`` is empty when it's let through."""

    status: int
    refused: str = ""
    job_id: uuid.UUID | None = None

    @property
    def allowed(self) -> bool:
        return self.status == 200


def credentials(header: str | None) -> tuple[uuid.UUID, uuid.UUID] | None:
    """The job ID and lease token a ``Proxy-Authorization: Basic …`` header carries."""
    if not header:
        return None
    scheme, _, encoded = header.strip().partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        user, _, password = base64.b64decode(encoded.strip(), validate=True).decode().partition(":")
        return uuid.UUID(user), uuid.UUID(password)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def normal(host: str) -> str:
    return host.strip().rstrip(".").lower()


def declares(network: list[str], host: str) -> bool:
    """Whether an action's ``network`` covers `host`. ``*.example.com`` covers its
    subdomains, at any depth, but not example.com itself."""
    for pattern in network:
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False


def _held(job_id: uuid.UUID, token: uuid.UUID) -> Job | None:
    return (
        Job.objects.select_related("run")
        .filter(
            pk=job_id,
            lease_token=token,
            state__in=HELD,
            lease_expires_at__gt=timezone.now(),
        )
        .first()
    )


def decide(header: str | None, host: str, port: int, *, tunnel: bool = True) -> Decision:
    """Whether to let the attempt whose credentials `header` gives reach `host`:`port`:
    only through a tunnel (CONNECT), never as plain HTTP. Refusals of a held attempt are
    kept, so its run's page says what was refused."""
    given = credentials(header)
    if given is None:
        return Decision(
            407,
            "The proxy needs the credentials Iki gives each run in HTTPS_PROXY.",
        )
    job = _held(*given)
    if job is None:
        return Decision(407, "These credentials are for an attempt that has ended.")
    host = normal(host)
    network: list[str] = job.run.spec.get("network") or []
    if not tunnel:
        why = "Runs reach hosts over HTTPS only: use an https:// address."
    elif port != PORT:
        why = "Runs reach hosts over HTTPS, on port 443, only."
    elif not declares(network, host):
        why = (
            f"{job.run.action} doesn't declare {host}. An action reaches only the hosts "
            "its plugin lists under `network`."
        )
    else:
        return Decision(200, job_id=job.pk)
    record(job.pk, host, port, why)
    return Decision(403, why, job.pk)


def still_held(job_id: uuid.UUID, header: str | None) -> bool:
    """Whether the attempt still holds its lease: its tunnels close as soon as it doesn't."""
    given = credentials(header)
    return given is not None and given[0] == job_id and _held(*given) is not None


def record(job_id: uuid.UUID, host: str, port: int, refused: str = "") -> None:
    """Counts a connection to `host`, or a refusal, for the attempt."""
    table = JobEgress._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO {table} (job_id, host, port, refused, connections, bytes_sent,
                                 bytes_received, first_at, last_at)
            VALUES (%s, %s, %s, %s, 1, 0, 0, now(), now())
            ON CONFLICT (job_id, host, port, refused)
            DO UPDATE SET connections = {table}.connections + 1, last_at = now()
            """,  # noqa: S608 - the table name is ours; values are parameters
            [job_id, host[:253], port, refused[:300]],
        )


def count(job_id: uuid.UUID, host: str, port: int, sent: int, received: int) -> None:
    """Adds what a closed tunnel carried."""
    JobEgress.objects.filter(job_id=job_id, host=host, port=port, refused="").update(
        bytes_sent=F("bytes_sent") + sent,
        bytes_received=F("bytes_received") + received,
        last_at=timezone.now(),
    )
