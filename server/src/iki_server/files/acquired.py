"""When a file's data was acquired, as the file itself says (ADR 0177).

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

import datetime as dt
import re
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol


@dataclass(frozen=True)
class Acquired:
    """When a file's data was acquired, as the file says."""

    #: The date, as the file gives it: the instrument's local date.
    on: dt.date
    #: ISO 8601 as the file gives it, no more precise than it is: ``2026-03-14``,
    #: ``2026-03-14T09:21:07``, ``2026-03-14T09:21:07.25`` or ``2026-03-14T09:21:07+01:00``.
    at: str
    #: The reader's key in :data:`SOURCES`.
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


#: Each reader's key, and what it reads, as people would say it.
SOURCES: dict[str, str] = {
    "abif": "the ABIF trace’s run start (RUND and RUNT)",
    "fcs": "the FCS file’s acquisition start ($BEGINDATETIME, or $DATE and $BTIM)",
    "mzml": "the mzML run’s start time (startTimeStamp)",
    "jcamp-dx": "the JCAMP-DX spectrum’s date (##LONGDATE, or ##DATE and ##TIME)",
    "exif": "the image’s EXIF date taken (DateTimeOriginal)",
    "ome-tiff": "the OME-TIFF image’s acquisition date (AcquisitionDate)",
    "sam": "the earliest date of the alignments’ read groups (@RG DT)",
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


# ABIF: Applied Biosystems' Genetic Analysis Data File Format.

#: A directory entry: name, number, element type, element size, number of elements,
#: data size, data offset (or the data itself, when it's 4 bytes or fewer), data handle.
_ABIF_ENTRY = struct.Struct(">4sihhiiii")
_ABIF_MAX_ENTRIES = 65536
_ABIF_DATE = 10
_ABIF_TIME = 11


def _abif(file: _File) -> Acquired:
    header = file.exact(0, 6 + _ABIF_ENTRY.size)
    if header[:4] != b"ABIF":
        raise _UnreadableError
    _, _, _, size, count, _, offset, _ = _ABIF_ENTRY.unpack(header[6:])
    if size != _ABIF_ENTRY.size or not 0 < count <= _ABIF_MAX_ENTRIES:
        raise _UnreadableError
    directory = file.exact(offset, count * size)
    found: dict[bytes, bytes] = {}
    for start in range(0, len(directory), size):
        entry = directory[start : start + size]
        name, number, kind, element, elements, length, _, _ = _ABIF_ENTRY.unpack(entry)
        # Number 1 is the run's start; 2 is its end.
        if name not in (b"RUND", b"RUNT") or number != 1 or name in found:
            continue
        wanted = _ABIF_DATE if name == b"RUND" else _ABIF_TIME
        if (kind, element, elements, length) != (wanted, 4, 1, 4):
            raise _UnreadableError
        # Data of 4 bytes or fewer is in the entry, where the offset would be.
        found[name] = entry[20:24]
    if b"RUND" not in found:
        raise _UnreadableError
    year, month, day = struct.unpack(">HBB", found[b"RUND"])
    if b"RUNT" not in found:
        return _acquired("abif", year, month, day)
    hour, minute, second, hundredths = struct.unpack(">BBBB", found[b"RUNT"])
    if hundredths > 99:
        raise _UnreadableError
    return _acquired("abif", year, month, day, (hour, minute, second), f"{hundredths:02d}")


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


def _fcs(file: _File) -> Acquired:
    header = file.exact(0, _FCS_HEADER)
    if header[:6] not in _FCS_VERSIONS:
        raise _UnreadableError
    start, end = _offset_field(header[10:18]), _offset_field(header[18:26])
    if start < _FCS_HEADER or end < start or end - start + 1 > _BUDGET // 2:
        raise _UnreadableError
    keywords = _fcs_keywords(file.exact(start, end - start + 1))
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


# mzML: HUPO-PSI's mass spectrometry format, and indexedmzML around it.

_MZML_READ = 1024 * 1024
_MZML_ROOT = re.compile(r"(?a)<(?:[\w.-]+:)?(?:indexedmzML|mzML)[\s>]")
_MZML_RUN = re.compile(r"(?a)<(?:[\w.-]+:)?run(?=[\s/>])")
# A start tag's rest, to its ">": quoted values may hold ">", so they're skipped whole.
_TAG_REST = re.compile(r"""(?a)(?:[^>"']|"[^"]*"|'[^']*')*>""")
_ATTRIBUTE = re.compile(r"""(?a)([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _mzml(file: _File) -> Acquired:
    text = file.read(0, _MZML_READ).decode("utf-8", "replace")
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


def _jcamp(file: _File) -> Acquired:
    labels = _jcamp_labels(file.read(0, _JCAMP_READ).decode("latin-1"))
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


def _tiff(view: _Exact) -> Acquired:
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


_JPEG_MAX_SEGMENTS = 64
_JPEG_STANDALONE = {0x01, *range(0xD0, 0xD8)}
_JPEG_SOS, _JPEG_EOI, _JPEG_APP1 = 0xDA, 0xD9, 0xE1


def _jpeg(file: _File) -> Acquired:
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


def _image(file: _File) -> Acquired:
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


def _sam(file: _File) -> Acquired:
    first = file.read(0, _CHUNK)
    lines = _bam_header(file, first) if first.startswith(_GZIP) else _sam_header(file, first)
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


# Which reader reads a file.

_Reader = Callable[[_File], Acquired]

_BY_EXTENSION: dict[str, _Reader] = {
    ".ab1": _abif,
    ".abi": _abif,
    ".fsa": _abif,
    ".fcs": _fcs,
    ".lmd": _fcs,
    ".mzml": _mzml,
    ".jdx": _jcamp,
    ".dx": _jcamp,
    ".jcamp": _jcamp,
    ".tif": _image,
    ".tiff": _image,
    ".jpg": _image,
    ".jpeg": _image,
    ".sam": _sam,
    ".bam": _sam,
}

_BY_MEDIA_TYPE: dict[str, _Reader] = {
    "application/x-abi": _abif,
    "application/vnd.isac.fcs": _fcs,
    "application/x-mzml": _mzml,
    "chemical/x-jcamp-dx": _jcamp,
    "image/tiff": _image,
    "image/jpeg": _image,
    "text/x-sam": _sam,
    "application/x-bam": _sam,
}


def read(name: str, media_type: str, source: Readable) -> Acquired | None:
    """When the file's data was acquired, as the file says, or None.

    Picks a reader by the file's extension (case-insensitive), else by its media type.
    Never raises for what's in the file: a malformed or truncated file is None. An error
    reading ``source`` itself is raised as it was."""
    reader = _BY_EXTENSION.get(PurePosixPath(name).suffix.lower())
    if reader is None:
        reader = _BY_MEDIA_TYPE.get(media_type.split(";", 1)[0].strip().lower())
    if reader is None:
        return None
    file = _File(source)
    try:
        return reader(file)
    except _SourceError as failure:
        raise failure.error from None
    except Exception:
        # Whatever the bytes are, they don't clearly say: struct, zlib, value and index
        # errors on a malformed file all mean the same.
        return None
