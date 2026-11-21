"""Tables downloaded as CSV: cells a spreadsheet shows and never runs, streamed as written.

A spreadsheet runs a cell that starts with ``=``, ``+``, ``-`` or ``@`` as a formula, and
some do after a tab, a line break, or those signs' full-width forms. A value someone typed
into Iki could then run in the spreadsheet of whoever downloads it: OWASP's CSV injection.
:func:`cell` starts such a cell with a quote, which spreadsheets show as text, and every
cell is quoted, so a separator inside one (a comma, or the semicolon some locales split
on) can't start another (https://owasp.org/www-community/attacks/CSV_Injection). A number
Iki writes, ``-20``, stays a number.

Rows are written as they're read. Served over ASGI, as in production, each next chunk is
made in a thread rather than by Django consuming the whole iterator first.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Any

from asgiref.sync import sync_to_async
from django.core.handlers.asgi import ASGIRequest
from django.http import HttpRequest, StreamingHttpResponse
from django.utils.http import content_disposition_header

__all__ = ["cell", "chunks", "lines", "streamed"]

#: What a cell a spreadsheet would run starts with.
_RUNS = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")  # noqa: RUF001 - full-width
#: A number as Iki writes one, which a spreadsheet reads as that number.
_NUMBER = re.compile(r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def cell(value: object) -> str:
    """``value`` as a cell a spreadsheet shows as it is."""
    text = "" if value is None else str(value)
    if text.startswith(_RUNS) and not _NUMBER.fullmatch(text):
        return "'" + text
    return text


def lines(rows: Iterable[Iterable[object]]) -> Iterator[str]:
    """Each row as a line of CSV, every cell quoted and safe."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL)
    for row in rows:
        writer.writerow([cell(v) for v in row])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()


def chunks(rows: Iterable[Iterable[object]], size: int = 500) -> Iterator[str]:
    """The rows as CSV, ``size`` lines at a time: each a chunk of a download."""
    batch: list[str] = []
    for line in lines(rows):
        batch.append(line)
        if len(batch) == size:
            yield "".join(batch)
            batch = []
    if batch:
        yield "".join(batch)


def streamed(request: HttpRequest, parts: Iterator[str], file_name: str) -> StreamingHttpResponse:
    """A download of ``parts``, as ``file_name``, each sent as it's made."""
    body: Iterator[str] | AsyncIterator[str] = (
        _in_threads(parts) if isinstance(request, ASGIRequest) else parts
    )
    response = StreamingHttpResponse(body, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = content_disposition_header(True, file_name)
    response["Cache-Control"] = "no-store"
    return response


async def _in_threads(parts: Iterator[str]) -> AsyncIterator[str]:
    """``parts``, each made in Django's thread for the request, which its database
    connection belongs to."""

    def step() -> str | None:
        return next(parts, None)

    try:
        while (part := await sync_to_async(step, thread_sensitive=True)()) is not None:
            yield part
    finally:
        close: Any = getattr(parts, "close", None)
        if close is not None:
            await sync_to_async(close, thread_sensitive=True)()
