//! CSV and XLSX tables: text in each encoding the importer reads, workbooks built here
//! (stored and Deflate parts, ZIP64), workbooks from openpyxl and Excel, and broken and
//! hostile ones.
// Test code: indexes, casts and short names as the formats' specifications write them.
#![allow(
    clippy::unwrap_used,
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_precision_loss,
    clippy::cast_sign_loss,
    clippy::too_many_lines,
    clippy::many_single_char_names,
    clippy::format_push_string,
    clippy::format_collect,
    clippy::items_after_statements,
    clippy::identity_op,
    clippy::range_plus_one,
    clippy::manual_is_multiple_of,
    clippy::needless_pass_by_value
)]

use iki_previews::table::{self, MAX_CELL_CHARS, Table};

fn csv(bytes: &[u8]) -> Table {
    table::csv(bytes, 0, 100, 100, true).unwrap()
}

fn rows(t: &Table) -> Vec<Vec<&str>> {
    t.rows
        .iter()
        .map(|r| r.iter().map(String::as_str).collect())
        .collect()
}

// --- CSV ------------------------------------------------------------------------------

#[test]
fn csv_quoted_fields_and_a_byte_order_mark() {
    let t = csv(b"\xef\xbb\xbfSample,Conc (ng/uL),Note\r\nS-001,12.5,\"diluted 1:10, twice\"\r\nS-002,7,\"two\nlines\"\r\n");
    assert_eq!(t.encoding, "UTF-8");
    assert_eq!(
        rows(&t),
        [
            vec!["Sample", "Conc (ng/uL)", "Note"],
            vec!["S-001", "12.5", "diluted 1:10, twice"],
            vec!["S-002", "7", "two\nlines"]
        ]
    );
    assert_eq!((t.first_row, t.first_column, t.columns), (1, 1, 3));
    assert!(!t.more_rows && !t.more_columns && t.problems.is_empty());
    assert!(t.sheets.is_empty());
}

#[test]
fn csv_delimiters_found_and_given() {
    // A European export: semicolons, decimal commas.
    let t = csv(b"Well;Cq;Quantity\nA1;18,43;1,52E+05\nA2;31,07;0,000125\n");
    assert_eq!(rows(&t)[1], ["A1", "18,43", "1,52E+05"]);
    let t = csv(b"a\tb\n1\t2\n");
    assert_eq!(rows(&t)[1], ["1", "2"]);
    let t = csv(b"a|b|c\n1|2|3\n");
    assert_eq!(rows(&t)[0], ["a", "b", "c"]);
    // Given: a tab, though the text has commas.
    let t = table::csv(b"a,b\tc\n", b'\t', 10, 10, true).unwrap();
    assert_eq!(rows(&t), [vec!["a,b", "c"]]);
    assert!(table::csv(b"a", b'"', 10, 10, true).is_err());
}

#[test]
fn csv_encodings() {
    let text = "Gène;Δ Cq\nµ-actin;1,2\n";
    let mut le = vec![0xff, 0xfe];
    le.extend(text.encode_utf16().flat_map(u16::to_le_bytes));
    let t = csv(&le);
    assert_eq!(
        (t.encoding.as_str(), rows(&t)[1].clone()),
        ("UTF-16LE", vec!["µ-actin", "1,2"])
    );
    let mut be = vec![0xfe, 0xff];
    be.extend(text.encode_utf16().flat_map(u16::to_be_bytes));
    let t = csv(&be);
    assert_eq!(
        (t.encoding.as_str(), rows(&t)[0].clone()),
        ("UTF-16BE", vec!["Gène", "Δ Cq"])
    );
    // Excel on Windows: é, €, curly quotes.
    let t = csv(b"Compound,Price\nCaf\xe9ine,\x8012\n\x93neat\x94,1\n");
    assert_eq!(t.encoding, "windows-1252");
    assert_eq!(rows(&t)[1..], [vec!["Caféine", "€12"], vec!["“neat”", "1"]]);
    // Bytes Windows-1252 doesn't have: shown, and the importer's refusal said.
    let t = csv(b"a\x81b,c\n\xff,1\n");
    assert_eq!(t.problems.len(), 1, "{:?}", t.problems);
}

