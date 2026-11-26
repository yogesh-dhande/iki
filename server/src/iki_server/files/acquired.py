"""What a file says about itself: when its data was acquired (ADR 0177), and which
instrument, who and what recorded it (ADR 0224).

An instrument stamps what it records: a sequencer the run's start, a cytometer the
acquisition's, a camera the moment it took the image. In GxP work that stamp is part of
the original record, so Iki reads it from the file and shows it as the file gives it.
It never stands in for it with something else: not the upload time, not the file's
modification time, not "now". When the file doesn't clearly say, this says None.

What's read, by format:

- **ABIF** (Sanger traces: ``.ab1``, ``.abi``, ``.fsa``): ``RUND`` 1 and ``RUNT`` 1, the
  run's start. ``RUND`` 2 and ``RUNT`` 2 are its end. ABIF has no time zone. Hundredths
  of a second are always recorded, so they're always given.
- **FCS** (flow cytometry): FCS 3.2's ``$BEGINDATETIME``, else ``$DATE`` and ``$BTIM``.
  A two-digit year (FCS 2.0's ``dd-mmm-yy``) is None: which century it means is a guess.
  FCS 2.0's sixtieths of a second (``hh:mm:ss:tt``) are dropped, not turned into a
  decimal fraction the file never gave.
- **mzML**: the ``<run>`` element's ``startTimeStamp``, in the first MiB.
- **JCAMP-DX** (spectra): ``##LONGDATE``, else ``##DATE`` and ``##TIME``. Only a
  ``YYYY/MM/DD`` date is read: the older ``YY/MM/DD`` and ``DD/MM/YY`` can't be told
  apart.
- **TIFF and JPEG**: EXIF's ``DateTimeOriginal``, with ``SubSecTimeOriginal`` and
  ``OffsetTimeOriginal`` when they're there. Never IFD0's ``DateTime``: that's when the
  file was last changed. An OME-TIFF's ``AcquisitionDate`` (its first Image's) comes first.
- **SAM and BAM**: the earliest ``DT`` of the header's ``@RG`` lines. When every one
  states an offset they're compared as instants, otherwise as the wall-clock times given.
- **CZI** (ZEISS microscopy): the document's ``Information/Image/AcquisitionDateAndTime``.

The same readers read the facts of :mod:`iki_server.files.metadata` that each format
defines: the instrument, its serial number, who operated it, the software and protocol
it ran, and a SAM or BAM file's read groups. Each reader's section says which, and where
its format's specification defines them. A fact the file doesn't state, or states twice
differently, is unknown; one that's malformed is unknown without making the rest so, and
the date and the facts are read independently of each other.

``at`` is no more precise than the file: a date, or a time to the second or to the
fraction given, with an offset only when the file states one. RFC 3339's ``-00:00``
("offset unknown") is no offset. Dates are 1980 to 2200, real calendar dates; a field
that's there but malformed makes the whole answer None, rather than half of it.

Files are read where they are, in object storage, by ranged reads: each reader reads at
most 4 MiB, in a few reads, and never the whole of a big file. A malformed or truncated
file is None; an error reading the source itself is raised as it was, so the caller can
try again.
"""

from __future__ import annotations

import csv
import datetime as dt
import functools
import json
import re
import struct
import zlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from importlib import resources
from typing import Protocol

from iki_server.files import metadata
from iki_server.files.metadata import Element, Fact, Facts

__all__ = ["READERS", "SOURCES", "Acquired", "BytesSource", "Fact", "FileMetadata", "Readable"]


@dataclass(frozen=True)
class Acquired:
    """When a file's data was acquired, as the file says."""

    #: The date, as the file gives it: the instrument's local date.
    on: dt.date
    #: ISO 8601 as the file gives it, no more precise than it is: ``2026-03-14``,
    #: ``2026-03-14T09:21:07``, ``2026-03-14T09:21:07.25`` or ``2026-03-14T09:21:07+01:00``.
    at: str
    #: The source of the date, as named in :data:`SOURCES`.
    source: str


class Readable(Protocol):
    """A file's bytes, read by range: an object in storage, or bytes in memory."""

    @property
    def size(self) -> int: ...

    def read(self, offset: int, length: int) -> bytes:
        """Up to ``length`` bytes at ``offset``: fewer at the end of the file."""
        ...


class BytesSource:
    """Bytes in memory, as a :class:`Readable`."""

    def __init__(self, data: bytes) -> None:
        self._data = bytes(data)

    @property
    def size(self) -> int:
        return len(self._data)

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0:
            raise ValueError("offset and length must not be negative")
        return self._data[offset : offset + length]


#: Where the date came from, as shown alongside the acquisition date.
SOURCES: dict[str, str] = {
    "abif": "the ABIF trace’s run start (RUND and RUNT)",
    "fcs": "the FCS file’s acquisition start ($BEGINDATETIME, or $DATE and $BTIM)",
    "mzml": "the mzML run’s start time (startTimeStamp)",
    "jcamp-dx": "the JCAMP-DX spectrum’s date (##LONGDATE, or ##DATE and ##TIME)",
    "exif": "the image’s EXIF date taken (DateTimeOriginal)",
    "ome-tiff": "the OME-TIFF image’s acquisition date (AcquisitionDate)",
    "sam": "the earliest date of the alignments’ read groups (@RG DT)",
    "czi": "the CZI image’s acquisition time (AcquisitionDateAndTime)",
}

#: What a reader may read of a file in all, and in how many reads: each is a ranged GET.
_BUDGET = 4 * 1024 * 1024
_MAX_READS = 16
#: What's read first, in one read, for formats whose fields are near the start.
_HEAD = 256 * 1024
#: What's read at once anywhere else.
_WINDOW = 64 * 1024
_CHUNK = 1024 * 1024
_EARLIEST_YEAR = 1980
_LATEST_YEAR = 2200


class _UnreadableError(Exception):
    """The file doesn't clearly say: it's malformed, truncated or too big to read in bounds."""


class _SourceError(Exception):
    """Reading the source failed: not the file's fault, so it's raised, not None."""

    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


def _guard[T](read: Callable[[], T]) -> T | None:
    """What ``read`` reads, or None when the file doesn't clearly say: so one malformed
    part of a file leaves the others known. Reading the source failing is still raised."""
    try:
        return read()
    except _SourceError:
        raise
    except Exception:
        # struct, zlib, XML, value and index errors on a malformed file all mean the same.
        return None


class _Exact(Protocol):
    def exact(self, offset: int, length: int) -> bytes: ...


