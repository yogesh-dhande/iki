"""Labels for what's kept (ADR 0180): a unique record code as Data Matrix or QR, its human HRID,
and optionally the vendor's separate Code 128.

The browser lays them out at the size for a label printer's driver; for Zebra printers,
Iki writes ZPL, which draws the barcodes with the printer's own commands (``^BX``,
``^BQ``, ``^BC``), so they're as sharp as it prints. Each label prepared is an audit event
on its record.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from django.db import transaction

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.entities import registry
from iki_server.entities.models import Entity
from iki_server.storage import services as storage
from iki_server.storage.scan_identity import is_explicit, record_code
from iki_spec import titles

#: The most labels printed in one go: a full 1536-well plate's, or a freezer's boxes.
MAX_LABELS = 1536
MAX_COPIES = 10
DPIS = (203, 300)


@dataclass(frozen=True)
class Size:
    key: str
    label: str
    width_mm: float
    height_mm: float
    #: Whether it has room for a Code 128 a scanner reads.
    code128: bool


#: Common sizes: a cryo tube's wrap-around label, a 2 × 1 in label for a tube rack or a
#: bag, and a 3 × 1 in label for a box's end.
SIZES: dict[str, Size] = {
    s.key: s
    for s in (
        Size("cryo", "Cryo tube, 32.5 × 12.7 mm", 32.5, 12.7, code128=False),
        Size("standard", "Standard, 50.8 × 25.4 mm (2 × 1 in)", 50.8, 25.4, code128=True),
        Size("box", "Box, 76.2 × 25.4 mm (3 × 1 in)", 76.2, 25.4, code128=True),
    )
}
SYMBOLOGIES = ("datamatrix", "qr")


@dataclass(frozen=True)
class Label:
    entity: Entity
    code: str
    hrid: str
    title: str
    line: str
    vendor_code: str | None


def labels_of(
    entities: Sequence[Entity], *, size: Size, dpi: int, include_vendor_code: bool
) -> list[Label]:
    """Each record's label: its code, its HRID, its title, and its type's label line."""
    from iki_server.api.entities import _title
    from iki_server.entities.services import current_values_of
    from iki_server.entities.values import shape, to_json

    if include_vendor_code and not size.code128:
        raise storage.Wrong(
            {
                "include_vendor_code": [
                    "For an additional vendor Code 128, choose Standard or Box. "
                    "Turn off the vendor barcode to print the cryo label."
                ]
            }
        )
    values = current_values_of(list(entities))
    out = []
    for index, e in enumerate(entities):
        fields = shape(e.type).fields()
        shown = {n: to_json(fields[n], v) for n, v in values.get(e.pk, {}).items() if n in fields}
        found = storage.spec(e.type)
        line = ""
        if found is not None and found.label:
            line = titles.render(found.label, shown, e.hrid, dict(fields))
        title = _title(e, shown)
        vendor = None
        if include_vendor_code:
            vendor = shown.get(found.barcode) if found and found.barcode else None
            issue = _vendor_issue(vendor, size, dpi)
            if issue:
                raise storage.Wrong({f"records/{index}": [f"{e.hrid}: {issue}"]})
        out.append(
            Label(e, record_code(e.pk), e.hrid, "" if title == e.hrid else title, line, vendor)
        )
    return out


@transaction.atomic
def recorded(user: User, labels: Sequence[Label], *, size: Size, how: str, copies: int) -> None:
    """Keeps each label prepared, as an audit event on its record."""
    for label in labels:
        log.record(
            "entity.labelled",
            actor=user,
            target=label.entity,
            project=label.entity.owner_project,
            data={
                "size": size.key,
                "format": how,
                "copies": copies,
                "code": label.code,
                "vendor_code": label.vendor_code,
            },
        )


# --- ZPL ----------------------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _field(text: str) -> str:
    """Field data as ZPL reads it with ``^FH\\``: its own control characters hex-escaped."""
    clean = _CONTROL.sub(" ", text)
    return clean.replace("\\", "\\5C").replace("^", "\\5E").replace("~", "\\7E")


def _fit(text: str, width_dots: int, height_dots: int) -> str:
    """``text`` cut to what fits in ``width_dots`` of ZPL's font 0 at ``height_dots``: its
    characters are about half as wide as they're tall."""
    most = max(1, int(width_dots / (0.56 * height_dots)))
    return text if len(text) <= most else text[: max(1, most - 3)].rstrip() + "..."


