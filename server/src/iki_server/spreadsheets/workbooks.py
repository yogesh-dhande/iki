"""Reading a workbook someone uploaded: an XLSX workbook's cell values, or a CSV file's
(ADR 0178).

Only values are read. A formula's value is the one the workbook saved when it was last
calculated: nothing is calculated here, and nothing in a workbook is ever run (macros
included). Workbooks are untrusted, so before anything parses one:

- Its first bytes say what it is. An encrypted workbook, or a legacy ``.xls`` one, is an
  OLE compound file, not a zip, and is refused with what to do instead.
- Its zip is read through once, every part decompressed with a running count, so a zip
  bomb stops at :data:`MAX_UNPACKED` bytes, or at :data:`MAX_RATIO` times a part's
  compressed size, whatever its headers claim. Encrypted parts, and compression methods
  a workbook doesn't use, are refused.
- Its XML declares no document type: OOXML never does, and a DTD is how XML's entity
  expansion attacks (the billion laughs) start.

Then `python-calamine` (Rust's calamine) reads the cells. A sheet has at most
:data:`MAX_ROWS` rows and :data:`MAX_COLUMNS` columns; a cell, at most Excel's 32,767
characters.

A CSV file is UTF-8 (with or without its byte order mark), UTF-16 with its mark, or
failing those Windows-1252, as Excel writes it on Windows. Its separator is a comma, a
semicolon, a tab or a bar, whichever its first lines are split by.
"""

from __future__ import annotations

import codecs
import csv
import datetime as dt
import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

__all__ = [
    "MAX_COLUMNS",
    "MAX_ROWS",
    "MAX_SIZE",
    "SAMPLE_COLUMNS",
    "SAMPLE_ROWS",
    "Sheet",
    "Unreadable",
    "Workbook",
    "as_json",
    "from_json",
    "kind_of",
    "open_workbook",
    "text_of",
]

#: The largest file Iki reads as a workbook.
MAX_SIZE = 100 * 1024 * 1024
#: The most an XLSX workbook's parts may hold, decompressed.
MAX_UNPACKED = 512 * 1024 * 1024
#: How many times its compressed size a part may grow, past a megabyte: sheets' XML
#: compresses about 10 to 30 times, and zip bombs a thousand.
MAX_RATIO = 200
#: The most parts a workbook may have.
MAX_PARTS = 10_000
#: The most rows a sheet may have (Excel's limit is 1,048,576).
MAX_ROWS = 1_000_000
#: The most columns: more than any table of records has.
MAX_COLUMNS = 1_000
#: The longest cell, as Excel allows.
MAX_CELL = 32_767
#: The most sheets a workbook may have.
MAX_SHEETS = 1_000
#: How much of each sheet's start Iki keeps to show: its first rows, and their first
#: columns. What it imports, its header row among it, is read from the workbook.
SAMPLE_ROWS = 30
SAMPLE_COLUMNS = 60

_ZIP = b"PK\x03\x04"
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_READ = 1024 * 1024
_SEPARATORS = ",;\t|"


class Unreadable(Exception):  # noqa: N818 - reads as "the workbook is unreadable"
    """Why Iki can't read the file as a workbook, for the person who gave it."""


#: A cell's value: text, a number, a truth value, a date or time, or None for an empty one.
Cell = str | int | float | bool | dt.date | dt.datetime | dt.time | dt.timedelta | None


@dataclass
class Sheet:
    """A sheet: its name, and what it holds once it's been read through."""

    name: str
    #: Rows up to its last that has anything in it.
    rows: int = 0
    #: The widest of them.
    columns: int = 0
    #: Its first rows, as text, for choosing its header and range.
    sample: list[list[str]] = field(default_factory=list)
    #: And as the workbook holds them, as JSON (:func:`as_json`): to show how they read.
    cells: list[list[Any]] = field(default_factory=list)
    hidden: bool = False


