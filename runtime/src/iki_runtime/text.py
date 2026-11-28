"""Files read as they were written, compressed or not.

Sequencers, pipelines and search engines write their reports plain or compressed, and
a file's name says nothing reliable about which: ``reads.fastq`` can be gzipped, and
``report.tsv.gz`` can be plain. So a file's compression is read from its first bytes,
its magic number, never from its name or media type:

- gzip, ``1f 8b`` (RFC 1952), which BGZF, the blocked gzip of BAM and tabix, also is;
- bzip2, ``BZh``;
- xz, ``fd 37 7a 58 5a 00``.

Anything else is read as it is.
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import zlib
from pathlib import Path
from typing import IO, BinaryIO, TextIO, Union

__all__ = ["UNREADABLE", "compression", "open_bytes", "open_text"]

#: The compressions read, by the first bytes that mark them.
MAGIC = (
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
)
#: What reading a damaged or cut-off compressed file raises, besides ``ValueError``:
#: give it to ``iki.main(..., refuse=...)`` so such a file fails its run with the reason.
UNREADABLE: tuple[type[BaseException], ...] = (EOFError, OSError, zlib.error, lzma.LZMAError)

PathLike = Union[str, Path]


def compression(path: PathLike) -> str | None:
    """The compression ``path``'s first bytes mark (``"gzip"``, ``"bzip2"``, ``"xz"``),
    or None for a file read as it is."""
    with Path(path).open("rb") as source:
        start = source.read(6)
    for magic, name in MAGIC:
        if start.startswith(magic):
            return name
    return None


def open_bytes(path: PathLike) -> BinaryIO:
    """``path``'s bytes, decompressed if its first bytes mark a compression."""
    found = compression(path)
    if found == "gzip":
        return gzip.open(path, "rb")  # type: ignore[return-value]
    if found == "bzip2":
        return bz2.open(path, "rb")  # type: ignore[return-value]
    if found == "xz":
        return lzma.open(path, "rb")  # type: ignore[return-value]
    return Path(path).open("rb")


def open_text(
    path: PathLike,
    *,
    encoding: str = "utf-8-sig",
    errors: str = "strict",
    newline: str | None = "",
) -> TextIO:
    """``path``'s text, decompressed if its first bytes mark a compression.

    By default it's UTF-8, a byte-order mark at its start dropped, and lines keep their
    endings as written (``newline=""``), which is what :mod:`csv` reads; give
    ``newline=None`` for lines ending ``"\\n"`` whatever the file had.
    """
    raw: IO[bytes] = open_bytes(path)
    return io.TextIOWrapper(raw, encoding=encoding, errors=errors, newline=newline)