class _File:
    """A source, read within the budget. What :meth:`exact` reads is kept, a window at a
    time, so a format's small reads near each other (a TIFF's IFD and its values, at the
    end of a big file) are one ranged read, not one each."""

    def __init__(self, source: Readable) -> None:
        self._source = source
        self.size = max(0, source.size)
        self._spent = 0
        self._reads = 0
        self._windows: list[tuple[int, bytes]] = []

    def read(self, offset: int, length: int) -> bytes:
        """Up to ``length`` bytes at ``offset``: fewer at the end of the file."""
        if offset < 0 or length < 0:
            raise _UnreadableError
        length = min(length, self.size - offset)
        if length <= 0:
            return b""
        if self._spent + length > _BUDGET or self._reads >= _MAX_READS:
            raise _UnreadableError
        self._spent += length
        self._reads += 1
        try:
            data = self._source.read(offset, length)
        except Exception as error:
            raise _SourceError(error) from error
        return bytes(data[:length])

    def exact(self, offset: int, length: int) -> bytes:
        """Exactly ``length`` bytes at ``offset``, from a window already read when one has
        them. The first window is the file's head."""
        if offset < 0 or length < 0:
            raise _UnreadableError
        if not self._windows:
            self._windows.append((0, self.read(0, _HEAD)))
        for start, data in self._windows:
            if start <= offset and offset + length <= start + len(data):
                return data[offset - start : offset - start + length]
        data = self.read(offset, max(length, _WINDOW))
        if len(data) < length:
            raise _UnreadableError
        self._windows.append((offset, data))
        return data[:length]


class _BytesView:
    """Bytes already read, such as a JPEG's Exif segment, read as :class:`_File` is."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def exact(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > len(self._data):
            raise _UnreadableError
        return self._data[offset : offset + length]


# Values, checked.


def _acquired(
    source: str,
    year: int,
    month: int,
    day: int,
    clock: tuple[int, int, int] | None = None,
    fraction: str = "",
    offset: str | None = None,
) -> Acquired:
    """What the file says, once it's checked to be a real date and time."""
    if not _EARLIEST_YEAR <= year <= _LATEST_YEAR:
        raise _UnreadableError
    try:
        on = dt.date(year, month, day)
    except ValueError:
        raise _UnreadableError from None
    at = on.isoformat()
    if clock is None:
        if fraction or offset:
            raise _UnreadableError
        return Acquired(on=on, at=at, source=source)
    hour, minute, second = clock
    # 24:00:00 and leap seconds are valid in some formats, but rare enough that a file
    # giving one is more likely wrong than right.
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise _UnreadableError
    at += f"T{hour:02d}:{minute:02d}:{second:02d}"
    if fraction:
        if not (fraction.isdigit() and fraction.isascii() and len(fraction) <= 9):
            raise _UnreadableError
        at += "." + fraction
    return Acquired(on=on, at=at + (offset or ""), source=source)


# Every pattern is (?a), ASCII: \d alone also matches other scripts' digits, which int() takes.
_OFFSET = re.compile(r"(?a)([+-])(\d{2})(?::?(\d{2}))?")


def _offset(text: str) -> str | None:
    """An offset from UTC as ``Z`` or ``+01:00``, or None when there's none (blank, or
    RFC 3339's ``-00:00``, "unknown")."""
    if not text:
        return None
    if text in ("Z", "z"):
        return "Z"
    match = _OFFSET.fullmatch(text)
    if match is None:
        raise _UnreadableError
    sign, hours, minutes = match.group(1), int(match.group(2)), int(match.group(3) or 0)
    if minutes > 59 or hours * 60 + minutes > 14 * 60:
        raise _UnreadableError
    if sign == "-" and hours == minutes == 0:
        return None
    return f"{sign}{hours:02d}:{minutes:02d}"


_ISO = re.compile(
    r"(?a)(\d{4})-(\d{2})-(\d{2})"
    r"(?:T(\d{2}):(\d{2}):(\d{2})(?:[.,](\d+))?(Z|z|[+-]\d{2}(?::?\d{2})?)?)?"
)


def _iso(source: str, text: str, *, date_only: bool) -> Acquired:
    """An ISO 8601 date-time (extended format), or a date when ``date_only`` allows it."""
    match = _ISO.fullmatch(text.strip())
    if match is None:
        raise _UnreadableError
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    if hour is None:
        if not date_only:
            raise _UnreadableError
        return _acquired(source, int(year), int(month), int(day))
    return _acquired(
        source,
        int(year),
        int(month),
        int(day),
        (int(hour), int(minute), int(second)),
        fraction or "",
        _offset(offset or ""),
    )


# ABIF: Applied Biosystems' "Genetic Analysis Data File Format" (2009), which describes
# each tag read below in its appendix. Biopython's ABI parser quotes the same descriptions:
# https://github.com/biopython/biopython/blob/master/Bio/SeqIO/AbiIO.py

#: A directory entry: name, number, element type, element size, number of elements,
#: data size, data offset (or the data itself, when it's 4 bytes or fewer), data handle.
_ABIF_ENTRY = struct.Struct(">4sihhiiii")
_ABIF_MAX_ENTRIES = 65536
_ABIF_CHAR, _ABIF_DATE, _ABIF_TIME, _ABIF_PSTRING, _ABIF_CSTRING = 2, 10, 11, 18, 19
#: The longest of a string tag that's read: the facts are far shorter.
_ABIF_TEXT = 1024

#: An entry: its name, number, element type, element size, elements, data size, its
#: 4-byte data field and the data offset that field gives.
_AbifEntry = tuple[bytes, int, int, int, int, int, bytes, int]


def _abif_directory(file: _File) -> list[_AbifEntry]:
    header = file.exact(0, 6 + _ABIF_ENTRY.size)
    if header[:4] != b"ABIF":
        raise _UnreadableError
    _, _, _, size, count, _, offset, _ = _ABIF_ENTRY.unpack(header[6:])
    if size != _ABIF_ENTRY.size or not 0 < count <= _ABIF_MAX_ENTRIES:
        raise _UnreadableError
    directory = file.exact(offset, count * size)
    entries: list[_AbifEntry] = []
    for start in range(0, len(directory), size):
        entry = directory[start : start + size]
        name, number, kind, element, elements, length, at, _ = _ABIF_ENTRY.unpack(entry)
        # Data of 4 bytes or fewer is in the entry, where the offset would be.
        entries.append((name, number, kind, element, elements, length, entry[20:24], at))
    return entries


def _abif_date(entries: list[_AbifEntry]) -> Acquired:
    found: dict[bytes, bytes] = {}
    for name, number, kind, element, elements, length, data, _ in entries:
        # Number 1 is the run's start; 2 is its end.
        if name not in (b"RUND", b"RUNT") or number != 1 or name in found:
            continue
        wanted = _ABIF_DATE if name == b"RUND" else _ABIF_TIME
        if (kind, element, elements, length) != (wanted, 4, 1, 4):
            raise _UnreadableError
        found[name] = data
    if b"RUND" not in found:
        raise _UnreadableError
    year, month, day = struct.unpack(">HBB", found[b"RUND"])
    if b"RUNT" not in found:
        return _acquired("abif", year, month, day)
    hour, minute, second, hundredths = struct.unpack(">BBBB", found[b"RUNT"])
    if hundredths > 99:
        raise _UnreadableError
    return _acquired("abif", year, month, day, (hour, minute, second), f"{hundredths:02d}")


def _abif_text(file: _File, entries: list[_AbifEntry], name: bytes, number: int) -> str:
    """A string tag's text: a pString (its length, then its characters), a cString (to its
    NUL) or a char array. The tag given twice is no answer."""
    matching = [e for e in entries if e[0] == name and e[1] == number]
    if len(matching) != 1:
        raise _UnreadableError
    _, _, kind, element, elements, length, field, offset = matching[0]
    if (
        kind not in (_ABIF_CHAR, _ABIF_PSTRING, _ABIF_CSTRING)
        or element != 1
        or length != elements
        or not 0 < length <= _ABIF_TEXT
    ):
        raise _UnreadableError
    data = field[:length] if length <= 4 else file.exact(offset, length)
    if kind == _ABIF_PSTRING:
        if data[0] > len(data) - 1:
            raise _UnreadableError
        data = data[1 : 1 + data[0]]
    else:
        data = data.split(b"\x00", 1)[0]
    # ABIF's strings are ASCII; anything else isn't what the specification describes.
    return data.decode("ascii")