def kind_of(head: bytes, name: str) -> str:
    """``xlsx`` or ``csv``, from the file's first bytes (and its name, for text). Raises
    :class:`Unreadable` for anything else."""
    if head.startswith(_OLE):
        raise Unreadable(
            "It's encrypted, or an Excel 97–2003 workbook (.xls). Iki reads .xlsx workbooks "
            "without a password: in Excel, remove the password (File > Info > Protect "
            "Workbook) and save it as an Excel Workbook (.xlsx)."
        )
    if head.startswith(_ZIP):
        return "xlsx"
    lowered = name.lower()
    if lowered.endswith((".xlsx", ".xlsm", ".xls", ".ods", ".numbers")):
        raise Unreadable(
            "It isn't a workbook Iki can read: save it as an Excel Workbook (.xlsx), or as CSV."
        )
    if b"\x00" in head and not head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        raise Unreadable("It isn't text or a workbook: import an .xlsx workbook or a CSV file.")
    return "csv"


class Workbook:
    """An opened workbook: its sheets, and each one's rows."""

    def __init__(self, path: Path, kind: str, name: str) -> None:
        self.path = path
        self.kind = kind
        self.name = name
        #: For a CSV file: how it was read, its encoding and separator.
        self.encoding = ""
        self.separator = ""
        self._book: Any = None
        if kind == "xlsx":
            self._open_xlsx()
        else:
            self._open_csv()

    # --- XLSX ---------------------------------------------------------------------------------

    def _open_xlsx(self) -> None:
        _check_zip(self.path)
        import python_calamine

        try:
            self._book = python_calamine.CalamineWorkbook.from_path(str(self.path))
        except python_calamine.PasswordError:
            raise Unreadable(
                "It's protected with a password: remove it in Excel and save it again."
            ) from None
        except Exception as error:  # calamine's errors say what's wrong with the file
            raise Unreadable(f"It isn't a workbook Iki can read: {_first(error)}") from None
        self._meta = {
            m.name: m
            for m in self._book.sheets_metadata
            if str(m.typ).endswith("WorkSheet")  # not chart or macro sheets
        }
        if not self._meta:
            raise Unreadable("It has no sheets of cells.")
        if len(self._meta) > MAX_SHEETS:
            raise Unreadable(f"It has over {MAX_SHEETS:,} sheets.")

    @property
    def sheet_names(self) -> list[str]:
        if self.kind == "csv":
            return [_csv_sheet(self.name)]
        return list(self._meta)

    def rows(self, sheet: str) -> Iterator[list[Cell]]:
        """The sheet's rows, from its first, each its cells' values to its last one. Raises
        :class:`Unreadable` for a sheet over the limits, as it gets there."""
        if self.kind == "csv":
            yield from self._csv_rows()
            return
        if sheet not in self._meta:
            raise Unreadable(f"It has no sheet {sheet}.")
        import python_calamine

        try:
            found = self._book.get_sheet_by_name(sheet)
        except python_calamine.CalamineError as error:
            raise Unreadable(f"Its sheet {sheet} can't be read: {_first(error)}") from None
        start_column = (found.start or (0, 0))[1]
        last_row, last_column = found.end or (0, 0)
        if last_column + 1 > MAX_COLUMNS:
            raise Unreadable(f"Its sheet {sheet} has over {MAX_COLUMNS:,} columns.")
        if last_row + 1 > MAX_ROWS + 1:
            raise Unreadable(
                f"Its sheet {sheet} has over {MAX_ROWS:,} rows: split it into smaller sheets."
            )
        # calamine gives rows from the sheet's first, but cells from its first used column:
        # those left of it are empty, so each row's cells are where the workbook has them.
        pad: list[Cell] = [None] * start_column
        for values in found.iter_rows():
            yield [*pad, *(_cell(v) for v in values)]

    def hidden(self, sheet: str) -> bool:
        if self.kind == "csv":
            return False
        return not str(self._meta[sheet].visible).endswith("Visible")

    # --- CSV ----------------------------------------------------------------------------------

    def _open_csv(self) -> None:
        with self.path.open("rb") as raw:
            head = raw.read(64 * 1024)
        if head.startswith(codecs.BOM_UTF8):
            self.encoding = "utf-8-sig"
        elif head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            self.encoding = "utf-16"
        elif _is_utf8(self.path):
            self.encoding = "utf-8"
        else:
            self.encoding = "cp1252"
        try:
            text = head.decode(self.encoding, errors="ignore")
        except LookupError:  # pragma: no cover - the encodings are Python's own
            text = ""
        self.separator = _separator(text, self.name)

    def _text(self) -> IO[str]:
        return self.path.open(encoding=self.encoding, errors="strict", newline="")

    def _csv_rows(self) -> Iterator[list[Cell]]:
        csv.field_size_limit(MAX_CELL + 1)
        number = 0
        with self._text() as text:
            reader = csv.reader(text, delimiter=self.separator, strict=False)
            try:
                for row in reader:
                    number += 1
                    if number > MAX_ROWS + 1:
                        raise Unreadable(
                            f"It has over {MAX_ROWS:,} rows: split it into smaller files."
                        )
                    if len(row) > MAX_COLUMNS:
                        raise Unreadable(f"Its row {number} has over {MAX_COLUMNS:,} columns.")
                    yield [cell if cell != "" else None for cell in row]
            except csv.Error as error:
                raise Unreadable(f"Its row {number + 1} can't be read: {error}.") from None
            except UnicodeDecodeError:
                raise Unreadable(
                    f"Its row {number + 1} isn't {_encoding_name(self.encoding)} text: save "
                    "it as CSV UTF-8."
                ) from None

    # --- Both ---------------------------------------------------------------------------------

    def describe(
        self, sample_rows: int = SAMPLE_ROWS, sample_columns: int = SAMPLE_COLUMNS
    ) -> list[Sheet]:
        """Each sheet, read through: how many rows and columns, and its first rows."""
        out = []
        for name in self.sheet_names:
            sheet = Sheet(name, hidden=self.hidden(name))
            for number, row in enumerate(self.rows(name), start=1):
                used = _width(row)
                if used:
                    sheet.rows = number
                    sheet.columns = max(sheet.columns, used)
                if number <= sample_rows:
                    sheet.sample.append([text_of(c)[:200] for c in row[:sample_columns]])
                    sheet.cells.append([as_json(c) for c in row[:sample_columns]])
            # Its sample ends at its last row with anything in it.
            del sheet.sample[sheet.rows :]
            del sheet.cells[sheet.rows :]
            out.append(sheet)
        return out

    def row_text(self, sheet: str, number: int) -> list[str]:
        """A row's text, every column of it, as a sample keeps a cell's: its header row's,
        say. Empty for a row past the sheet's last, or for row 0."""
        if number < 1:
            return []
        for at, row in enumerate(self.rows(sheet), start=1):
            if at == number:
                return [text_of(c)[:200] for c in row[: _width(row)]]
        return []

    def close(self) -> None:
        if self._book is not None:
            self._book.close()
            self._book = None


