"""Workbooks for tests, written byte by byte: XLSX as the Office Open XML standard lays one
out (what Excel saves), so a test can make exactly the workbook it means, well formed or
not."""

import io
import zipfile
from collections.abc import Sequence
from typing import Any
from xml.sax.saxutils import escape

CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class Date:
    """A cell holding a date as Excel does: days since 1899-12-30, formatted as a date."""

    def __init__(self, serial: float) -> None:
        self.serial = serial


class Formula:
    """A cell with a formula, and the value Excel saved for it (None if it never did)."""

    def __init__(self, text: str, cached: Any = None) -> None:
        self.text = text
        self.cached = cached


def letter(index: int) -> str:
    out = ""
    n = index + 1
    while n:
        n, rest = divmod(n - 1, 26)
        out = chr(65 + rest) + out
    return out


def _cell(row: int, column: int, value: Any) -> str:
    ref = f"{letter(column)}{row}"
    if value is None:
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{int(value)}</v></c>'
    if isinstance(value, int | float):
        return f'<c r="{ref}"><v>{value}</v></c>'
    if isinstance(value, Date):
        return f'<c r="{ref}" s="1"><v>{value.serial}</v></c>'
    if isinstance(value, Formula):
        cached = "" if value.cached is None else f"<v>{escape(str(value.cached))}</v>"
        kind = ' t="str"' if isinstance(value.cached, str) else ""
        return f'<c r="{ref}"{kind}><f>{escape(value.text)}</f>{cached}</c>'
    return (
        f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'
    )


def xlsx(
    *sheets: tuple[str, Sequence[Sequence[Any]]],
    dimension: str | None = None,
    hidden: Sequence[str] = (),
    extra: dict[str, bytes] | None = None,
    sheet_xml: dict[int, str] | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    """A workbook of these sheets, each its name and its rows from row 1 and column A.
    ``extra`` adds parts as they are; ``sheet_xml`` replaces a sheet's XML (by its place)."""
    out = io.BytesIO()
    names = [n for n, _ in sheets]
    with zipfile.ZipFile(out, "w", compression) as z:
        z.writestr(
            "[Content_Types].xml",
            f'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="{CONTENT_TYPES}">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/'
                'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(len(sheets))
            )
            + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.spreadsheetml.styles+xml"/></Types>',
        )
        z.writestr(
            "_rels/.rels",
            f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{RELS}">'
            f'<Relationship Id="rId1" Type="{OFFICE}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        z.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{MAIN}" xmlns:r="{OFFICE}">'
            "<sheets>"
            + "".join(
                f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"'
                + (' state="hidden"' if n in hidden else "")
                + "/>"
                for i, n in enumerate(names)
            )
            + "</sheets></workbook>",
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{RELS}">'
            + "".join(
                f'<Relationship Id="rId{i + 1}" Type="{OFFICE}/worksheet" '
                f'Target="worksheets/sheet{i + 1}.xml"/>'
                for i in range(len(sheets))
            )
            + f'<Relationship Id="rId{len(sheets) + 1}" Type="{OFFICE}/styles" '
            'Target="styles.xml"/></Relationships>',
        )
        z.writestr(
            "xl/styles.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="{MAIN}"><cellXfs '
            'count="2"><xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/></cellXfs>'
            "</styleSheet>",
        )
        for i, (_, rows) in enumerate(sheets):
            if sheet_xml and i in sheet_xml:
                z.writestr(f"xl/worksheets/sheet{i + 1}.xml", sheet_xml[i])
                continue
            dim = f'<dimension ref="{dimension}"/>' if dimension else ""
            body = "".join(
                f'<row r="{r + 1}">'
                + "".join(_cell(r + 1, c, v) for c, v in enumerate(row))
                + "</row>"
                for r, row in enumerate(rows)
            )
            z.writestr(
                f"xl/worksheets/sheet{i + 1}.xml",
                f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{MAIN}">{dim}'
                f"<sheetData>{body}</sheetData></worksheet>",
            )
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return out.getvalue()


def csv_text(rows: Sequence[Sequence[Any]], separator: str = ",") -> str:
    import csv

    out = io.StringIO()
    writer = csv.writer(out, delimiter=separator, lineterminator="\r\n")
    writer.writerows([["" if v is None else v for v in row] for row in rows])
    return out.getvalue()
