"""Types for browsing files with the Python SDK."""

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Literal, TypedDict

from iki.schema import FileDetailOut, PageHoldingRecordOut, PageUsingRunOut

type FileSort = Literal["newest", "oldest", "name", "name_desc", "acquired", "acquired_oldest"]


class FileFilters(TypedDict, total=False):
    """Keyword filters shared by ``files.list`` and ``files.count``."""

    project: str | Mapping[str, Any] | None
    q: str | None
    format: str | Sequence[str] | None
    acquired_from: date | str | None
    acquired_to: date | str | None
    sample: str | None
    run: str | None
    accepts: str | Sequence[str] | None
    max_size: int | None


class FileWithUses(FileDetailOut):
    """File metadata with independently paged record revisions and runs."""

    records: PageHoldingRecordOut
    runs: PageUsingRunOut