#[test]
fn csv_the_start_of_a_file() {
    // The page read only the first bytes: the last record may be cut.
    let t = table::csv(b"id,name\n1,alpha\n2,be", 0, 100, 100, false).unwrap();
    assert_eq!(rows(&t), [vec!["id", "name"], vec!["1", "alpha"]]);
    assert!(t.more_rows);
    // A character cut in half at the end is no reason to read it as Windows-1252.
    let t = table::csv(
        "id,name\n1,Gène\n2,Gè".as_bytes().split_last().unwrap().1,
        0,
        100,
        100,
        false,
    )
    .unwrap();
    assert_eq!(t.encoding, "UTF-8");
    assert_eq!(rows(&t)[1], ["1", "Gène"]);
}

#[test]
fn csv_limits() {
    let mut text = String::from("a,b,c,d,e\n");
    for i in 0..50 {
        text.push_str(&format!("{i},x,y,z,w\n"));
    }
    let t = table::csv(text.as_bytes(), 0, 10, 3, true).unwrap();
    assert_eq!(t.rows.len(), 10);
    assert_eq!(rows(&t)[9], ["8", "x", "y"]);
    assert!(t.more_rows && t.more_columns);
    assert_eq!(t.columns, 5);
    // Padded to the widest row; a row wider than the first is said.
    let t = csv(b"a,b\n1\n1,2,3\n");
    assert_eq!(
        rows(&t),
        [vec!["a", "b", ""], vec!["1", "", ""], vec!["1", "2", "3"]]
    );
    assert_eq!(
        t.problems,
        ["Row 3 has 3 cells, more than the first row's 2."]
    );
    // A long cell is cut.
    let long = "x".repeat(MAX_CELL_CHARS + 5);
    let t = csv(format!("a\n{long}\n").as_bytes());
    assert_eq!(t.cut_cells, 1);
    assert_eq!(t.rows[1][0].chars().count(), MAX_CELL_CHARS + 1);
    assert!(t.rows[1][0].ends_with('\u{2026}'));
}

#[test]
fn csv_blank_lines_are_rows_as_the_importer_reads_them() {
    let t = csv(b"a,b\n\n1,2\r\n\r\n3,4\n\n\n");
    assert_eq!(
        rows(&t),
        [
            vec!["a", "b"],
            vec!["", ""],
            vec!["1", "2"],
            vec!["", ""],
            vec!["3", "4"]
        ]
    );
}

