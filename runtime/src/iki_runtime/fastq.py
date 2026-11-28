"""FASTQ, read as Cock et al. (2010) define it: https://doi.org/10.1093/nar/gkp1137.

A record is an ``@`` title line naming the read, its bases on one line or several, a
``+`` line (which may repeat the title), and one Phred+33 quality character per base, on
one line or several. A quality line may begin with ``@`` or ``+``, so a record ends when
it has as many qualities as bases, not at the next line that looks like a title. Blank
lines between records are passed over.

Every reader of FASTQ in Iki's plugins reads it with this one, so two analyses of the
same file can't count its reads differently.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import BinaryIO, Iterator, NamedTuple

from iki_runtime.text import open_bytes

__all__ = ["MAX_READ_LENGTH", "PHRED_OFFSET", "Read", "read", "reads"]

#: Sanger FASTQ's quality offset (Cock et al. 2010, Table 1): ``!`` is Phred 0.
PHRED_OFFSET = 33
#: The longest read, and line, it reads: longer is refused rather than held in memory.
MAX_READ_LENGTH = 10_000_000
_QUALITY = re.compile(rb"[!-~]+")
#: IUPAC's nucleotide codes, either case, and ``.`` for a base not called.
_BASES = re.compile(rb"[ACGTURYSWKMBDHVNacgturyswkmbdhvn.]+")


class Read(NamedTuple):
    """A read: its title (after the ``@``), bases and qualities, as the file has them."""

    title: bytes
    bases: bytes
    quality: bytes

    @property
    def id(self) -> bytes:
        """Its identifier: its title's first word."""
        return self.title.split(maxsplit=1)[0]


def _strip(raw: bytes) -> bytes:
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    return raw


def _line(source: BinaryIO) -> bytes:
    content = _strip(source.readline(MAX_READ_LENGTH + 3))
    if len(content) > MAX_READ_LENGTH:
        raise ValueError("a FASTQ line exceeds 10 million characters.")
    return content


def reads(source: BinaryIO) -> Iterator[Read]:
    """Each read in ``source``, a binary stream, in order. Raises ``ValueError`` saying
    which read, from 1, isn't FASTQ, and why."""
    number = 1
    while True:
        header = source.readline(MAX_READ_LENGTH + 3)
        if not header:
            return
        if not header.strip():
            continue  # a blank line between records, or at the end
        try:
            title = _strip(header)
            if len(title) > MAX_READ_LENGTH or not title.startswith(b"@") or not title[1:].split():
                raise ValueError("expected an @ title with a read identifier.")
            bases = bytearray()
            while True:
                part = _line(source)
                if part.startswith(b"+"):
                    break
                if not _BASES.fullmatch(part):
                    raise ValueError("expected sequence bases followed by a + separator.")
                bases.extend(part)
                if len(bases) > MAX_READ_LENGTH:
                    raise ValueError("the sequence exceeds 10 million bases.")
            if not bases:
                raise ValueError("the read has no bases.")
            if part[1:] and part[1:] != title[1:]:
                raise ValueError("the + title differs from the @ title.")
            quality = bytearray()
            while len(quality) < len(bases):
                part = _line(source)
                if not _QUALITY.fullmatch(part):
                    raise ValueError("expected printable Phred+33 qualities for every base.")
                quality.extend(part)
            if len(quality) != len(bases):
                raise ValueError("the base and quality counts differ.")
        except ValueError as exc:
            raise ValueError(f"read {number}: {exc}") from exc
        yield Read(title[1:], bytes(bases), bytes(quality))
        number += 1


def read(path: str | Path) -> Iterator[Read]:
    """Each read in the FASTQ file at ``path``, compressed or not (see
    :mod:`iki_runtime.text`)."""
    with open_bytes(path) as source:
        yield from reads(source)