def zpl(
    labels: Sequence[Label],
    *,
    size: Size,
    dpi: int = 203,
    symbology: str = "datamatrix",
    copies: int = 1,
) -> str:
    """ZPL II for ``labels``, one label each, ``copies`` times."""
    per_mm = dpi / 25.4

    def d(mm: float) -> int:
        return round(mm * per_mm)

    width, height = d(size.width_mm), d(size.height_mm)
    margin = d(0.5 if not size.code128 else 1.5)
    gap = d(1)
    narrow = d(0.25)
    # All canonical 36-character IDs fit 24×24 ECC200 even uncompressed, or version 3 QR
    # at level Q in alphanumeric mode. Reserve quiet zones independently of field content.
    modules, quiet = (24, 1) if symbology == "datamatrix" else (29, 4)
    font_scale = round(d(2.5) / 9)  # Font A is monospaced: 5 dots wide, 9 tall, 1 space.
    out: list[str] = []
    for label in labels:
        bar_height = d(6) if label.vendor_code is not None else 0
        bottom = height - margin - (bar_height + gap if bar_height else 0)
        available = min(bottom - margin, d(14))
        module = available // (modules + 2 * quiet)
        origin = margin + quiet * module
        text_x = margin + (modules + 2 * quiet) * module + gap
        text_width = width - text_x - margin
        lines = ["^XA", "^CI28", f"^PW{width}", f"^LL{height}", "^LH0,0"]
        if symbology == "qr":
            lines.append(f"^FO{origin},{origin}^BQN,2,{module}^FH\\^FDQM,A{label.code}^FS")
        else:
            lines.append(f"^FO{origin},{origin}^BXN,{module},200,24,24^FH\\^FD{label.code}^FS")
        # HRIDs are up to 32 characters. Wrap them losslessly, taking space from the
        # optional title and description first.
        y = margin
        chars = text_width // (6 * font_scale)
        for offset in range(0, len(label.hrid), chars):
            part = _field(label.hrid[offset : offset + chars])
            lines.append(f"^FO{text_x},{y}^AAN,{9 * font_scale},{5 * font_scale}^FH\\^FD{part}^FS")
            y += 10 * font_scale
        small = d(2.3 if not size.code128 else 2.8)
        for text in (label.title, label.line):
            if not text or y + small > bottom:
                continue
            fitted = _field(_fit(text, text_width, small))
            lines.append(f"^FO{text_x},{y}^A0N,{small},{small}^FH\\^FD{fitted}^FS")
            y += small + d(0.6)
        if label.vendor_code is not None:
            # The origin locates the bars. Leave ten narrow modules blank on each end.
            bar_x = margin + 10 * narrow
            data, _ = _code128(label.vendor_code)
            lines.append(
                f"^FO{bar_x},{height - margin - bar_height}^BY{narrow},3,{bar_height}"
                f"^BCN,{bar_height},N,N,N,N^FH\\^FD{_field(data)}^FS"
            )
        lines.extend([f"^PQ{copies},0,1,Y", "^XZ"])
        out.append("\n".join(lines))
    return "\n".join(out) + "\n"


def _code128(code: str) -> tuple[str, int]:
    """Exact Code 128 data and module width, including both ten-module quiet zones.

    Explicitly use subset C for digit pairs in runs of four or more. The printer uses B
    for the other printable ASCII characters; >, ^ and ~ use Zebra's literal invocations.
    """
    data: list[str] = []
    symbols = 0
    for index, part in enumerate(re.split(r"((?:[0-9]{2}){2,})", code)):
        if not part:
            continue
        if index % 2:
            symbols += len(part) // 2 + int(bool(data))
            data.append((">5" if data else ">;") + part)
        else:
            if data:
                data.append(">6")
                symbols += 1
            data.append(part.replace(">", ">0").replace("^", "><").replace("~", ">="))
            symbols += len(part)
    # Start and checksum are 11 modules each, stop 13, plus two 10-module quiet zones.
    return "".join(data), 11 * symbols + 55


def code128_modules(code: str) -> int:
    return _code128(code)[1]


def _vendor_issue(code: str | None, size: Size, dpi: int) -> str | None:
    if not code:
        return "There is no vendor barcode on this record. Turn it off to print the Iki ID."
    if is_explicit(code):
        return "This vendor value uses a reserved Iki identity. Turn off the vendor barcode."
    if not code.isascii() or not code.isprintable():
        return "The vendor Code 128 needs printable ASCII. Turn it off to print the Iki ID."
    width = round(size.width_mm * dpi / 25.4)
    margin = round(1.5 * dpi / 25.4)
    narrow = round(0.25 * dpi / 25.4)
    if code128_modules(code) * narrow > width - 2 * margin:
        return (
            "The full vendor Code 128 does not fit this stock. Choose a wider label "
            "or turn off the vendor barcode."
        )
    return None


def size_of(key: str) -> Size | None:
    return SIZES.get(key)


def describe(sizes: Mapping[str, Size] = SIZES) -> list[dict[str, object]]:
    return [
        {
            "key": s.key,
            "label": s.label,
            "width_mm": s.width_mm,
            "height_mm": s.height_mm,
            "code128": s.code128,
        }
        for s in sizes.values()
    ]


def storable(entity: Entity) -> bool:
    return storage.spec(entity.type) is not None and entity.type in registry.types()
