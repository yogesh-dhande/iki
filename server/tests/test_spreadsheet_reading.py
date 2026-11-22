"""Reading spreadsheets (ADR 0178): an XLSX workbook's cell values, never its formulas or
macros; a CSV file however Excel wrote it; untrusted workbooks refused before they're
parsed; and each cell read as the field its column maps to."""

import codecs
import datetime as dt
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
from workbook_files import Date, Formula, csv_text, xlsx

from iki_server.spreadsheets import cells, mapping, workbooks
from iki_server.spreadsheets.cells import Column, Reference
from iki_server.spreadsheets.workbooks import Unreadable, open_workbook
from iki_spec.model import Field

# A plasmid inventory, as a lab keeps one (Addgene's catalogue numbers and descriptions).
INVENTORY: list[list[Any]] = [
    ["Plasmid", "Addgene ID", "Backbone", "Resistance", "Copy number", "Received"],
    ["pUC19", 50005, "pUC", "Ampicillin", "High", Date(46023)],
    ["pET-28a(+)", None, "pBR322", "Kanamycin", "Low", Date(46030)],
    ["pcDNA3.1(+)", None, "pUC", "Ampicillin; Neomycin", "High", Formula("TODAY()", 46031)],
]


def opened(tmp_path: Path, data: bytes | str, name: str = "inventory.xlsx") -> workbooks.Workbook:
    path = tmp_path / "upload"
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return open_workbook(path, name)


def refused(tmp_path: Path, data: bytes | str, name: str = "inventory.xlsx") -> str:
    with pytest.raises(Unreadable) as error:
        book = opened(tmp_path, data, name)
        for sheet in book.sheet_names:
            list(book.rows(sheet))
    return str(error.value)


# --- XLSX ---------------------------------------------------------------------------------------


def test_a_workbooks_sheets_and_values_are_read(tmp_path: Path) -> None:
    book = opened(tmp_path, xlsx(("Inventory", INVENTORY), ("Notes", [["Kept in freezer B"]])))
    assert book.kind == "xlsx"
    assert book.sheet_names == ["Inventory", "Notes"]
    rows = list(book.rows("Inventory"))
    assert rows[0][0] == "Plasmid"
    assert rows[1][:2] == ["pUC19", 50005.0]
    assert rows[1][5] == dt.date(2026, 1, 1)
    # An empty cell is None, wherever it is.
    assert rows[2][1] is None


def test_a_formulas_saved_value_is_read_and_nothing_is_calculated(tmp_path: Path) -> None:
    book = opened(
        tmp_path,
        xlsx(
            (
                "Sheet1",
                [
                    ["Total", "Never calculated", "Text"],
                    [Formula("SUM(1,2)", 3), Formula("SUM(1,2)"), Formula('"a"&"b"', "ab")],
                ],
            )
        ),
    )
    total, never, text = list(book.rows("Sheet1"))[1]
    assert total == 3
    # Saved without a value, it's empty: Iki doesn't calculate it.
    assert never is None
    assert text == "ab"


def test_a_sheets_size_is_what_it_holds_not_what_it_claims(tmp_path: Path) -> None:
    # Its <dimension> says it's a million rows; it has four.
    book = opened(tmp_path, xlsx(("Inventory", INVENTORY), dimension="A1:ALL1048576"))
    (sheet,) = book.describe()
    assert (sheet.rows, sheet.columns) == (4, 6)
    assert sheet.sample[1][:2] == ["pUC19", "50005"]


def test_cells_are_where_the_sheet_has_them(tmp_path: Path) -> None:
    rows = [[], [None, None, "Plasmid", "Backbone"], [None, None, "pUC19", "pUC"]]
    book = opened(tmp_path, xlsx(("Sheet1", rows)))
    read = list(book.rows("Sheet1"))
    (sheet,) = book.describe()
    assert read[1][2:4] == ["Plasmid", "Backbone"]
    assert read[2][:3] == [None, None, "pUC19"]
    assert sheet.rows == 3


def test_hidden_sheets_say_so(tmp_path: Path) -> None:
    book = opened(tmp_path, xlsx(("Lists", [["a"]]), ("Inventory", INVENTORY), hidden=["Lists"]))
    assert [(s.name, s.hidden) for s in book.describe()] == [
        ("Lists", True),
        ("Inventory", False),
    ]


def test_a_macro_enabled_workbooks_macros_are_never_run_or_read(tmp_path: Path) -> None:
    # An .xlsm keeps its macros in a binary part: Iki reads its cells, and nothing else.
    data = xlsx(("Inventory", INVENTORY), extra={"xl/vbaProject.bin": b"\xd0\xcf\x11\xe0" * 64})
    book = opened(tmp_path, data, "inventory.xlsm")
    assert list(book.rows("Inventory"))[1][0] == "pUC19"


