"""How many records of each type someone can read (ADR 0176): the sidebar's counts, and the
All entities page's type pills.

The counts are one grouped query over what the reader can read, by the rules that read one
record (:func:`iki_server.entities.access.visible`): a record in a project they aren't in
counts for nothing, and neither does a view showing something they can't read. Only current
records count, as the lists show them by default; registry entries belong to no project, and
count through their records.

**A count that would be slow is left out, not estimated** (ADR 0103's rule for sorts). A
count reads every record it counts: no index answers "how many", so it costs as much as the
reader has. Before counting, Postgres is asked how many rows the plan reads; past
:data:`COUNT_MAX` (about half a second's reading, ADR 0103) nothing is counted. What is
counted runs with a short time limit besides, in case the plan was wrong.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from django.db.models import Count, Exists, OuterRef, QuerySet

from iki_server.entities import access, search
from iki_server.entities.models import ProjectLink

#: The most rows a count may read, by Postgres's estimate: past this, it's left out.
COUNT_MAX = 1_000_000
#: How long counting may take, in milliseconds, should Postgres's estimate be wrong.
TIME_LIMIT_MS = 2_000


def _counted(user: Any, project: uuid.UUID | None) -> QuerySet[Any]:  # noqa: ANN401 - User
    found = access.visible(user).filter(archived_at__isnull=True)
    if project is not None:
        found = found.filter(
            Exists(ProjectLink.objects.filter(entity=OuterRef("pk"), project_id=project))
        )
    return found.order_by().values("type").annotate(n=Count("pk"))


def by_type(user: Any, project: uuid.UUID | None = None) -> dict[str, int] | None:  # noqa: ANN401 - User
    """How many current records of each concrete type ``user`` can read, in ``project`` if
    it's given (the caller checks they can read it): types with none are left out. None if
    counting would be slow."""
    grouped = _counted(user, project)
    try:
        with search.time_limit(TIME_LIMIT_MS):
            if _rows_read(json.loads(grouped.explain(format="json"))[0]["Plan"]) > COUNT_MAX:
                return None
            return {row["type"]: row["n"] for row in grouped}
    except search.TooSlow:
        return None


def _rows_read(plan: Mapping[str, Any], workers: int = 1) -> float:
    """The most rows any step of the plan reads, by Postgres's estimate: a parallel plan's
    are each worker's, and the leader's."""
    if plan["Node Type"] in ("Gather", "Gather Merge"):
        workers = plan.get("Workers Planned", 0) + 1
    own = float(plan["Plan Rows"]) * workers
    return max([own, *(_rows_read(p, workers) for p in plan.get("Plans", []))])