def open_workbook(path: Path, name: str) -> Workbook:
    """The workbook at ``path``, which someone added as ``name``. Raises
    :class:`Unreadable`."""
    size = path.stat().st_size
    if size > MAX_SIZE:
        raise Unreadable(
            f"It's over {MAX_SIZE // (1024 * 1024)} MiB: split it into smaller workbooks."
        )
    if size == 0:
        raise Unreadable("It's empty.")
    with path.open("rb") as raw:
        head = raw.read(8)
    return Workbook(path, kind_of(head, name), name)


def as_json(value: Cell) -> Any:  # noqa: ANN401 - JSON
    """A cell's value as JSON keeps it, typed: :func:`from_json` reads it back."""
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, dt.datetime):
        return {"datetime": value.isoformat()}
    if isinstance(value, dt.date):
        return {"date": value.isoformat()}
    if isinstance(value, dt.time | dt.timedelta):
        return text_of(value) if isinstance(value, dt.time) else str(value)
    return value


def from_json(value: Any) -> Cell:  # noqa: ANN401 - JSON
    if isinstance(value, dict):
        if "datetime" in value:
            return dt.datetime.fromisoformat(value["datetime"])
        if "date" in value:
            return dt.date.fromisoformat(value["date"])
        return None
    return value  # type: ignore[no-any-return]