#: Where each ABIF fact is, in the order they're preferred.
_ABIF_FACTS = (
    ("instrument", b"HCFG", 3, "ABIF tag HCFG 3 (official instrument name)"),
    ("instrument", b"MODL", 1, "ABIF tag MODL 1 (model number)"),
    ("instrument_name", b"MCHN", 1, "ABIF tag MCHN 1 (instrument name and serial number)"),
    ("software", b"SVER", 1, "ABIF tag SVER 1 (data collection software version)"),
    ("protocol", b"RPrN", 1, "ABIF tag RPrN 1 (run protocol name)"),
)


def _abif_facts(file: _File, entries: list[_AbifEntry]) -> dict[str, Fact]:
    """The instrument (HCFG 3, the official name such as ``3730xl``, else MODL 1, the model
    number), the name it was given (MCHN 1, with its serial in it), the serial (HCFG 4's
    ``SerialNumber``), the data collection software's version (SVER 1) and the run protocol
    (RPrN 1). ABIF's ``User`` is who created the plate, not who ran it: not read."""
    facts = Facts()
    for key, name, number, stated_in in _ABIF_FACTS:
        text = _guard(functools.partial(_abif_text, file, entries, name, number))
        facts.add(key, text, stated_in)
    parameters = _guard(lambda: _abif_text(file, entries, b"HCFG", 4))
    if parameters is not None:
        # Key-value pairs separated by semicolons, such as "UnitID=3;SerialNumber=1404-021;".
        pairs = [part.partition("=") for part in parameters.split(";") if part.strip()]
        serials = {
            value.strip() for key, sep, value in pairs if sep and key.strip() == "SerialNumber"
        }
        if len(serials) == 1:
            facts.add(
                "serial", serials.pop(), "ABIF tag HCFG 4 (instrument parameters), SerialNumber"
            )
    return facts.found()


def _abif(file: _File) -> FileMetadata:
    entries = _abif_directory(file)
    return FileMetadata(
        acquired=_guard(lambda: _abif_date(entries)),
        facts=_guard(lambda: _abif_facts(file, entries)) or {},
    )


# FCS: ISAC's Flow Cytometry Standard, 2.0 to 3.2.

_FCS_VERSIONS = (b"FCS2.0", b"FCS3.0", b"FCS3.1", b"FCS3.2")
_FCS_HEADER = 58
_MONTHS = {
    name: number
    for number, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}
_FCS_DATE = re.compile(r"(?a)(\d{1,2})-([A-Za-z]{3})-(\d+)")
# FCS 3.x gives hundredths after a dot; FCS 2.0 sixtieths after a colon.
_FCS_TIME = re.compile(r"(?a)(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6})|:(\d{2}))?")


def _offset_field(field: bytes) -> int:
    text = field.strip(b" ")
    if not text.isdigit():
        raise _UnreadableError
    return int(text)


def _fcs_keywords(text: bytes) -> dict[str, list[str]]:
    """The TEXT segment's keywords (upper-cased) and their values.

    Its first byte is the delimiter. A doubled delimiter is the delimiter itself, inside
    a keyword or value."""
    if len(text) < 2:
        raise _UnreadableError
    delimiter = text[:1]
    escaped = re.escape(delimiter)
    # A token is what's up to the next delimiter that isn't doubled.
    token = re.compile(rb"((?:[^" + escaped + rb"]|" + escaped + escaped + rb")*)" + escaped)
    tokens: list[bytes] = []
    at = 1
    while at < len(text):
        match = token.match(text, at)
        if match is None:
            # Writers pad TEXT out to its stated end after the last delimiter (Sysmex's
            # CyFlow does, with spaces): padding is no token. Anything else is one.
            if text[at:].strip(b" \x00\r\n"):
                tokens.append(text[at:])
            break
        tokens.append(match.group(1).replace(delimiter * 2, delimiter))
        at = match.end()
    if len(tokens) % 2:
        # Keywords and values come in pairs; an odd one out means the TEXT isn't what
        # it seems, so no value in it can be trusted to be the one it looks like.
        raise _UnreadableError
    keywords: dict[str, list[str]] = {}
    for keyword, value in zip(tokens[::2], tokens[1::2], strict=True):
        name = keyword.decode("utf-8", "replace").strip().upper()
        keywords.setdefault(name, []).append(value.decode("utf-8", "replace").strip())
    return keywords


def _one(keywords: dict[str, list[str]], name: str) -> str | None:
    values = keywords.get(name)
    if not values:
        return None
    if len(set(values)) > 1:
        raise _UnreadableError
    return values[0]


def _fcs_date(keywords: dict[str, list[str]]) -> Acquired:
    begin = _one(keywords, "$BEGINDATETIME")
    if begin is not None:
        return _iso("fcs", begin, date_only=False)
    date = _one(keywords, "$DATE")
    if date is None:
        raise _UnreadableError
    match = _FCS_DATE.fullmatch(date)
    if match is None or len(match.group(3)) != 4 or match.group(2).upper() not in _MONTHS:
        raise _UnreadableError
    day, month, year = int(match.group(1)), _MONTHS[match.group(2).upper()], int(match.group(3))
    time = _one(keywords, "$BTIM")
    if time is None:
        return _acquired("fcs", year, month, day)
    clock = _FCS_TIME.fullmatch(time)
    if clock is None or (clock.group(5) is not None and int(clock.group(5)) > 59):
        raise _UnreadableError
    hour, minute, second = int(clock.group(1)), int(clock.group(2)), int(clock.group(3))
    return _acquired("fcs", year, month, day, (hour, minute, second), clock.group(4) or "")


def _fcs_facts(version: bytes, keywords: dict[str, list[str]]) -> dict[str, Fact]:
    """``$CYT`` (the type of flow cytometer), ``$CYTSN`` (its serial number, from FCS 3.0)
    and ``$OP`` (the name of its operator), as FCS 3.1's optional keywords define them
    (Spidlen et al., Cytometry A 77A:97, 2010, https://doi.org/10.1002/cyto.a.20825; FCS
    3.2 keeps them, https://doi.org/10.1002/cyto.a.24225)."""
    facts = Facts()
    named = [("instrument", "$CYT"), ("serial", "$CYTSN"), ("operator", "$OP")]
    for key, keyword in named:
        if keyword == "$CYTSN" and version == b"FCS2.0":
            continue
        value = _guard(functools.partial(_one, keywords, keyword))
        facts.add(key, value, f"FCS keyword {keyword}")
    return facts.found()


def _fcs(file: _File) -> FileMetadata:
    header = file.exact(0, _FCS_HEADER)
    if header[:6] not in _FCS_VERSIONS:
        raise _UnreadableError
    start, end = _offset_field(header[10:18]), _offset_field(header[18:26])
    if start < _FCS_HEADER or end < start or end - start + 1 > _BUDGET // 2:
        raise _UnreadableError
    keywords = _fcs_keywords(file.exact(start, end - start + 1))
    return FileMetadata(
        acquired=_guard(lambda: _fcs_date(keywords)),
        facts=_fcs_facts(header[:6], keywords),
    )


