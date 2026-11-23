"""Labels for what's kept (ADR 0180): a record's code as a DataMatrix or QR code and as Code
128, its HRID, its title and its type's label line, at common label sizes.

The browser lays them out at the size for a label printer's driver; for Zebra printers,
Iki writes ZPL, which draws the barcodes with the printer's own commands (``^BX``,
``^BQ``, ``^BC``), so they're as sharp as it prints. Each label prepared is an audit event
on its record.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from iki_server.accounts.models import User
from iki_server.audit import log
from iki_server.entities import registry
from iki_server.entities.models import Entity
from iki_server.storage import services as storage
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


def labels_of(entities: Sequence[Entity]) -> list[Label]:
    """Each record's label: its code, its HRID, its title, and its type's label line."""
    from iki_server.api.entities import _title
    from iki_server.entities.services import current_values_of
    from iki_server.entities.values import shape, to_json

    codes = storage.codes_of(entities)
    values = current_values_of(list(entities))
    out = []
    for e in entities:
        fields = shape(e.type).fields()
        shown = {n: to_json(fields[n], v) for n, v in values.get(e.pk, {}).items() if n in fields}
        found = storage.spec(e.type)
        line = ""
        if found is not None and found.label:
            line = titles.render(found.label, shown, e.hrid, dict(fields))
        title = _title(e, shown)
        out.append(Label(e, codes[e.pk], e.hrid, "" if title == e.hrid else title, line))
    return out


def recorded(user: User, labels: Sequence[Label], *, size: Size, how: str, copies: int) -> None:
    """Keeps each label prepared, as an audit event on its record."""
    for label in labels:
        log.record(
            "entity.labelled",
            actor=user,
            target=label.entity,
            project=label.entity.owner_project,
            data={"size": size.key, "format": how, "copies": copies, "code": label.code},
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
    margin = d(1.0 if size.height_mm < 20 else 2.0)
    # The 2D code fills the label's height above its Code 128, or all of it.
    bar_height = d(5.0) if size.code128 else 0
    gap = d(1.0)
    square = height - 2 * margin - (bar_height + gap if size.code128 else 0)
    square = min(square, d(12.0))
    # 16 x 16 modules hold an HRID or a 12-character barcode; a QR code is 21 x 21 or 25 x 25.
    modules = 18 if symbology == "datamatrix" else 25
    module = max(2, square // modules)
    text_x = margin + module * modules + d(1.5)
    text_width = width - text_x - margin
    big = d(3.0 if size.height_mm < 20 else 3.6)
    small = d(2.3 if size.height_mm < 20 else 2.8)
    out: list[str] = []
    for label in labels:
        lines = [
            "^XA",
            "^CI28",
            f"^PW{width}",
            f"^LL{height}",
            "^LH0,0",
        ]
        code = _field(label.code)
        if symbology == "qr":
            lines.append(f"^FO{margin},{margin}^BQN,2,{module}^FH\\^FDQA,{code}^FS")
        else:
            lines.append(f"^FO{margin},{margin}^BXN,{module},200^FH\\^FD{code}^FS")
        y = margin
        rows = [(label.hrid, big), (label.title, small), (label.line, small)]
        for text, h in rows:
            if not text:
                continue
            if y + h > (height - margin - (bar_height + gap if size.code128 else 0)):
                break
            fitted = _field(_fit(text, text_width, h))
            lines.append(f"^FO{text_x},{y}^A0N,{h},{h}^FH\\^FD{fitted}^FS")
            y += h + d(0.6)
        narrow = max(1, d(0.25))
        if size.code128 and code128_modules(label.code) * narrow <= width - 2 * margin:
            lines.append(
                f"^FO{margin},{height - margin - bar_height}^BY{narrow},3,{bar_height}"
                f"^BCN,{bar_height},N,N,N,A^FH\\^FD{code}^FS"
            )
        lines.append(f"^PQ{copies},0,1,Y")
        lines.append("^XZ")
        out.append("\n".join(lines))
    return "\n".join(out) + "\n"


def code128_modules(code: str) -> int:
    """How many modules wide ``code`` is in Code 128, with its quiet zones: subset C's pairs
    of digits where there are at least four in a row, subset B's characters otherwise."""
    digits = re.findall(r"\d{4,}", code)
    paired = sum(len(run) // 2 * 2 for run in digits)
    symbols = (len(code) - paired) + paired // 2 + 2 * len(digits)
    return 11 * symbols + 35 + 20


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