#[test]
fn csv_that_isnt_text() {
    let ole = table::csv(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest", 0, 10, 10, true).unwrap_err();
    assert!(ole.contains(".xls"), "{ole}");
    assert!(
        table::csv(b"PK\x03\x04rest", 0, 10, 10, true)
            .unwrap_err()
            .contains("workbook")
    );
    assert!(
        table::csv(b"\0\0\x01\0\x01\0 an icon", 0, 10, 10, true)
            .unwrap_err()
            .contains("isn't text")
    );
    assert_eq!(table::csv(b"", 0, 10, 10, true).unwrap_err(), "It's empty.");
}

// --- A small ZIP writer ---------------------------------------------------------------

struct Part {
    name: String,
    data: Vec<u8>,
    deflate: bool,
    flags: u16,
    method: Option<u16>,
}

fn part(name: &str, data: impl Into<Vec<u8>>) -> Part {
    Part {
        name: name.to_owned(),
        data: data.into(),
        deflate: true,
        flags: 0,
        method: None,
    }
}

fn zip(parts: &[Part], zip64: bool) -> Vec<u8> {
    let mut out = Vec::new();
    let mut central = Vec::new();
    for p in parts {
        let stored = if p.deflate {
            miniz_oxide::deflate::compress_to_vec(&p.data, 6)
        } else {
            p.data.clone()
        };
        let method = p.method.unwrap_or(if p.deflate { 8 } else { 0 });
        let offset = out.len() as u32;
        out.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
        out.extend_from_slice(&20u16.to_le_bytes());
        out.extend_from_slice(&p.flags.to_le_bytes());
        out.extend_from_slice(&method.to_le_bytes());
        out.extend_from_slice(&[0; 8]); // time, date, crc (not checked)
        out.extend_from_slice(&(stored.len() as u32).to_le_bytes());
        out.extend_from_slice(&(p.data.len() as u32).to_le_bytes());
        out.extend_from_slice(&(p.name.len() as u16).to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(p.name.as_bytes());
        out.extend_from_slice(&stored);
        central.extend_from_slice(&0x0201_4b50u32.to_le_bytes());
        central.extend_from_slice(&[20, 0, 20, 0]);
        central.extend_from_slice(&p.flags.to_le_bytes());
        central.extend_from_slice(&method.to_le_bytes());
        central.extend_from_slice(&[0; 8]);
        central.extend_from_slice(&(stored.len() as u32).to_le_bytes());
        central.extend_from_slice(&(p.data.len() as u32).to_le_bytes());
        central.extend_from_slice(&(p.name.len() as u16).to_le_bytes());
        central.extend_from_slice(&[0; 12]); // extra, comment, disk, attributes
        central.extend_from_slice(&offset.to_le_bytes());
        central.extend_from_slice(p.name.as_bytes());
    }
    let directory = out.len() as u64;
    out.extend_from_slice(&central);
    let count = parts.len() as u64;
    if zip64 {
        let record = out.len() as u64;
        out.extend_from_slice(&0x0606_4b50u32.to_le_bytes());
        out.extend_from_slice(&44u64.to_le_bytes());
        out.extend_from_slice(&[45, 0, 45, 0, 0, 0, 0, 0, 0, 0, 0, 0]);
        out.extend_from_slice(&count.to_le_bytes());
        out.extend_from_slice(&count.to_le_bytes());
        out.extend_from_slice(&(central.len() as u64).to_le_bytes());
        out.extend_from_slice(&directory.to_le_bytes());
        out.extend_from_slice(&0x0706_4b50u32.to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&record.to_le_bytes());
        out.extend_from_slice(&1u32.to_le_bytes());
    }
    out.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
    out.extend_from_slice(&[0; 4]);
    let (n, size, at) = if zip64 {
        (0xffff, 0xffff_ffff, 0xffff_ffff)
    } else {
        (count as u16, central.len() as u32, directory as u32)
    };
    out.extend_from_slice(&n.to_le_bytes());
    out.extend_from_slice(&n.to_le_bytes());
    out.extend_from_slice(&size.to_le_bytes());
    out.extend_from_slice(&at.to_le_bytes());
    out.extend_from_slice(b"\x05\x00hello");
    out
}

const NS: &str = r#"xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships""#;
const REL: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";

/// A workbook of these sheets (name, state, its XML's `sheetData` contents), and parts.
fn workbook(
    sheets: &[(&str, &str, &str)],
    strings: &[&str],
    styles: Option<&str>,
    date1904: bool,
) -> Vec<Part> {
    let mut parts = vec![part("[Content_Types].xml", "<Types/>")];
    let mut book = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><workbook {NS}><workbookPr{}/><sheets>",
        if date1904 { " date1904=\"1\"" } else { "" }
    );
    let mut rels = String::from(
        "<?xml version=\"1.0\"?><Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">",
    );
    for (i, (name, state, data)) in sheets.iter().enumerate() {
        let state = if state.is_empty() {
            String::new()
        } else {
            format!(" state=\"{state}\"")
        };
        book.push_str(&format!(
            "<sheet name=\"{name}\" sheetId=\"{}\"{state} r:id=\"rId{}\"/>",
            i + 1,
            i + 1
        ));
        rels.push_str(&format!("<Relationship Id=\"rId{}\" Type=\"{REL}/worksheet\" Target=\"worksheets/sheet{}.xml\"/>", i + 1, i + 1));
        parts.push(part(
            &format!("xl/worksheets/sheet{}.xml", i + 1),
            format!(
                "<?xml version=\"1.0\"?><worksheet {NS}><sheetData>{data}</sheetData></worksheet>"
            ),
        ));
    }
    book.push_str("</sheets></workbook>");
    rels.push_str(&format!("<Relationship Id=\"rIdS\" Type=\"{REL}/sharedStrings\" Target=\"sharedStrings.xml\"/><Relationship Id=\"rIdT\" Type=\"{REL}/styles\" Target=\"/xl/styles.xml\"/></Relationships>"));
    parts.push(part("xl/workbook.xml", book));
    parts.push(part("xl/_rels/workbook.xml.rels", rels));
    let sst: String = strings
        .iter()
        .map(|s| format!("<si><t xml:space=\"preserve\">{s}</t></si>"))
        .collect();
    parts.push(part(
        "xl/sharedStrings.xml",
        format!(
            "<sst {NS} count=\"{0}\" uniqueCount=\"{0}\">{sst}</sst>",
            strings.len()
        ),
    ));
    if let Some(styles) = styles {
        parts.push(part(
            "xl/styles.xml",
            format!("<styleSheet {NS}>{styles}</styleSheet>"),
        ));
    }
    parts
}

fn xlsx(parts: &[Part], sheet: usize) -> Result<Table, String> {
    table::xlsx(&zip(parts, false), sheet, 100, 100)
}

const STYLES: &str = r#"<numFmts count="2"><numFmt numFmtId="164" formatCode="yyyy\-mm\-dd\ hh:mm"/><numFmt numFmtId="165" formatCode="0.00&quot; mg&quot;"/></numFmts><cellStyleXfs count="1"><xf numFmtId="14"/></cellStyleXfs><cellXfs count="5"><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/><xf numFmtId="165"/><xf numFmtId="46"/></cellXfs>"#;

// --- XLSX -----------------------------------------------------------------------------

#[test]
fn xlsx_values_as_the_importer_writes_them() {
    let data = r#"
<row r="3"><c r="C3" t="s"><v>0</v></c><c r="D3" t="s"><v>1</v></c><c r="E3" t="inlineStr"><is><r><t xml:space="preserve">Run </t></r><r><t>date</t></r><rPh><t>ignored</t></rPh></is></c></row>
<row r="4"><c r="C4"><v>18.4321</v></c><c r="D4" s="1"><v>46090</v></c><c r="E4" s="2"><v>46090.5</v></c><c r="F4" t="b"><v>1</v></c></row>
<row r="5"><c r="C5" s="3"><v>2.5</v></c><c r="D5" t="e"><f>1/0</f><v>#DIV/0!</v></c><c r="E5" t="str"><f>A1&amp;"x"</f><v>a&amp;b_x000D_</v></c><c r="F5" s="4"><v>1.5</v></c></row>
<row r="6"><c r="C6"><v>1.5E-10</v></c><c r="D6"><v>1E+16</v></c><c r="E6" s="1"><v>0.25</v></c><c r="F6" t="b"><v>0</v></c></row>
"#;
    let parts = workbook(
        &[
            ("Plate 1", "", data),
            ("Calibration", "hidden", ""),
            ("Raw", "veryHidden", ""),
        ],
        &["Well", " Sample &lt;1&gt; "],
        Some(STYLES),
        false,
    );
    let t = xlsx(&parts, 0).unwrap();
    assert_eq!(
        t.sheets
            .iter()
            .map(|s| (s.name.as_str(), s.hidden))
            .collect::<Vec<_>>(),
        [("Plate 1", false), ("Calibration", true), ("Raw", true)]
    );
    assert_eq!((t.first_row, t.first_column, t.columns), (3, 3, 4));
    assert_eq!(
        rows(&t),
        [
            vec!["Well", " Sample <1> ", "Run date", ""],
            vec!["18.4321", "2026-03-09", "2026-03-09T12:00:00", "TRUE"],
            vec!["2.5", "#DIV/0!", "a&b\r", "1 day, 12:00:00"],
            vec!["1.5e-10", "1e+16", "06:00:00", "FALSE"],
        ]
    );
    assert_eq!(t.encoding, "UTF-8");
    assert!(t.problems.is_empty(), "{:?}", t.problems);
}

#[test]
fn xlsx_the_1904_date_system() {
    let parts = workbook(
        &[(
            "S",
            "",
            r#"<row r="1"><c r="A1" s="1"><v>0</v></c><c r="B1" s="1"><v>1</v></c></row>"#,
        )],
        &[],
        Some(STYLES),
        true,
    );
    let t = xlsx(&parts, 0).unwrap();
    // A serial under 1 is a time of day, as calamine reads it.
    assert_eq!(rows(&t), [vec!["00:00:00", "1904-01-02"]]);
}

#[test]
fn xlsx_rows_and_cells_without_references() {
    let data = r#"<row><c><v>1</v></c><c><v>2</v></c></row><row><c r="C2"><v>3</v></c><c><v>4</v></c></row>"#;
    let t = xlsx(&workbook(&[("S", "", data)], &[], None, false), 0).unwrap();
    assert_eq!(rows(&t), [vec!["1", "2", "", ""], vec!["", "", "3", "4"]]);
}

#[test]
fn xlsx_limits_stop_reading() {
    let mut data = String::new();
    for r in 1..=5_000 {
        data.push_str(&format!("<row r=\"{r}\">"));
        for c in ["A", "B", "C", "D"] {
            data.push_str(&format!("<c r=\"{c}{r}\"><v>{r}</v></c>"));
        }
        data.push_str("</row>");
    }
    let file = zip(&workbook(&[("S", "", &data)], &[], None, false), false);
    let t = table::xlsx(&file, 0, 25, 2).unwrap();
    assert_eq!(t.rows.len(), 25);
    assert_eq!(rows(&t)[24], ["25", "25"]);
    assert!(t.more_rows && t.more_columns);
    assert_eq!(t.columns, 4);
    // Rows before the first with anything in it are left out, empty ones between kept.
    let data = r#"<row r="2"><c r="B2"/></row><row r="4"><c r="B4"><v>1</v></c></row><row r="6"><c r="C6"><v>2</v></c></row>"#;
    let t = xlsx(&workbook(&[("S", "", data)], &[], None, false), 0).unwrap();
    assert_eq!((t.first_row, t.first_column), (4, 2));
    assert_eq!(rows(&t), [vec!["1", ""], vec!["", ""], vec!["", "2"]]);
}

#[test]
fn xlsx_extreme_and_wrong_references() {
    let data = r#"<row r="1048576"><c r="XFD1048576"><v>9</v></c></row>"#;
    let t = xlsx(&workbook(&[("S", "", data)], &[], None, false), 0).unwrap();
    assert_eq!(
        (t.first_row, t.first_column, rows(&t)),
        (1_048_576, 16_384, vec![vec!["9"]])
    );
    let data = r#"<row r="1"><c r="A1"><v>1</v></c><c r="XFE1"><v>2</v></c></row><row r="99999999"><c r="A99999999"><v>3</v></c></row><row r="5"><c r="B5"><v>4</v></c><c r="A5"><v>5</v></c></row><row r="2"><c r="A2"><v>6</v></c></row>"#;
    let t = xlsx(&workbook(&[("S", "", data)], &[], None, false), 0).unwrap();
    assert_eq!(
        rows(&t),
        [
            vec!["1", ""],
            vec!["", ""],
            vec!["", ""],
            vec!["", ""],
            vec!["", "4"]
        ]
    );
    assert_eq!(t.problems.len(), 4, "{:?}", t.problems);
    assert!(t.problems[0].contains("XFD"));
    assert!(t.problems[1].contains("99,999,999"));
    assert!(t.problems[2].contains("A5"));
    assert!(t.problems[3].contains("Row 2 comes after row 5"));
}

#[test]
fn xlsx_shared_string_out_of_range() {
    let data = r#"<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>7</v></c></row>"#;
    let t = xlsx(&workbook(&[("S", "", data)], &["only"], None, false), 0).unwrap();
    assert_eq!(rows(&t), [vec!["only", ""]]);
    assert_eq!(
        t.problems,
        ["Cell B1 refers to shared string 7, but the workbook has 1."]
    );
}

#[test]
fn xlsx_zip64_and_stored_parts() {
    let mut parts = workbook(
        &[("S", "", r#"<row r="1"><c r="A1"><v>42</v></c></row>"#)],
        &[],
        None,
        false,
    );
    for p in &mut parts {
        p.deflate = false;
    }
    let t = table::xlsx(&zip(&parts, true), 0, 10, 10).unwrap();
    assert_eq!(rows(&t), [vec!["42"]]);
}

#[test]
fn xlsx_sheets_that_dont_exist() {
    let parts = workbook(&[("A", "", ""), ("B", "", "")], &[], None, false);
    assert_eq!(
        xlsx(&parts, 2).unwrap_err(),
        "This workbook has 2 sheets, so it has no sheet 3."
    );
    let t = xlsx(&parts, 1).unwrap();
    assert!(t.rows.is_empty());
    assert_eq!((t.sheet, t.columns, t.first_row), (1, 0, 1));
}

#[test]
fn xlsx_zip_bombs_are_refused() {
    // Its sheet's XML grows past 200 times its compressed size before any row.
    let padding = " ".repeat(64 * 1024 * 1024);
    let data = format!("{padding}<row r=\"1\"><c r=\"A1\"><v>1</v></c></row>");
    let parts = workbook(&[("S", "", &data)], &[], None, false);
    let error = xlsx(&parts, 0).unwrap_err();
    assert!(
        error.starts_with("It unpacks to far more than it holds"),
        "{error}"
    );
    // As do its shared strings.
    let parts = workbook(
        &[("S", "", r#"<row r="1"><c r="A1" t="s"><v>1</v></c></row>"#)],
        &[&padding, "x"],
        None,
        false,
    );
    assert!(
        xlsx(&parts, 0)
            .unwrap_err()
            .starts_with("It unpacks to far more")
    );
}

#[test]
fn xlsx_that_cant_be_read() {
    let parts = || workbook(&[("S", "", "")], &[], None, false);
    let mut encrypted = parts();
    encrypted[1].flags = 1;
    assert_eq!(
        xlsx(&encrypted, 0).unwrap_err(),
        "Its parts are encrypted: remove the password and save it again."
    );
    let mut bzip2 = parts();
    bzip2[1].method = Some(12);
    assert_eq!(
        xlsx(&bzip2, 0).unwrap_err(),
        "Its parts are compressed in a way workbooks aren't."
    );
    let no_book: Vec<Part> = parts()
        .into_iter()
        .filter(|p| p.name != "xl/workbook.xml")
        .collect();
    assert!(
        xlsx(&no_book, 0)
            .unwrap_err()
            .contains("not an Excel workbook")
    );
    let xlsb = vec![part("xl/workbook.bin", "x")];
    assert!(xlsx(&xlsb, 0).unwrap_err().contains(".xlsb"));
    let no_rels: Vec<Part> = parts()
        .into_iter()
        .filter(|p| p.name != "xl/_rels/workbook.xml.rels")
        .collect();
    assert!(xlsx(&no_rels, 0).unwrap_err().contains("workbook.xml.rels"));
    let no_sheet: Vec<Part> = parts()
        .into_iter()
        .filter(|p| !p.name.contains("worksheets"))
        .collect();
    assert!(xlsx(&no_sheet, 0).unwrap_err().contains("is missing"));
    let mut doctype = parts();
    doctype[2].data = b"<?xml version=\"1.0\"?><!DOCTYPE lol [<!ENTITY lol \"lol\">]><worksheet><sheetData/></worksheet>".to_vec();
    assert!(xlsx(&doctype, 0).unwrap_err().contains("document type"));
    let file = zip(&parts(), false);
    assert!(
        table::xlsx(&file[..file.len() - 40], 0, 10, 10)
            .unwrap_err()
            .contains("damaged")
    );
    assert!(
        table::xlsx(b"not a zip", 0, 10, 10)
            .unwrap_err()
            .contains("isn't a workbook")
    );
    let mut broken = parts();
    broken[2].data = b"<worksheet><sheetData><row><c><v>1</v></row>".to_vec();
    assert!(xlsx(&broken, 0).unwrap_err().contains("XML is broken"));
}

/// A small deterministic generator, for mutating files.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
}

#[test]
fn mutated_workbooks_never_panic() {
    let mut originals = vec![
        sample("qpcr_plate_results.xlsx"),
        sample("calamine_any_sheets.xlsx"),
    ];
    let mut stored = workbook(
        &[(
            "S",
            "",
            r#"<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" s="1"><v>4.5</v></c></row>"#,
        )],
        &["x"],
        Some(STYLES),
        false,
    );
    for p in &mut stored {
        p.deflate = false;
    }
    originals.push(zip(&stored, false));
    let mut rng = Rng(0x9e37_79b9_7f4a_7c15);
    for original in &originals {
        for _ in 0..400 {
            let mut file = original.clone();
            for _ in 0..1 + rng.next() % 6 {
                let at = (rng.next() % file.len() as u64) as usize;
                file[at] = rng.next() as u8;
            }
            let _ = table::xlsx(&file, (rng.next() % 3) as usize, 50, 50);
            let _ = table::csv(&file, 0, 50, 50, rng.next() % 2 == 0);
        }
    }
}

// --- Workbooks from openpyxl and Excel ------------------------------------------------

fn sample(name: &str) -> Vec<u8> {
    let path = format!(
        "{}/../../../server/tests/data/previews/{name}",
        env!("CARGO_MANIFEST_DIR")
    );
    std::fs::read(path).unwrap()
}

#[test]
fn openpyxl_qpcr_results() {
    let t = table::xlsx(&sample("qpcr_plate_results.xlsx"), 0, 100, 100).unwrap();
    assert_eq!(
        t.sheets
            .iter()
            .map(|s| (s.name.as_str(), s.hidden))
            .collect::<Vec<_>>(),
        [("Results", false), ("Standard curve", true)]
    );
    assert_eq!((t.first_row, t.first_column, t.columns), (3, 2, 9));
    // As the importer reads it (python-calamine 0.9.1, then `text_of`).
    assert_eq!(
        rows(&t),
        [
            vec![
                "Well",
                "Sample",
                "Target",
                "Cq",
                "Quantity (copies/µL)",
                "Run started",
                "Run time",
                "Passed QC",
                "Notes"
            ],
            vec![
                "A1",
                "NTC",
                "GAPDH",
                "Undetermined",
                "",
                "2026-03-09T14:05:30",
                "1:42:07",
                "FALSE",
                "no template control"
            ],
            vec![
                "A2",
                "Donor 17 PBMC",
                "GAPDH",
                "18.4321",
                "152000",
                "2026-03-09T14:05:30",
                "1:42:07",
                "TRUE",
                ""
            ],
            vec![
                "A3",
                "Donor 17 PBMC",
                "IL-6",
                "31.07",
                "0.000125",
                "2026-03-09T14:05:30",
                "1:42:07",
                "TRUE",
                "  low copy, repeat  "
            ],
            vec![
                "B1",
                "Standard 1e6",
                "GAPDH",
                "15.002",
                "1000000",
                "2026-03-09",
                "1 day, 2:00:00",
                "TRUE",
                "Std curve slope \u{2212}3.32"
            ],
        ]
    );
    let t = table::xlsx(&sample("qpcr_plate_results.xlsx"), 1, 100, 100).unwrap();
    assert_eq!(rows(&t)[3], ["4", "21.65"]);
}

#[test]
fn excel_workbooks_from_calamines_tests() {
    // Worksheets only, as the importer lists them: the chart sheet isn't one.
    let t = table::xlsx(&sample("calamine_any_sheets.xlsx"), 0, 100, 100).unwrap();
    assert_eq!(
        t.sheets
            .iter()
            .map(|s| (s.name.as_str(), s.hidden))
            .collect::<Vec<_>>(),
        [("Visible", false), ("Hidden", true), ("VeryHidden", true)]
    );
    assert_eq!(
        rows(&t)[..3],
        [vec!["1", "2"], vec!["3", "4"], vec!["5", "6"]]
    );
    let t = table::xlsx(&sample("calamine_date_1904.xlsx"), 0, 100, 100).unwrap();
    assert_eq!(
        rows(&t),
        [
            vec!["2021-01-01", "15"],
            vec!["2021-01-02", "16"],
            vec!["10 days, 15:10:10", "17"]
        ]
    );
    // Phonetic runs (furigana) aren't the cell's text.
    let t = table::xlsx(&sample("calamine_rph.xlsx"), 0, 100, 100).unwrap();
    assert_eq!(rows(&t), [vec!["課きく\u{3000}毛こ"]]);
    let t = table::xlsx(
        &sample("calamine_rich_text_shared_strings.xlsx"),
        0,
        100,
        100,
    )
    .unwrap();
    assert_eq!(
        rows(&t),
        [
            vec!["test1"],
            vec!["test2 other"],
            vec!["test3 aaa"],
            vec!["test4"]
        ]
    );
    let error = table::xlsx(&sample("calamine_password_protected.xlsx"), 0, 100, 100).unwrap_err();
    assert!(
        error.starts_with("It's encrypted, or an Excel 97\u{2013}2003 workbook"),
        "{error}"
    );
}