# --- Untrusted workbooks -----------------------------------------------------------------------


def test_an_encrypted_workbook_is_refused_with_what_to_do(tmp_path: Path) -> None:
    # Excel saves a workbook with a password as an OLE compound file, as it did .xls.
    message = refused(tmp_path, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
    assert "encrypted" in message and "remove the password" in message


def test_a_zip_bomb_stops_before_it_unpacks(tmp_path: Path) -> None:
    # 64 MiB of zeros compresses about a thousandfold.
    bomb = xlsx(("Sheet1", [["a"]]), extra={"xl/media/padding.bin": b"\x00" * (64 * 1024 * 1024)})
    assert len(bomb) < 1024 * 1024
    assert "unpacks to far more than it holds" in refused(tmp_path, bomb)


def test_a_workbook_whose_parts_unpack_too_large_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workbooks, "MAX_UNPACKED", 4 * 1024)
    data = xlsx(("Inventory", INVENTORY * 40))
    assert "at most 0 MiB unpacked" in refused(tmp_path, data)


def test_xml_with_a_document_type_is_refused(tmp_path: Path) -> None:
    # The billion laughs: entities that expand to gigabytes.
    laughs = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>&lol2;</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    message = refused(tmp_path, xlsx(("Sheet1", []), sheet_xml={0: laughs}))
    assert "document type" in message


def test_an_encrypted_zip_part_is_refused(tmp_path: Path) -> None:
    data = bytearray(xlsx(("Sheet1", [["a"]])))
    # Set the encrypted flag on its first part, as a zip with a password has.
    data[6] |= 0x01
    data[data.find(b"PK\x01\x02") + 8] |= 0x01
    assert "encrypted" in refused(tmp_path, bytes(data)).lower()


def test_a_binary_workbook_is_refused(tmp_path: Path) -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.bin", b"\x83\x01\x00")
    assert ".xlsb" in refused(tmp_path, out.getvalue(), "inventory.xlsb")


def test_a_sheet_over_the_row_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workbooks, "MAX_ROWS", 1000)
    far = (
        '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml'
        '/2006/main"><sheetData><row r="1"><c r="A1"><v>1</v></c></row><row r="5000">'
        '<c r="A5000"><v>2</v></c></row></sheetData></worksheet>'
    )
    assert "over 1,000 rows" in refused(tmp_path, xlsx(("Sheet1", []), sheet_xml={0: far}))


def test_a_sheet_over_the_column_limit_is_refused(tmp_path: Path) -> None:
    wide = (
        '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml'
        '/2006/main"><sheetData><row r="1"><c r="A1"><v>1</v></c><c r="XFD1"><v>2</v></c>'
        "</row></sheetData></worksheet>"
    )
    assert "over 1,000 columns" in refused(tmp_path, xlsx(("Sheet1", []), sheet_xml={0: wide}))


def test_a_file_too_large_is_refused_before_its_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workbooks, "MAX_SIZE", 1024)
    assert "MiB" in refused(tmp_path, xlsx(("Inventory", INVENTORY * 20)))


def test_files_that_arent_workbooks_say_what_to_save_them_as(tmp_path: Path) -> None:
    assert "Excel Workbook (.xlsx)" in refused(tmp_path, b"\x00\x01binary", "inventory.ods")
    assert "isn't text" in refused(tmp_path, b"\x00\x01\x02\x03binary", "inventory.dat")
    assert "empty" in refused(tmp_path, b"", "inventory.csv")
    assert "isn't a workbook" in refused(tmp_path, b"PK\x03\x04garbage", "inventory.xlsx")


# --- CSV ----------------------------------------------------------------------------------------


def test_a_csv_files_encoding_and_separator_are_found(tmp_path: Path) -> None:
    rows = [["Plasmid", "Resistance", "Conc. (µg/mL)"], ["pUC19", "Ampicillin", "1,5"]]
    for data, encoding, separator in (
        (csv_text(rows).encode(), "utf-8", ","),
        (codecs.BOM_UTF8 + csv_text(rows).encode(), "utf-8-sig", ","),
        # Excel on a European Windows machine: Windows-1252, separated by semicolons.
        (csv_text(rows, ";").encode("cp1252"), "cp1252", ";"),
        (csv_text(rows, "\t").encode("utf-16"), "utf-16", "\t"),
    ):
        book = opened(tmp_path, data, "inventory.csv")
        assert (book.kind, book.encoding, book.separator) == ("csv", encoding, separator)
        assert next(iter(book.rows(book.sheet_names[0])))[2] == "Conc. (µg/mL)"


def test_a_csv_file_over_the_row_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workbooks, "MAX_ROWS", 10)
    assert "over 10 rows" in refused(tmp_path, csv_text([["a"]] * 20), "big.csv")


