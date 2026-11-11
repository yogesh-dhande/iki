"""Publishing live updates.

An event is addressed to one person and carries only identifiers and
counts, never content: streams tell the page *that* something changed, and
the page reads what changed through the API, with that person's permissions.
Postgres delivers the notification when the transaction commits, so a
rolled-back change sends nothing.
"""

import json
from typing import Any
from uuid import UUID

from django.db import connection

CHANNEL = "iki_events"
#: Postgres refuses NOTIFY payloads of 8000 bytes or more.
MAX_PAYLOAD = 7000


def publish(user_id: UUID, event: str, data: dict[str, Any] | None = None) -> None:
    """To each of one person's streams."""
    publish_to(str(user_id), event, data)


def publish_to(topic: str, event: str, data: dict[str, Any] | None = None) -> None:
    """To every stream following ``topic``: a person's ID, or a thing many people may
    watch, such as ``run:<id>``. Each stream checks its caller may see it before sending."""
    payload = json.dumps({"to": topic, "event": event, "data": data or {}}, separators=(",", ":"))
    if len(payload.encode()) > MAX_PAYLOAD:
        raise ValueError(f"Event {event!r} is too large for NOTIFY; send identifiers, not content.")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_notify(%s, %s)", [CHANNEL, payload])