def text_of(value: Cell) -> str:
    """A cell's value as people read it in the sheet: ``12`` for 12.0, dates in ISO 8601."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    return str(value)


def _cell(value: Any) -> Cell:  # noqa: ANN401 - calamine's
    if value == "":
        return None
    if isinstance(value, str) and len(value) > MAX_CELL:
        raise Unreadable(f"A cell has over {MAX_CELL:,} characters.")
    return value  # type: ignore[no-any-return]


def _width(row: list[Cell]) -> int:
    for index in range(len(row) - 1, -1, -1):
        value = row[index]
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return index + 1
    return 0


def _check_zip(path: Path) -> None:
    """Reads every part of the zip through, counting, so a zip bomb or a DTD is refused
    before calamine parses anything."""
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as error:
        raise Unreadable(f"It isn't a workbook Iki can read: {_first(error)}") from None
    with archive:
        parts = archive.infolist()
        if len(parts) > MAX_PARTS:
            raise Unreadable(f"It has over {MAX_PARTS:,} parts: it isn't a workbook.")
        names = {p.filename for p in parts}
        if "xl/workbook.xml" not in names and "xl/workbook.bin" not in names:
            raise Unreadable(
                "It's a zip, but not an Excel workbook: save it as an Excel Workbook (.xlsx)."
            )
        if "xl/workbook.bin" in names:
            raise Unreadable(
                "It's an Excel Binary Workbook (.xlsb): save it as an Excel Workbook (.xlsx)."
            )
        unpacked = 0
        for part in parts:
            if part.flag_bits & 0x1:
                raise Unreadable("Its parts are encrypted: remove the password and save it again.")
            if part.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise Unreadable("Its parts are compressed in a way workbooks aren't.")
            if part.is_dir():
                continue
            limit = max(_READ, part.compress_size * MAX_RATIO)
            grown = 0
            xml = part.filename.endswith((".xml", ".rels"))
            tail = b""
            try:
                with archive.open(part) as stream:
                    while block := stream.read(_READ):
                        grown += len(block)
                        unpacked += len(block)
                        if grown > limit or unpacked > MAX_UNPACKED:
                            raise Unreadable(
                                "It unpacks to far more than it holds: it isn't a workbook Iki "
                                f"reads (at most {MAX_UNPACKED // (1024 * 1024)} MiB unpacked)."
                            )
                        if xml:
                            window = tail + block
                            if b"<!DOCTYPE" in window or b"<!ENTITY" in window:
                                raise Unreadable(
                                    "Its XML declares a document type, which workbooks don't: "
                                    "it isn't a workbook Iki reads."
                                )
                            tail = block[-16:]
            except (zipfile.BadZipFile, EOFError, OSError, ValueError) as error:
                raise Unreadable(f"It's damaged: {_first(error)}") from None
            except RuntimeError as error:  # an encrypted part, as zipfile says it
                raise Unreadable(f"It can't be read: {_first(error)}") from None


def _is_utf8(path: Path) -> bool:
    decoder = codecs.getincrementaldecoder("utf-8")()
    with path.open("rb") as raw:
        while block := raw.read(_READ):
            try:
                decoder.decode(block)
            except UnicodeDecodeError:
                return False
    try:
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _separator(text: str, name: str) -> str:
    if name.lower().endswith((".tsv", ".tab")):
        return "\t"
    lines = [line for line in io.StringIO(text).read().splitlines()[:20] if line.strip()]
    if not lines:
        return ","
    try:
        return csv.Sniffer().sniff("\n".join(lines), delimiters=_SEPARATORS).delimiter
    except csv.Error:
        # The one that splits the first line most.
        counts = {s: lines[0].count(s) for s in _SEPARATORS}
        best = max(counts, key=lambda s: counts[s])
        return best if counts[best] else ","


def _csv_sheet(name: str) -> str:
    """A CSV file's one sheet: named after the file, as Excel names it."""
    stem = name.rsplit("/", 1)[-1]
    return (stem.rsplit(".", 1)[0] if "." in stem else stem)[:100] or "Sheet1"


def _encoding_name(encoding: str) -> str:
    return {"utf-8": "UTF-8", "utf-8-sig": "UTF-8", "utf-16": "UTF-16", "cp1252": "Windows-1252"}[
        encoding
    ]


def _first(error: BaseException) -> str:
    text = str(error).strip().splitlines()
    return (text[0][:300] if text else type(error).__name__).rstrip(".") + "."
