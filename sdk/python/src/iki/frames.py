"""Results as pandas DataFrames: ``pip install 'ikisci[pandas]'``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from iki.errors import IkiError

if TYPE_CHECKING:
    import pandas as pd

__all__ = ["entities", "records", "runs"]

#: What every entity has, before its own fields.
_ENTITY_COLUMNS = ("type", "revision", "archived", "created_at", "updated_at", "id")
_RUN_COLUMNS = (
    "action",
    "status",
    "project",
    "requested_by",
    "via",
    "reason",
    "tier",
    "created_at",
    "started_at",
    "finished_at",
    "id",
)
_TIMES = ("created_at", "updated_at", "started_at", "finished_at", "archived_at")


def _pandas() -> Any:  # noqa: ANN401 - the module
    try:
        import pandas as pd
    except ImportError:
        raise IkiError(
            "DataFrames need pandas: pip install 'ikisci[pandas]' (or pip install pandas)."
        ) from None
    return pd


def _dates(frame: pd.DataFrame) -> pd.DataFrame:
    pd = _pandas()
    for column in _TIMES:
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], utc=True, format="ISO8601")
    return frame


def _readable(value: Any, refs: Mapping[str, Any]) -> Any:  # noqa: ANN401 - a value
    """A value as people read it: references by their HRIDs."""
    if isinstance(value, str) and value in refs:
        return refs[value].get("hrid", value)
    if isinstance(value, list):
        return [_readable(v, refs) for v in value]
    return value


def entities(items: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """One row per entity, indexed by HRID: what every entity has, then its values,
    one column per field. References show the HRIDs of the entities they refer to."""
    pd = _pandas()
    rows = []
    for entity in items:
        refs = entity.get("refs") or {}
        row: dict[str, Any] = {"hrid": entity["hrid"]}
        row.update({key: entity.get(key) for key in _ENTITY_COLUMNS})
        owner = entity.get("owner_project")
        row["project"] = owner.get("slug") if isinstance(owner, Mapping) else None
        for field, value in (entity.get("values") or {}).items():
            # A field named like a column above keeps both: the field gets a prefix.
            row[field if field not in row else f"values.{field}"] = _readable(value, refs)
        rows.append(row)
    frame = pd.DataFrame(rows, columns=None if rows else ["hrid", *_ENTITY_COLUMNS])
    return _dates(frame).set_index("hrid")


def runs(items: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """One row per run, indexed by HRID, with who asked for it and how it went."""
    pd = _pandas()
    rows = []
    for run in items:
        row: dict[str, Any] = {"hrid": run["hrid"]}
        row.update({key: run.get(key) for key in _RUN_COLUMNS})
        person = run.get("requested_by")
        row["requested_by"] = person.get("name") if isinstance(person, Mapping) else person
        row["inputs"] = [i.get("hrid") for i in run.get("inputs") or []]
        row.update({f"metrics.{k}": v for k, v in (run.get("metrics") or {}).items()})
        rows.append(row)
    frame = pd.DataFrame(rows, columns=None if rows else ["hrid", *_RUN_COLUMNS])
    return _dates(frame).set_index("hrid")


def records(items: Sequence[Any]) -> pd.DataFrame:
    """Anything else: nested objects become dotted columns."""
    pd = _pandas()
    return _dates(pd.json_normalize(list(items)))