# --- Cells --------------------------------------------------------------------------------------

CONCENTRATION = Field("concentration", "quantity", unit="mg/mL")
TM = Field("tm", "quantity", unit="Cel")
TOPOLOGY = Field("topology", "enum", values=("linear", "circular"))
TAGS = Field("tags", "list", items=Field("tags", "string"))
RECEIVED = Field("received", "date")
SEEN = Field("seen", "datetime")


def read(f: Field, value: Any, **options: Any) -> Any:
    return cells.convert(f, Column(0, f.name, **options), value)


def wrong(f: Field, value: Any, **options: Any) -> str:
    with pytest.raises(ValueError, match=r".") as error:
        read(f, value, **options)
    return str(error.value)


def test_a_quantitys_cells_are_in_the_columns_unit_or_their_own() -> None:
    assert read(CONCENTRATION, 1250, unit="ug/mL") == {"value": 1250, "unit": "ug/mL"}
    assert read(CONCENTRATION, "2.1 mg/mL", unit="ug/mL") == {"value": 2.1, "unit": "mg/mL"}
    assert read(CONCENTRATION, "500 µg/mL") == {"value": 500, "unit": "ug/mL"}
    assert read(CONCENTRATION, 3.5) == {"value": 3.5, "unit": "mg/mL"}
    assert read(TM, "62 °C") == {"value": 62, "unit": "Cel"}


def test_a_unit_that_cant_convert_says_why() -> None:
    message = wrong(CONCENTRATION, "12 mM")
    assert "can't convert to mg/mL" in message
    assert cells.unit_problem(CONCENTRATION, "mol/L") is not None
    assert cells.unit_problem(CONCENTRATION, "g/L") is None


def test_headers_name_their_units() -> None:
    assert cells.unit_in("Conc. (ug/mL)") == "ug/mL"
    assert cells.unit_in("Tm [°C]") == "Cel"
    assert cells.unit_in("Plasmid (pUC)") is None
    assert cells.unit_in("Plasmid") is None


def ambiguous(f: Field, value: Any, **options: Any) -> str:
    with pytest.raises(cells.Ambiguous) as error:
        read(f, value, **options)
    return str(error.value)


PURITY = Field("purity", "float")


def test_numbers_read_as_the_sheet_writes_them() -> None:
    assert read(PURITY, "1.250,5", decimal="comma") == 1250.5
    assert read(PURITY, "1,234", decimal="point") == 1234
    assert read(PURITY, "1,234", decimal="comma") == 1.234
    assert read(PURITY, "\u22123.5") == -3.5  # a minus sign
    assert "With a decimal comma it would be 12.5" in wrong(PURITY, "12,5", decimal="point")
    assert "whole number" in wrong(Field("copies", "int"), 2.5)
    assert "truth value" in wrong(PURITY, True)


def test_a_number_that_reads_two_ways_is_flagged_until_the_sheets_locale_is_said() -> None:
    assert ambiguous(PURITY, "1,234") == (
        "reads as 1,234 with a decimal point, and 1.234 with a decimal comma: say which the "
        "sheet's numbers have."
    )
    assert "decimal" in ambiguous(PURITY, "1.234")
    assert "decimal" in ambiguous(CONCENTRATION, "2,500 mg/mL")
    # Text that reads one way only is read so, and numbers the workbook stored are numbers.
    assert read(PURITY, "1,250.5") == 1250.5
    assert read(PURITY, "1.250,5") == 1250.5
    assert read(PURITY, "12,5") == 12.5
    assert read(PURITY, "98.5") == 98.5
    assert read(PURITY, 1.234) == 1.234
    # The sheet's locale is each column's, unless the column says otherwise.
    locale = cells.Locale(decimal="comma")
    assert cells.convert(PURITY, Column(0, "purity").under(locale), "1,234") == 1.234
    assert (
        cells.convert(PURITY, Column(0, "purity", decimal="point").under(locale), "1,234") == 1234
    )


def test_choices_read_as_the_sheet_spells_them() -> None:
    assert read(TOPOLOGY, "Circular") == "circular"
    assert read(TOPOLOGY, "circ.", values={"circ.": "circular"}) == "circular"
    assert "isn't one of its choices (linear, circular)" in wrong(TOPOLOGY, "supercoiled")


def test_truth_values_read_as_people_write_them() -> None:
    sequenced = Field("sequenced", "bool")
    assert [read(sequenced, v) for v in ("Yes", "n", True, "TRUE", "✓")] == [
        True,
        False,
        True,
        True,
        True,
    ]
    assert read(sequenced, "Verified", values={"Verified": "true"}) is True
    assert "isn't yes or no" in wrong(sequenced, "maybe")