# mzML: HUPO-PSI's mass spectrometry format, and indexedmzML around it.

_MZML_READ = 1024 * 1024
_MZML_ROOT = re.compile(r"(?a)<(?:[\w.-]+:)?(?:indexedmzML|mzML)[\s>]")
_MZML_RUN = re.compile(r"(?a)<(?:[\w.-]+:)?run(?=[\s/>])")
# A start tag's rest, to its ">": quoted values may hold ">", so they're skipped whole.
_TAG_REST = re.compile(r"""(?a)(?:[^>"']|"[^"]*"|'[^']*')*>""")
_ATTRIBUTE = re.compile(r"""(?a)([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _mzml_date(text: str) -> Acquired:
    root = _MZML_ROOT.search(text)
    if root is None:
        raise _UnreadableError
    run = _MZML_RUN.search(text, root.end())
    if run is None:
        raise _UnreadableError
    tag = _TAG_REST.match(text, run.end())
    if tag is None:
        raise _UnreadableError
    for match in _ATTRIBUTE.finditer(tag.group()):
        if match.group(1) == "startTimeStamp":
            value = match.group(2) if match.group(2) is not None else match.group(3)
            return _iso("mzml", value, date_only=False)
    raise _UnreadableError


_MZML_NAMESPACE = "http://psi.hupo.org/ms/mzml"
#: What's kept of an mzML document's header: its software and instrument configurations,
#: and the parameter groups they refer to. Nothing in a run, which is where parsing ends.
_MZML_KEPT = {
    ("mzML",),
    ("mzML", "referenceableParamGroupList"),
    ("mzML", "referenceableParamGroupList", "referenceableParamGroup"),
    ("mzML", "referenceableParamGroupList", "referenceableParamGroup", "cvParam"),
    ("mzML", "softwareList"),
    ("mzML", "softwareList", "software"),
    ("mzML", "softwareList", "software", "cvParam"),
    ("mzML", "instrumentConfigurationList"),
    ("mzML", "instrumentConfigurationList", "instrumentConfiguration"),
    *(
        ("mzML", "instrumentConfigurationList", "instrumentConfiguration", child)
        for child in ("cvParam", "referenceableParamGroupRef", "softwareRef")
    ),
}
_INSTRUMENT_MODEL, _INSTRUMENT_SERIAL = "MS:1000031", "MS:1000529"


@functools.cache
def _psi_ms() -> dict[str, dict[str, list[str]]]:
    """The PSI-MS terms under instrument model and software, each with its parents
    there, as scripts/build_psi_ms_terms.py wrote them from a release of the vocabulary."""
    terms = json.loads(
        resources.files("iki_server.files").joinpath("psi_ms_terms.json").read_text()
    )
    return {"instrument_models": terms["instrument_models"], "software": terms["software"]}


def _most_specific(accessions: set[str], terms: dict[str, list[str]]) -> str:
    """The one term of those given that the others are broader than ("acquisition
    software" and "Xcalibur" are Xcalibur), or no answer when there isn't one."""
    broader: set[str] = set()
    for accession in accessions:
        stack = list(terms[accession])
        while stack:
            parent = stack.pop()
            if parent not in broader:
                broader.add(parent)
                stack.extend(terms.get(parent, ()))
    narrowest = accessions - broader
    if len(narrowest) != 1:
        raise _UnreadableError
    return narrowest.pop()


def _cv_params(element: Element, groups: dict[str, Element]) -> list[Element]:
    """An element's cvParams, its own and those of the parameter groups it refers to."""
    params = element.all("cvParam")
    for ref in element.all("referenceableParamGroupRef"):
        group = groups.get(ref.attributes.get("ref", ""))
        if group is None:
            raise _UnreadableError
        params += group.all("cvParam")
    return params


def _cv_named(params: list[Element], terms: dict[str, list[str]]) -> tuple[str, str]:
    """The most specific of the cvParams whose term is under ``terms``' root: its accession
    and the name the file gives it."""
    named: dict[str, set[str]] = {}
    for param in params:
        accession = param.attributes.get("accession", "")
        if accession in terms:
            named.setdefault(accession, set()).add(param.attributes.get("name", ""))
    accession = _most_specific(set(named), terms)
    if len(named[accession]) != 1:
        raise _UnreadableError
    return accession, named[accession].pop()


def _mzml_facts(head: bytes) -> dict[str, Fact]:
    """From the run's default instrument configuration (``defaultInstrumentConfigurationRef``,
    which mzML 1.1 requires): the instrument model, the cvParam whose term is under
    MS:1000031 ("instrument model"), or that term's own value; its serial number
    (MS:1000529, an "instrument attribute"); and the software its ``softwareRef`` names,
    with that software's version. mzML 1.1.0's schema and its CV mapping rules define each:
    https://github.com/HUPO-PSI/mzML/blob/master/schema/schema_1.1/mzML1.1.0.xsd,
    https://github.com/HUPO-PSI/mzML/blob/master/validator/src/main/resources/ms-mapping.xml.
    An mzML file's contact is whom to ask about it, not who ran the instrument: not read."""

    def keep(path: tuple[str, ...]) -> bool:
        return (path[1:] if path[0] == "indexedmzML" else path) in _MZML_KEPT

    def stop(path: tuple[str, ...], starting: bool) -> bool:
        return starting and (path[1:] if path[0] == "indexedmzML" else path) == ("mzML", "run")

    def keep_all(path: tuple[str, ...]) -> bool:
        return path == ("indexedmzML",) or keep(path)

    root, stopped = metadata.parse_xml([head], keep_all, stop=stop)
    if root is not None and root.name == "indexedmzML":
        root = root.one("mzML")
    if root is None or not stopped or root.namespace != _MZML_NAMESPACE:
        raise _UnreadableError
    if not root.attributes.get("version", "").startswith("1.1"):
        raise _UnreadableError
    run = root.one("run")
    configurations = (root.one("instrumentConfigurationList") or root).all(
        "instrumentConfiguration"
    )
    wanted = run.attributes.get("defaultInstrumentConfigurationRef") if run else None
    chosen = [c for c in configurations if c.attributes.get("id") == wanted]
    if len(chosen) != 1:
        raise _UnreadableError
    configuration = chosen[0]
    named = f"mzML instrument configuration “{wanted}”"
    groups: dict[str, Element] = {}
    for group in (root.one("referenceableParamGroupList") or root).all("referenceableParamGroup"):
        groups.setdefault(group.attributes.get("id", ""), group)
    params = _cv_params(configuration, groups)
    terms = _psi_ms()
    facts = Facts()

    def model() -> tuple[str, str]:
        accession, name = _cv_named(params, terms["instrument_models"])
        return accession, name

    found = _guard(model)
    if found is not None:
        facts.add("instrument", found[1], f"{named}, {found[0]}")
    else:
        # The term itself may be given, with the model as its value, for a model the
        # vocabulary doesn't have.
        stated = {
            p.attributes.get("value", "")
            for p in params
            if p.attributes.get("accession") == _INSTRUMENT_MODEL
        }
        if len(stated) == 1:
            facts.add("instrument", stated.pop(), f"{named}, {_INSTRUMENT_MODEL}")
    serials = {
        p.attributes.get("value", "")
        for p in params
        if p.attributes.get("accession") == _INSTRUMENT_SERIAL
    }
    if len(serials) == 1:
        facts.add("serial", serials.pop(), f"{named}, {_INSTRUMENT_SERIAL}")

    def software() -> str:
        refs = configuration.all("softwareRef")
        if len(refs) != 1:
            raise _UnreadableError
        ref = refs[0].attributes.get("ref", "")
        matching = [
            s
            for s in (root.one("softwareList") or root).all("software")
            if s.attributes.get("id") == ref
        ]
        if len(matching) != 1:
            raise _UnreadableError
        _, name = _cv_named(_cv_params(matching[0], groups), terms["software"])
        version = metadata.clean_value(matching[0].attributes.get("version", ""))
        return f"{name} {version}" if version else name

    facts.add("software", _guard(software), f"mzML software that {named} names")
    return facts.found()


def _mzml(file: _File) -> FileMetadata:
    head = file.read(0, _MZML_READ)
    return FileMetadata(
        acquired=_guard(lambda: _mzml_date(head.decode("utf-8", "replace"))),
        facts=_guard(lambda: _mzml_facts(head)) or {},
    )


# JCAMP-DX: IUPAC's spectra exchange format, 4.24 and 5.01.

_JCAMP_READ = 256 * 1024
_JCAMP_LABEL = re.compile(r"(?a)##([^=]*)=(.*)")
_JCAMP_LONGDATE = re.compile(
    r"(?a)(\d{4})/(\d{2})/(\d{2})"
    r"(?:\s+(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,4}))?(?:\s*([+-]\d{4}))?)?"
)
_JCAMP_DATE = re.compile(r"(?a)(\d{4})/(\d{2})/(\d{2})")
_JCAMP_TIME = re.compile(r"(?a)(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,4}))?")


def _jcamp_labels(text: str) -> dict[str, str]:
    """The first block's labels, as JCAMP-DX compares them: upper-cased, without spaces,
    dashes, slashes or underscores. Each label's first value; ``$$`` starts a comment."""
    labels: dict[str, str] = {}
    for line in text.splitlines():
        match = _JCAMP_LABEL.match(line.strip())
        if match is None:
            continue
        name = re.sub(r"(?a)[\s\-/_]", "", match.group(1)).upper()
        if not labels and name != "TITLE":
            # Every JCAMP-DX block starts with ##TITLE=.
            raise _UnreadableError
        if name == "END":
            break
        labels.setdefault(name, match.group(2).split("$$", 1)[0].strip())
    return labels


def _jcamp_date(labels: dict[str, str]) -> Acquired:
    if "LONGDATE" in labels:
        match = _JCAMP_LONGDATE.fullmatch(labels["LONGDATE"])
        if match is None:
            raise _UnreadableError
        year, month, day, hour, minute, second, fraction, offset = match.groups()
        if hour is None:
            return _acquired("jcamp-dx", int(year), int(month), int(day))
        return _acquired(
            "jcamp-dx",
            int(year),
            int(month),
            int(day),
            (int(hour), int(minute), int(second)),
            fraction or "",
            _offset(offset or ""),
        )
    if "DATE" not in labels:
        raise _UnreadableError
    date = _JCAMP_DATE.fullmatch(labels["DATE"])
    if date is None:
        raise _UnreadableError
    year, month, day = (int(group) for group in date.groups())
    if "TIME" not in labels:
        return _acquired("jcamp-dx", year, month, day)
    time = _JCAMP_TIME.fullmatch(labels["TIME"])
    if time is None:
        raise _UnreadableError
    clock = (int(time.group(1)), int(time.group(2)), int(time.group(3)))
    return _acquired("jcamp-dx", year, month, day, clock, time.group(4) or "")


def _jcamp(file: _File) -> FileMetadata:
    """The date, and the spectrometer: ``##SPECTROMETER/DATA SYSTEM``, which JCAMP-DX 4.24
    defines as the spectrometer's and data system's manufacturer and model (McDonald and
    Wilks, Applied Spectroscopy 42:151, 1988, https://doi.org/10.1366/0003702884428734).
    JCAMP-DX defines no operator: ##OWNER is whose the data is."""
    labels = _jcamp_labels(file.read(0, _JCAMP_READ).decode("latin-1"))
    facts = Facts()
    spectrometer = labels.get("SPECTROMETERDATASYSTEM")
    # JCAMP-DX is ASCII: other bytes, read as Latin-1, could be anything.
    if spectrometer is not None and spectrometer.isascii():
        facts.add("instrument", spectrometer, "JCAMP-DX ##SPECTROMETER/DATA SYSTEM")
    return FileMetadata(acquired=_guard(lambda: _jcamp_date(labels)), facts=facts.found())


# TIFF, JPEG and EXIF; OME-TIFF.

_TIFF_MAX_ENTRIES = 1024
_TIFF_ASCII, _TIFF_UNDEFINED, _TIFF_UTF8 = 2, 7, 129
_TIFF_LONG, _TIFF_IFD = 4, 13
_IMAGE_DESCRIPTION = 0x010E
_EXIF_IFD = 0x8769
_DATE_TIME_ORIGINAL = 0x9003
_OFFSET_TIME_ORIGINAL = 0x9011
_SUB_SEC_TIME_ORIGINAL = 0x9291
#: How much of an ImageDescription is read for its OME-XML.
_OME_READ = 2 * 1024 * 1024
_OME_NAMESPACE = "openmicroscopy.org/Schemas/OME/"
_OME_ACQUISITION = re.compile(
    r"(?a)<(?:[\w.-]+:)?AcquisitionDate\s*>([^<]*)</(?:[\w.-]+:)?AcquisitionDate\s*>"
)
_EXIF_DATE = re.compile(r"(?a)(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})")

#: A tag's type, count and its 4-byte value or offset.
_Entry = tuple[int, int, bytes]


def _ifd(view: _Exact, order: str, offset: int) -> dict[int, _Entry]:
    (count,) = struct.unpack(order + "H", view.exact(offset, 2))
    if count > _TIFF_MAX_ENTRIES:
        raise _UnreadableError
    data = view.exact(offset + 2, count * 12)
    entries: dict[int, _Entry] = {}
    for start in range(0, len(data), 12):
        tag, kind, number = struct.unpack(order + "HHI", data[start : start + 8])
        entries.setdefault(tag, (kind, number, data[start + 8 : start + 12]))
    return entries


def _text(view: _Exact, order: str, entry: _Entry, limit: int) -> str:
    """An ASCII (or UTF-8) tag's value to its first NUL, without trailing spaces. At most
    ``limit`` bytes of it are read."""
    kind, count, value = entry
    if kind not in (_TIFF_ASCII, _TIFF_UNDEFINED, _TIFF_UTF8):
        raise _UnreadableError
    if count <= 4:
        data = value[:count]
    else:
        (offset,) = struct.unpack(order + "I", value)
        data = view.exact(offset, min(count, limit))
    return data.split(b"\x00", 1)[0].decode("utf-8", "replace").rstrip(" ")


def _tiff_date(view: _Exact, order: str, ifd0: dict[int, _Entry]) -> Acquired:
    if _IMAGE_DESCRIPTION in ifd0:
        description = _text(view, order, ifd0[_IMAGE_DESCRIPTION], _OME_READ)
        if _OME_NAMESPACE in description:
            match = _OME_ACQUISITION.search(description)
            if match is not None:
                return _iso("ome-tiff", match.group(1), date_only=False)
    # IFD0's DateTime (0x0132) is when the file was last changed, so it's never read.
    pointer = ifd0.get(_EXIF_IFD)
    if pointer is None or pointer[0] not in (_TIFF_LONG, _TIFF_IFD) or pointer[1] != 1:
        raise _UnreadableError
    exif = _ifd(view, order, struct.unpack(order + "I", pointer[2])[0])
    if _DATE_TIME_ORIGINAL not in exif:
        raise _UnreadableError
    # Unknown parts are blanks, so a blank or zero date is no date.
    match = _EXIF_DATE.fullmatch(_text(view, order, exif[_DATE_TIME_ORIGINAL], 64))
    if match is None:
        raise _UnreadableError
    year, month, day, hour, minute, second = (int(group) for group in match.groups())
    fraction = ""
    if _SUB_SEC_TIME_ORIGINAL in exif:
        fraction = _text(view, order, exif[_SUB_SEC_TIME_ORIGINAL], 64).strip(" ")
    offset = None
    if _OFFSET_TIME_ORIGINAL in exif:
        stated = _text(view, order, exif[_OFFSET_TIME_ORIGINAL], 64).strip(" ")
        offset = None if stated in ("", ":") else _offset(stated)
    return _acquired("exif", year, month, day, (hour, minute, second), fraction, offset)


#: What's kept of OME-XML: the images' references, and what they refer to.
_OME_KEPT = {
    ("OME",),
    ("OME", "Image"),
    ("OME", "Image", "InstrumentRef"),
    ("OME", "Image", "ExperimenterRef"),
    ("OME", "Instrument"),
    ("OME", "Instrument", "Microscope"),
    ("OME", "Experimenter"),
}


def _by_id(elements: list[Element], wanted: str | None, key: str = "ID") -> Element:
    """The one element whose ``key`` is ``wanted``."""
    found = [e for e in elements if wanted and e.attributes.get(key) == wanted]
    if len(found) != 1:
        raise _UnreadableError
    return found[0]


def _person(names: list[str | None]) -> str | None:
    given = [name.strip() for name in names if name and name.strip()]
    return " ".join(given) if given else None


def _ome_facts(description: str) -> dict[str, Fact]:
    """The first Image's microscope, through its InstrumentRef: the Microscope's Model,
    Manufacturer and SerialNumber; and its operator, through its ExperimenterRef: the
    Experimenter's names, or else their UserName. The OME schema (2016-06) defines an
    Experimenter as "a person who performed an imaging experiment":
    https://www.openmicroscopy.org/Schemas/Documentation/Generated/OME-2016-06/ome.html"""
    # The schema puts Experimenters and Instruments before Images, so parsing can end with
    # the first Image: a plate's OME-XML can be longer than what's read of it.
    root, _ = metadata.parse_xml(
        [description.encode("utf-8")],
        lambda path: path in _OME_KEPT,
        stop=lambda path, starting: not starting and path == ("OME", "Image"),
    )
    if root is None or root.name != "OME" or _OME_NAMESPACE not in root.namespace:
        raise _UnreadableError
    images = root.all("Image")
    if not images:
        raise _UnreadableError
    image = images[0]
    facts = Facts()

    def microscope() -> Element:
        ref = image.one("InstrumentRef")
        instrument = _by_id(root.all("Instrument"), ref.attributes.get("ID") if ref else None)
        found = instrument.one("Microscope")
        if found is None:
            raise _UnreadableError
        return found

    scope = _guard(microscope)
    if scope is not None:
        for key, attribute in (
            ("instrument", "Model"),
            ("manufacturer", "Manufacturer"),
            ("serial", "SerialNumber"),
        ):
            facts.add(key, scope.attributes.get(attribute), f"OME-XML Microscope {attribute}")

    def experimenter() -> Element:
        ref = image.one("ExperimenterRef")
        return _by_id(root.all("Experimenter"), ref.attributes.get("ID") if ref else None)

    person = _guard(experimenter)
    if person is not None:
        names = [person.attributes.get(n) for n in ("FirstName", "MiddleName", "LastName")]
        if _person(names) is not None:
            facts.add("operator", _person(names), "OME-XML Experimenter name")
        else:
            facts.add(
                "operator", person.attributes.get("UserName"), "OME-XML Experimenter UserName"
            )
    return facts.found()


def _tiff(view: _Exact) -> FileMetadata:
    header = view.exact(0, 8)
    if header[:4] == b"II*\x00":
        order = "<"
    elif header[:4] == b"MM\x00*":
        order = ">"
    else:
        # BigTIFF (43) and anything else.
        raise _UnreadableError
    (first,) = struct.unpack(order + "I", header[4:8])
    ifd0 = _ifd(view, order, first)
    facts: dict[str, Fact] = {}
    if _IMAGE_DESCRIPTION in ifd0:
        description = _guard(lambda: _text(view, order, ifd0[_IMAGE_DESCRIPTION], _OME_READ))
        if description is not None and _OME_NAMESPACE in description:
            facts = _guard(lambda: _ome_facts(description)) or {}
    return FileMetadata(acquired=_guard(lambda: _tiff_date(view, order, ifd0)), facts=facts)


_JPEG_MAX_SEGMENTS = 64
_JPEG_STANDALONE = {0x01, *range(0xD0, 0xD8)}
_JPEG_SOS, _JPEG_EOI, _JPEG_APP1 = 0xDA, 0xD9, 0xE1


def _jpeg(file: _File) -> FileMetadata:
    position = 2
    for _ in range(_JPEG_MAX_SEGMENTS):
        marker = file.exact(position, 4)
        if marker[0] != 0xFF:
            raise _UnreadableError
        kind = marker[1]
        if kind == 0xFF:
            position += 1
            continue
        if kind in _JPEG_STANDALONE:
            position += 2
            continue
        if kind in (_JPEG_SOS, _JPEG_EOI):
            # The image data starts: EXIF comes before it or not at all.
            raise _UnreadableError
        (length,) = struct.unpack(">H", marker[2:4])
        if length < 2:
            raise _UnreadableError
        if kind == _JPEG_APP1 and length >= 8:
            body = file.exact(position + 4, length - 2)
            # Others (XMP's, for one) are APP1 too.
            if body.startswith(b"Exif\x00"):
                return _tiff(_BytesView(body[6:]))
        position += 2 + length
    raise _UnreadableError


def _image(file: _File) -> FileMetadata:
    magic = file.exact(0, 4)
    if magic[:2] in (b"II", b"MM"):
        return _tiff(file)
    if magic[:2] == b"\xff\xd8":
        return _jpeg(file)
    raise _UnreadableError


# SAM and BAM: the SAM/BAM Format Specification.

_GZIP = b"\x1f\x8b"
_BAM_MAGIC = b"BAM\x01"
#: How much of a BAM's start is decompressed for its header.
_BAM_INFLATED = 4 * 1024 * 1024
_HAS_OFFSET = re.compile(r"(?a)(?:Z|[+-]\d{2}:\d{2})$")


def _sam_header(file: _File, first: bytes) -> list[bytes]:
    """A SAM file's header lines: those that start with ``@``, up to the first that doesn't."""
    lines: list[bytes] = []
    buffer, offset = first, len(first)
    while True:
        *complete, buffer = buffer.split(b"\n")
        for line in complete:
            if not line.startswith(b"@"):
                return lines
            lines.append(line)
        if buffer and not buffer.startswith(b"@"):
            return lines
        if offset >= file.size:
            if buffer:
                lines.append(buffer)
            return lines
        chunk = file.read(offset, _CHUNK)
        if not chunk:
            raise _UnreadableError
        buffer += chunk
        offset += len(chunk)


def _bam_header(file: _File, first: bytes) -> list[bytes]:
    """A BAM file's header text's lines. BGZF is gzip members one after another, so each
    is decompressed in turn until the header's text is all there."""
    inflated = bytearray()
    data, offset = first, len(first)
    inflater = zlib.decompressobj(31)
    while True:
        if len(inflated) >= 8:
            if inflated[:4] != _BAM_MAGIC:
                raise _UnreadableError
            (length,) = struct.unpack("<i", inflated[4:8])
            if length < 0 or 8 + length > _BAM_INFLATED:
                raise _UnreadableError
            if len(inflated) >= 8 + length:
                return bytes(inflated[8 : 8 + length]).split(b"\x00", 1)[0].split(b"\n")
        if not data:
            if offset >= file.size:
                raise _UnreadableError
            data = file.read(offset, _CHUNK)
            if not data:
                raise _UnreadableError
            offset += len(data)
        # Bounded, so a member that inflates to far more than it should can't.
        inflated += inflater.decompress(data, _BAM_INFLATED + 1 - len(inflated))
        if len(inflated) > _BAM_INFLATED:
            raise _UnreadableError
        if inflater.eof:
            data = inflater.unused_data
            inflater = zlib.decompressobj(31)
        else:
            data = inflater.unconsumed_tail


def _instant(acquired: Acquired) -> dt.datetime:
    match = _ISO.fullmatch(acquired.at)
    if match is None or match.group(4) is None:
        raise _UnreadableError
    year, month, day, hour, minute, second = (int(group) for group in match.groups()[:6])
    micro = int((match.group(7) or "").ljust(6, "0")[:6])
    offset = match.group(8)
    zone = dt.UTC
    if offset != "Z":
        minutes = int(offset[1:3]) * 60 + int(offset[4:6])
        zone = dt.timezone(dt.timedelta(minutes=-minutes if offset[0] == "-" else minutes))
    return dt.datetime(year, month, day, hour, minute, second, micro, tzinfo=zone)


def _sam_date(lines: list[bytes]) -> Acquired:
    dates: list[Acquired] = []
    for line in lines:
        fields = line.decode("utf-8", "replace").rstrip("\r").split("\t")
        if fields[0] != "@RG":
            continue
        stated = [field[3:] for field in fields[1:] if field.startswith("DT:")]
        if len(stated) > 1:
            raise _UnreadableError
        if stated:
            dates.append(_iso("sam", stated[0], date_only=True))
    if not dates:
        raise _UnreadableError
    if all(_HAS_OFFSET.search(date.at) for date in dates):
        return min(dates, key=_instant)
    # Without offsets for all, the times given are compared as they are: wall-clock times,
    # which the extended format orders as strings do.
    return min(dates, key=lambda date: _HAS_OFFSET.sub("", date.at))


_SAM_TAG = re.compile(r"(?a)[A-Za-z][A-Za-z0-9]")


def _sam_read_groups(lines: list[bytes]) -> list[dict[str, str]]:
    """Each ``@RG`` line's standard tags (SAM/BAM Format Specification, section 1.3:
    https://samtools.github.io/hts-specs/SAMv1.pdf), all of them or none
    (:func:`metadata.read_groups`). A header that isn't UTF-8, as the specification
    requires, or a field that isn't ``TAG:VALUE``, makes it none."""
    groups: list[dict[str, str] | None] = []
    for line in lines:
        fields = line.decode("utf-8").rstrip("\r").split("\t")
        if fields[0] != "@RG":
            continue
        tags: list[tuple[str, str]] = []
        for field_ in fields[1:]:
            if len(field_) < 4 or field_[2] != ":" or not _SAM_TAG.fullmatch(field_[:2]):
                raise _UnreadableError
            tags.append((field_[:2], field_[3:]))
        groups.append(metadata.read_group(tags))
    return metadata.read_groups(groups)


def _sam(file: _File) -> FileMetadata:
    first = file.read(0, _CHUNK)
    lines = _bam_header(file, first) if first.startswith(_GZIP) else _sam_header(file, first)
    return FileMetadata(
        acquired=_guard(lambda: _sam_date(lines)),
        read_groups=tuple(_guard(lambda: _sam_read_groups(lines)) or ()),
    )


# CZI: ZEISS's ZISRAW file format. Its segments are as libCZI, ZEISS's reference reader,
# declares them: https://github.com/ZEISS/libczi/blob/main/Src/libCZI/CziStructs.h

_CZI_FILE = b"ZISRAWFILE".ljust(16, b"\x00")
_CZI_METADATA = b"ZISRAWMETADATA".ljust(16, b"\x00")
#: A segment's header (its ID, allocated and used sizes), then the file header's data,
#: in which MetadataPosition is at 60; and the metadata segment's fixed data.
_CZI_HEADER, _CZI_METADATA_POSITION, _CZI_METADATA_DATA = 32, 60, 256
#: What's read of the metadata's XML: its Information comes first, and ends the reading.
_CZI_XML = 3 * 1024 * 1024
_CZI_READ = 512 * 1024
_CZI_INFORMATION = ("ImageDocument", "Metadata", "Information")
_CZI_KEPT = {
    (),
    ("User",),
    *(("User", name) for name in ("DisplayName", "FirstName", "LastName", "UserName")),
    ("Image",),
    ("Image", "AcquisitionDateAndTime"),
    ("Image", "MicroscopeRef"),
    ("Instrument",),
    ("Instrument", "Microscopes"),
    ("Instrument", "Microscopes", "Microscope"),
    ("Instrument", "Microscopes", "Microscope", "System"),
    ("Instrument", "Microscopes", "Microscope", "Manufacturer"),
    *(
        ("Instrument", "Microscopes", "Microscope", "Manufacturer", name)
        for name in ("Manufacturer", "Model", "SerialNumber")
    ),
}


def _czi_xml(file: _File) -> Element:
    header = file.exact(0, _CZI_HEADER + _CZI_METADATA_POSITION + 8)
    if header[:16] != _CZI_FILE:
        raise _UnreadableError
    at = _CZI_HEADER + _CZI_METADATA_POSITION
    (position,) = struct.unpack("<q", header[at : at + 8])
    if not _CZI_HEADER <= position <= file.size - _CZI_HEADER - _CZI_METADATA_DATA:
        raise _UnreadableError
    segment = file.exact(position, _CZI_HEADER + 8)
    allocated, used = struct.unpack("<qq", segment[16:32])
    xml_size, _ = struct.unpack("<ii", segment[32:40])
    # A used size of 0 means the allocated size is all used.
    room = (used or allocated) - _CZI_METADATA_DATA
    start = position + _CZI_HEADER + _CZI_METADATA_DATA
    if segment[:16] != _CZI_METADATA or not 0 < xml_size <= room or start + xml_size > file.size:
        raise _UnreadableError
    end = start + min(xml_size, _CZI_XML)

    def chunks() -> Iterator[bytes]:
        at = start
        while at < end:
            data = file.read(at, min(_CZI_READ, end - at))
            if not data:
                raise _UnreadableError
            yield data
            at += len(data)

    def keep(path: tuple[str, ...]) -> bool:
        if len(path) <= len(_CZI_INFORMATION):
            return path == _CZI_INFORMATION[: len(path)]
        return path[:3] == _CZI_INFORMATION and path[3:] in _CZI_KEPT

    root, _ = metadata.parse_xml(
        chunks(), keep, stop=lambda path, starting: not starting and path == _CZI_INFORMATION
    )
    information = root.one("Metadata") if root is not None else None
    information = information.one("Information") if information is not None else None
    if root is None or root.name != "ImageDocument" or information is None:
        raise _UnreadableError
    return information


def _czi_date(information: Element) -> Acquired:
    image = information.one("Image")
    stated = image.text_of("AcquisitionDateAndTime") if image is not None else None
    if stated is None:
        raise _UnreadableError
    return _iso("czi", stated, date_only=False)


def _czi_facts(information: Element) -> dict[str, Fact]:
    """The microscope the image refers to (Image/MicroscopeRef): its Manufacturer's Model,
    else its Name, else its System, and its Manufacturer's Manufacturer and SerialNumber;
    and the operator, Information/User: its DisplayName, else its names, else its
    UserName. Bio-Formats, the OME reader, makes that User the image's Experimenter, the
    person who performed it. Document/UserName is whoever saved the document last, and the
    Application what saved it: neither is read."""
    facts = Facts()
    image = information.one("Image")
    ref = image.one("MicroscopeRef") if image is not None else None
    microscopes = information.one("Instrument")
    microscopes = microscopes.one("Microscopes") if microscopes is not None else None
    microscope = _guard(
        lambda: _by_id(
            microscopes.all("Microscope") if microscopes else [],
            ref.attributes.get("Id") if ref else None,
            "Id",
        )
    )
    if microscope is not None:
        maker = microscope.one("Manufacturer")
        if maker is not None:
            facts.add("instrument", maker.text_of("Model"), "CZI Microscope Manufacturer/Model")
        facts.add("instrument", microscope.attributes.get("Name"), "CZI Microscope Name")
        facts.add("instrument", microscope.text_of("System"), "CZI Microscope System")
        if maker is not None:
            facts.add("manufacturer", maker.text_of("Manufacturer"), "CZI Microscope Manufacturer")
            facts.add("serial", maker.text_of("SerialNumber"), "CZI Microscope SerialNumber")
    user = information.one("User")
    if user is not None:
        facts.add("operator", user.text_of("DisplayName"), "CZI Information/User DisplayName")
        names = _person([user.text_of("FirstName"), user.text_of("LastName")])
        facts.add("operator", names, "CZI Information/User FirstName and LastName")
        facts.add("operator", user.text_of("UserName"), "CZI Information/User UserName")
    return facts.found()


def _czi(file: _File) -> FileMetadata:
    information = _czi_xml(file)
    return FileMetadata(
        acquired=_guard(lambda: _czi_date(information)),
        facts=_guard(lambda: _czi_facts(information)) or {},
    )


# A plate reader's CSV export. BMG LABTECH's MARS writes the run's header above the data
# when it exports a plate (MARS manual, sections 3.2 and 3.4:
# https://medicine.umich.edu/sites/default/files/PHERAstar_data%20analysis.pdf): first
# "User: …,Path: …,Test run no.: …", then "Test name: …,Date: …,Time: …". A test is MARS's
# measurement protocol, and its user who ran it.

_CSV_READ = 16 * 1024
_CSV_DELIMITERS = (",", ";", "\t")


def _mars_header(lines: list[str]) -> dict[str, str] | None:
    """The MARS header's labelled cells, from the file's first two lines, or None when they
    aren't one. Its date is in the PC's regional format (03/03/2016 could be either month
    first or day first), so it's never read."""
    for delimiter in _CSV_DELIMITERS:
        try:
            rows = list(csv.reader(lines, delimiter=delimiter, strict=True))
        except csv.Error:
            continue
        if len(rows) != 2 or not rows[0] or not rows[1]:
            continue
        cells: dict[str, list[str]] = {}
        for row in rows:
            for cell in row:
                label, colon, value = cell.partition(":")
                if colon:
                    cells.setdefault(label.strip(), []).append(value)
        if (
            rows[0][0].startswith("User:")
            and rows[1][0].startswith("Test name:")
            and "Test run no." in cells
        ):
            return {label: values[0] for label, values in cells.items() if len(values) == 1}
    return None


def _csv(file: _File) -> FileMetadata:
    raw = b"\n".join(file.read(0, _CSV_READ).split(b"\n", 2)[:2])
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # MARS on Windows writes Windows-1252.
        text = raw.decode("cp1252")
    header = _mars_header([line.rstrip("\r") for line in text.split("\n")])
    facts = Facts()
    if header is not None:
        facts.add("operator", header.get("User"), "BMG MARS export header, User")
        facts.add("protocol", header.get("Test name"), "BMG MARS export header, Test name")
    return FileMetadata(facts=facts.found())


@dataclass(frozen=True)
class FileMetadata:
    """What a reader read of the file itself: when its data was acquired, its facts
    (:mod:`iki_server.files.metadata`) and a SAM or BAM file's read groups."""

    acquired: Acquired | None = None
    facts: Mapping[str, Fact] = field(default_factory=dict)
    read_groups: tuple[Mapping[str, str], ...] = ()


Reader = Callable[[_File], FileMetadata]


# Trusted implementations named by format declarations. File extensions and media types
# belong to those declarations. A reader never imports or executes code from a plugin.
READERS: dict[str, Reader] = {
    "abif": _abif,
    "fcs": _fcs,
    "mzml": _mzml,
    "jcamp-dx": _jcamp,
    "image": _image,
    "sam": _sam,
    "czi": _czi,
    "csv": _csv,
}


def read_metadata(reader: str | None, source: Readable) -> FileMetadata:
    """Use the format's registered reader. Malformed bytes yield unknown metadata.
    A storage error propagates so reading the object can be retried."""
    if not reader or not source.size:
        return FileMetadata()
    read_file = READERS[reader]
    try:
        return read_file(_File(source))
    except _SourceError as failure:
        raise failure.error from None
    except Exception:
        # Whatever the bytes are, they don't clearly say: struct, zlib, value and index
        # errors on a malformed file all mean the same.
        return FileMetadata()


def read(name: str, media_type: str, source: Readable) -> Acquired | None:
    """Read the date for an existing file, by its original name or stored media type."""
    from iki_server.entities import registry

    formats = registry.file_formats()
    named = next((fmt for fmt in formats.by_extension(name) if fmt.reader), None)
    fmt = named or formats.by_media_type(media_type)
    return read_metadata(fmt.reader if fmt else None, source).acquired