def test_lists_are_split_and_each_item_read() -> None:
    assert read(TAGS, "kinase; crystallography;") == ["kinase", "crystallography"]
    assert read(TAGS, "a|b", separator="|") == ["a", "b"]
    copies = Field("copies", "list", items=Field("copies", "int"))
    assert "Its item 2, “two”" in wrong(copies, "1; two")


def test_dates_need_their_order_when_its_only_numbers() -> None:
    assert read(RECEIVED, dt.date(2026, 3, 1)) == "2026-03-01"
    assert read(RECEIVED, "2026-03-01") == "2026-03-01"
    assert read(RECEIVED, "1 Mar 2026") == "2026-03-01"
    assert read(RECEIVED, "March 1, 2026") == "2026-03-01"
    assert read(RECEIVED, "01/03/2026", dates="day_first") == "2026-03-01"
    assert read(RECEIVED, "03/01/2026", dates="month_first") == "2026-03-01"
    assert ambiguous(RECEIVED, "03/04/2026") == (
        "reads as 3 April 2026 day first, and 4 March 2026 month first: say which the sheet's "
        "dates are."
    )
    # A date only one order makes is read so; a day that's its month too reads either way.
    assert read(RECEIVED, "31/12/2026") == "2026-12-31"
    assert read(RECEIVED, "12/31/2026") == "2026-12-31"
    assert read(RECEIVED, "04/04/2026") == "2026-04-04"
    assert "isn't a date, read month first" in wrong(RECEIVED, "31/12/2026", dates="month_first")
    assert "date and time" in wrong(RECEIVED, dt.datetime(2026, 3, 1, 9, 30))  # noqa: DTZ001 - as a workbook holds it


def test_excel_serial_dates_are_read_only_when_the_sheet_has_them() -> None:
    # A date column Excel lost the format of: its cells are numbers.
    assert wrong(RECEIVED, 46023) == (
        "is a number: if the sheet's dates are Excel serial dates (46023 is 1 January 2026), "
        "say so."
    )
    assert read(RECEIVED, 46023, serial_dates=True) == "2026-01-01"
    assert read(RECEIVED, "46023", serial_dates=True) == "2026-01-01"
    seen = read(SEEN, 46023.5, serial_dates=True, time_zone="Europe/London")
    assert seen == "2026-01-01T12:00:00+00:00"
    assert "Excel serial date" in wrong(RECEIVED, 0, serial_dates=True)


def test_a_date_and_time_needs_its_time_zone() -> None:
    assert "time zone" in wrong(SEEN, "2026-03-01T09:30")
    assert read(SEEN, "2026-03-01T09:30", time_zone="Europe/London") == "2026-03-01T09:30:00+00:00"
    assert read(SEEN, "2026-03-01T09:30+01:00") == "2026-03-01T09:30:00+01:00"


def test_a_reference_is_found_later_by_what_the_cell_says() -> None:
    assert read(Field("protein", "ref", to=("acme/protein.Protein",)), " ABL1 ") == Reference(
        "ABL1"
    )


def test_fields_a_cell_cant_set_say_why() -> None:
    assert "computed" in str(cells.not_mappable(Field("mw", "quantity", unit="u", computed=True)))
    assert "file" in str(cells.not_mappable(Field("map", "file")))
    assert "structured" in str(cells.not_mappable(Field("features", "json")))
    assert cells.not_mappable(TAGS) is None


# --- Mapping ------------------------------------------------------------------------------------


def test_headers_are_compared_without_case_punctuation_or_unit() -> None:
    assert mapping.key("Conc. (mg/mL)") == "conc"
    assert mapping.key("Due on") == mapping.key("due_on") == "dueon"
    assert mapping.letter(0) == "A" and mapping.letter(27) == "AB"
    sample = [["Label", "", "Purity"]]
    assert mapping.headers_of(sample, 1, 4) == ["Label", "Column B", "Purity", "Column D"]
    assert mapping.headers_of(sample, 0, 2) == ["Column A", "Column B"]


def test_a_saved_mapping_maps_a_sheet_whose_columns_moved() -> None:
    saved = mapping.saved_columns(
        [Column(0, "label"), Column(1), Column(2, "concentration", unit="ug/mL")],
        ["Label", "Notes", "Conc. (ug/mL)"],
    )
    assert saved == [
        {"header": "Label", "field": "label"},
        {"header": "Conc. (ug/mL)", "field": "concentration", "unit": "ug/mL"},
    ]
    headers = ["conc. (ug/mL)", "Label", "Extra"]
    assert mapping.matches([c["header"] for c in saved], headers) == 1.0
    assert mapping.from_saved(saved, headers) == [
        Column(0, "concentration", unit="ug/mL"),
        Column(1, "label"),
        Column(2),
    ]
    assert mapping.matches(["Label", "Lot"], headers) == 0.5
