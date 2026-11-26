//! XLSX and XLSM workbooks (ECMA-376), read as the server's importer reads them (through
//! calamine): the workbook's worksheets, its shared strings and number formats, then one
//! sheet's first rows, values as the importer's `text_of` writes them.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;
use std::io::BufReader;

use quick_xml::XmlVersion;
use quick_xml::events::{BytesStart, Event};

use super::zip::{Part, Zip};
use super::{
    COLUMN_LIMIT, MAX_CELL_CHARS, MAX_GRID_CELLS, MAX_SHARED_STRINGS, OLE, OLE_MESSAGE, Problems,
    ROW_LIMIT, Sheet, Table, grid, thousands,
};

/// The most sheets a workbook may have, as the importer allows.
const MAX_SHEETS: usize = 1_000;
/// Excel's last row.
const LAST_ROW: u32 = 1_048_576;
/// Excel's last column, XFD.
const LAST_COLUMN: u32 = 16_384;
/// The most cell formats (`cellXfs`) read; Excel allows 64,000.
const MAX_FORMATS: usize = 100_000;
/// The most relationships read from the workbook's.
const MAX_RELATIONSHIPS: usize = 100_000;

pub(super) fn read(
    bytes: &[u8],
    sheet: usize,
    max_rows: usize,
    max_columns: usize,
) -> Result<Table, String> {
    if bytes.starts_with(OLE) {
        return Err(OLE_MESSAGE.to_owned());
    }
    let zip = Zip::open(bytes)?;
    if !zip.has("xl/workbook.xml") {
        if zip.has("xl/workbook.bin") {
            return Err(
                "It's an Excel Binary Workbook (.xlsb): save it as an Excel Workbook (.xlsx)."
                    .to_owned(),
            );
        }
        return Err(
            "It's a zip, but not an Excel workbook: save it as an Excel Workbook (.xlsx)."
                .to_owned(),
        );
    }
    let mut problems = Problems::default();
    let book = Book::read(&zip, &mut problems)?;
    let Some(chosen) = book.sheets.get(sheet) else {
        let count = book.sheets.len();
        return Err(format!(
            "This workbook has {} sheet{}, so it has no sheet {}.",
            thousands(count),
            if count == 1 { "" } else { "s" },
            thousands(sheet.saturating_add(1)),
        ));
    };
    let part = zip.open_part(&chosen.target)?.ok_or_else(|| {
        format!(
            "Its sheet {} is missing: the workbook says it's at {}, but there's nothing there.",
            chosen.name, chosen.target
        )
    })?;
    let max_rows = max_rows.min(ROW_LIMIT);
    let read = read_sheet(part, max_rows, &mut problems)?;
    let strings = match (&book.shared_strings, read.shared.is_empty()) {
        (Some(target), false) => match zip.open_part(target)? {
            Some(part) => read_strings(part, &read.shared)?,
            None => Strings::default(),
        },
        _ => Strings::default(),
    };
    let formats = match (&book.styles, read.styled) {
        (Some(target), true) => match zip.open_part(target)? {
            Some(part) => read_formats(part)?,
            None => Vec::new(),
        },
        _ => Vec::new(),
    };
    let sheets = book
        .sheets
        .iter()
        .map(|s| Sheet {
            name: s.name.clone(),
            hidden: s.hidden,
        })
        .collect();
    Ok(lay_out(
        &read,
        &strings,
        &formats,
        book.date1904,
        max_columns,
        sheets,
        sheet,
        problems,
    ))
}

/// A worksheet, and where its part is.
struct BookSheet {
    name: String,
    hidden: bool,
    target: String,
}

/// What `xl/workbook.xml` and its relationships say.
struct Book {
    sheets: Vec<BookSheet>,
    date1904: bool,
    shared_strings: Option<String>,
    styles: Option<String>,
}

/// One relationship: its type and target part.
struct Relationship {
    kind: String,
    target: String,
}

impl Book {
    fn read(zip: &Zip<'_>, problems: &mut Problems) -> Result<Self, String> {
        let part = zip
            .open_part("xl/workbook.xml")?
            .ok_or_else(|| "It has no xl/workbook.xml.".to_owned())?;
        let mut xml = Xml::new(part);
        let mut listed = Vec::new();
        let mut date1904 = false;
        loop {
            match xml.next()? {
                Event::Start(e) => match e.local_name().as_ref() {
                    // `x15:workbookPr` in `extLst` shares the name, without `date1904`.
                    "workbookPr" => {
                        if let Some(value) = attribute(&e, "date1904") {
                            date1904 = value == "1" || value == "true";
                        }
                    }
                    "sheet" => {
                        if listed.len() >= MAX_SHEETS {
                            return Err(format!("It has over {} sheets.", thousands(MAX_SHEETS)));
                        }
                        let name = attribute(&e, "name").unwrap_or_default();
                        let state = attribute(&e, "state").unwrap_or_default();
                        let id = relationship_id(&e).unwrap_or_default();
                        listed.push((name, state == "hidden" || state == "veryHidden", id));
                    }
                    _ => {}
                },
                Event::Eof => break,
                _ => {}
            }
        }
        let relationships = read_relationships(zip)?;
        let mut sheets = Vec::new();
        for (name, hidden, id) in listed {
            match relationships.get(&id) {
                Some(r) if r.kind.ends_with("/worksheet") => sheets.push(BookSheet {
                    name,
                    hidden,
                    target: r.target.clone(),
                }),
                // Chart sheets, dialog sheets and macro sheets hold no cells.
                Some(_) => {}
                None => problems.add(format!(
                    "Its sheet {name} isn't in the workbook's relationships, so it was left out."
                )),
            }
        }
        if sheets.is_empty() {
            return Err("It has no sheets of cells.".to_owned());
        }
        let find = |suffix: &str, fallback: &str| {
            relationships
                .values()
                .find(|r| r.kind.ends_with(suffix))
                .map_or_else(|| fallback.to_owned(), |r| r.target.clone())
        };
        Ok(Self {
            sheets,
            date1904,
            shared_strings: Some(find("/sharedStrings", "xl/sharedStrings.xml")),
            styles: Some(find("/styles", "xl/styles.xml")),
        })
    }
}

fn read_relationships(zip: &Zip<'_>) -> Result<BTreeMap<String, Relationship>, String> {
    let part = zip
        .open_part("xl/_rels/workbook.xml.rels")?
        .ok_or_else(|| {
            "It's missing xl/_rels/workbook.xml.rels, which says where its sheets are: save it \
         again as an Excel Workbook (.xlsx)."
                .to_owned()
        })?;
    let mut xml = Xml::new(part);
    let mut relationships = BTreeMap::new();
    loop {
        match xml.next()? {
            Event::Start(e) if e.local_name().as_ref() == "Relationship" => {
                if attribute(&e, "TargetMode").as_deref() == Some("External") {
                    continue;
                }
                if relationships.len() >= MAX_RELATIONSHIPS {
                    break;
                }
                let id = attribute(&e, "Id").unwrap_or_default();
                let kind = attribute(&e, "Type").unwrap_or_default();
                let target = resolve(&attribute(&e, "Target").unwrap_or_default());
                relationships.insert(id, Relationship { kind, target });
            }
            Event::Eof => break,
            _ => {}
        }
    }
    Ok(relationships)
}

/// A relationship's target as a part name: from `xl/` unless it's absolute.
fn resolve(target: &str) -> String {
    let (base, path) = match target.strip_prefix('/') {
        Some(absolute) => ("", absolute),
        None => ("xl", target),
    };
    let mut parts: Vec<&str> = base.split('/').filter(|p| !p.is_empty()).collect();
    for piece in path.split('/') {
        match piece {
            "" | "." => {}
            ".." => {
                parts.pop();
            }
            p => parts.push(p),
        }
    }
    parts.join("/")
}

/// An attribute's value by its name (without a prefix), its entities resolved.
fn attribute(e: &BytesStart<'_>, name: &str) -> Option<String> {
    e.attributes().flatten().find_map(|a| {
        (a.key.as_ref() == name)
            .then(|| {
                a.normalized_value(XmlVersion::Implicit1_0)
                    .ok()
                    .map(std::borrow::Cow::into_owned)
            })
            .flatten()
    })
}

/// A sheet's `r:id`, whatever its prefix.
fn relationship_id(e: &BytesStart<'_>) -> Option<String> {
    e.attributes().flatten().find_map(|a| {
        (a.key.prefix().is_some() && a.key.local_name().as_ref() == "id")
            .then(|| {
                a.normalized_value(XmlVersion::Implicit1_0)
                    .ok()
                    .map(std::borrow::Cow::into_owned)
            })
            .flatten()
    })
}

/// A part's XML, read event by event, refusing a document type.
struct Xml<'a> {
    reader: quick_xml::Reader<BufReader<Part<'a>>>,
    buf: Vec<u8>,
    name: String,
}

impl<'a> Xml<'a> {
    fn new(part: Part<'a>) -> Self {
        let name = part.name.clone();
        let mut reader = quick_xml::Reader::from_reader(BufReader::with_capacity(64 * 1024, part));
        let config = reader.config_mut();
        config.expand_empty_elements = true;
        config.trim_text(false);
        Self {
            reader,
            buf: Vec::new(),
            name,
        }
    }

    fn next(&mut self) -> Result<Event<'_>, String> {
        self.buf.clear();
        let event = self.reader.read_event_into(&mut self.buf);
        match event {
            Ok(Event::DocType(_)) => Err(
                "Its XML declares a document type, which workbooks don't: it isn't a workbook \
                 Iki reads."
                    .to_owned(),
            ),
            Ok(event) => Ok(event),
            Err(error) => {
                let position = self.reader.error_position();
                let failure = self.reader.get_mut().get_mut().failure.take();
                Err(failure.unwrap_or_else(|| {
                    format!(
                        "It's damaged: its part {}'s XML is broken at byte {}: {error}.",
                        self.name,
                        thousands(usize::try_from(position).unwrap_or(usize::MAX))
                    )
                }))
            }
        }
    }
}

/// Text collected from XML, kept to one more character than a cell shows, so a longer
/// one is still known to be longer.
#[derive(Default)]
struct Text {
    text: String,
    chars: usize,
}

impl Text {
    fn push(&mut self, s: &str) {
        for c in s.chars() {
            if self.chars > MAX_CELL_CHARS {
                return;
            }
            self.text.push(c);
            self.chars += 1;
        }
    }

    fn push_ref(&mut self, name: &str) {
        if let Some(code) = name.strip_prefix('#') {
            let value = match code.strip_prefix('x') {
                Some(hex) => u32::from_str_radix(hex, 16).ok(),
                None => code.parse().ok(),
            };
            match value.and_then(char::from_u32) {
                Some(c) => self.push(c.encode_utf8(&mut [0; 4])),
                None => self.push(&format!("&{name};")),
            }
        } else if let Some(text) = quick_xml::escape::resolve_xml_entity(name) {
            self.push(text);
        } else {
            self.push(&format!("&{name};"));
        }
    }
}

/// Excel's escapes for characters XML can't hold, `_x000D_` for a carriage return, as
/// calamine undoes them in strings (two hex digits after `_x00`).
fn unescape_excel(text: &str) -> String {
    if !text.contains("_x00") {
        return text.to_owned();
    }
    let bytes = text.as_bytes();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes.get(i..i + 4) == Some(b"_x00") && bytes.get(i + 6) == Some(&b'_') {
            let hex = text
                .get(i + 4..i + 6)
                .and_then(|h| u8::from_str_radix(h, 16).ok());
            if let Some(code) = hex {
                out.push(char::from(code));
                i += 7;
                continue;
            }
        }
        match text.get(i..).and_then(|rest| rest.chars().next()) {
            Some(c) => {
                out.push(c);
                i += c.len_utf8();
            }
            None => break,
        }
    }
    out
}

/// A cell as the sheet holds it, before its shared string or number format is looked up.
#[derive(Debug, Clone)]
enum Raw {
    Shared(usize),
    Text(String),
    Number(f64, Option<usize>),
    Bool(bool),
    Error(String),
}

struct RawRow {
    number: u32,
    cells: Vec<(u32, Raw)>,
}

/// The rows read from a sheet.
struct SheetRows {
    rows: Vec<RawRow>,
    more_rows: bool,
    /// The shared strings its cells use.
    shared: BTreeSet<usize>,
    /// Whether any number has a cell format, so the styles are needed.
    styled: bool,
    dimension: Option<(u32, u32, u32, u32)>,
}

/// A cell reference's column and row (either may be absent), as `AB12`.
fn cell_ref(text: &str) -> (Option<u32>, Option<u32>) {
    let letters = text.chars().take_while(char::is_ascii_alphabetic).count();
    let mut column: Option<u32> = None;
    for c in text[..letters].chars() {
        let digit = u32::from(c.to_ascii_uppercase()) - u32::from('A') + 1;
        column = Some(
            column
                .unwrap_or(0)
                .saturating_mul(26)
                .saturating_add(digit)
                .min(u32::MAX - 1),
        );
    }
    let digits = &text[letters..];
    let row = if digits.is_empty() {
        None
    } else {
        Some(digits.parse::<u32>().unwrap_or(u32::MAX))
    };
    (column, row)
}

/// A column's letters, as `AB`.
fn column_name(mut column: u32) -> String {
    let mut letters = Vec::new();
    while column > 0 {
        let rest = (column - 1) % 26;
        letters.push(char::from(b'A' + u8::try_from(rest).unwrap_or(0)));
        column = (column - 1) / 26;
    }
    letters.iter().rev().collect()
}

#[derive(PartialEq, Eq, Clone, Copy)]
enum Collecting {
    None,
    Value,
    Inline,
}

/// The cell being read.
struct CellState {
    column: u32,
    kind: Option<String>,
    style: Option<usize>,
    value: Option<Text>,
    inline: Option<Text>,
    /// The `t` being read in `is`, and whether it keeps its spaces.
    run: Option<(Text, bool)>,
}

#[allow(clippy::too_many_lines)] // One pass over the sheet's XML, as a state machine.
fn read_sheet(
    part: Part<'_>,
    max_rows: usize,
    problems: &mut Problems,
) -> Result<SheetRows, String> {
    let mut xml = Xml::new(part);
    let mut out = SheetRows {
        rows: Vec::new(),
        more_rows: false,
        shared: BTreeSet::new(),
        styled: false,
        dimension: None,
    };
    let mut in_data = false;
    let mut row: Option<RawRow> = None;
    let mut last_row = 0u32;
    let mut last_column = 0u32;
    let mut cell: Option<CellState> = None;
    let mut collecting = Collecting::None;
    let mut in_phonetic = false;
    let mut first_row: Option<u32> = None;
    loop {
        match xml.next()? {
            Event::Start(e) => match e.local_name().as_ref() {
                "dimension" if !in_data => {
                    if let Some(range) = attribute(&e, "ref") {
                        let mut ends = range.split(':');
                        let (c1, r1) = cell_ref(ends.next().unwrap_or(""));
                        let (c2, r2) = ends.next().map_or((c1, r1), cell_ref);
                        if let (Some(c1), Some(r1), Some(c2), Some(r2)) = (c1, r1, c2, r2) {
                            out.dimension = Some((c1, r1, c2, r2));
                        }
                    }
                }
                "sheetData" => in_data = true,
                "row" if in_data => {
                    let number = attribute(&e, "r")
                        .and_then(|r| r.trim().parse::<u32>().ok())
                        .unwrap_or(last_row.saturating_add(1));
                    last_column = 0;
                    row = Some(RawRow {
                        number,
                        cells: Vec::new(),
                    });
                }
                "c" if row.is_some() => {
                    let column = attribute(&e, "r")
                        .and_then(|r| cell_ref(&r).0)
                        .unwrap_or(last_column.saturating_add(1));
                    let style = attribute(&e, "s").map(|s| s.trim().parse::<usize>().unwrap_or(0));
                    cell = Some(CellState {
                        column,
                        kind: attribute(&e, "t"),
                        style,
                        value: None,
                        inline: None,
                        run: None,
                    });
                }
                "v" if cell.is_some() => {
                    if let Some(c) = cell.as_mut() {
                        c.value = Some(Text::default());
                    }
                    collecting = Collecting::Value;
                }
                "is" if cell.is_some() => {
                    if let Some(c) = cell.as_mut() {
                        c.inline = Some(Text::default());
                    }
                }
                "rPh" => in_phonetic = true,
                "t" if cell.as_ref().is_some_and(|c| c.inline.is_some()) && !in_phonetic => {
                    if let Some(c) = cell.as_mut() {
                        c.run = Some((Text::default(), preserves(&e)));
                    }
                    collecting = Collecting::Inline;
                }
                _ => {}
            },
            Event::Text(t) => {
                let text = t.xml10_content();
                append(&mut cell, collecting, &text, None);
            }
            Event::CData(t) => {
                let text = t.xml10_content();
                append(&mut cell, collecting, &text, None);
            }
            Event::GeneralRef(r) => {
                let name = r.into_inner();
                append(&mut cell, collecting, "", Some(&name));
            }
            Event::End(e) => match e.local_name().as_ref() {
                "v" => collecting = Collecting::None,
                "t" => {
                    if collecting == Collecting::Inline
                        && let Some(c) = cell.as_mut()
                        && let (Some((run, preserve)), Some(inline)) =
                            (c.run.take(), c.inline.as_mut())
                    {
                        inline.push(&finish_run(&run.text, preserve));
                    }
                    collecting = Collecting::None;
                }
                "rPh" => in_phonetic = false,
                "c" => {
                    if let (Some(done), Some(current)) = (cell.take(), row.as_mut()) {
                        let column = done.column;
                        if column > LAST_COLUMN {
                            problems.add(format!(
                                "Row {} has a cell past Excel's last column (XFD), so it was \
                                 left out.",
                                thousands(current.number as usize)
                            ));
                        } else if column <= last_column {
                            problems.add(format!(
                                "Cell {}{} comes after a cell to its right in the sheet's XML, \
                                 so it was left out.",
                                column_name(column),
                                current.number
                            ));
                        } else {
                            last_column = column;
                            if let Some(raw) = finish_cell(done, problems) {
                                current.cells.push((column, raw));
                            }
                        }
                    }
                }
                "row" => {
                    let Some(done) = row.take() else { continue };
                    let number = done.number;
                    if number == 0 || number > LAST_ROW {
                        problems.add(format!(
                            "The sheet has a row numbered {}, past Excel's last (1,048,576), so \
                             it was left out.",
                            thousands(number as usize)
                        ));
                        continue;
                    }
                    if number <= last_row {
                        problems.add(format!(
                            "Row {} comes after row {} in the sheet's XML, so it was left out.",
                            thousands(number as usize),
                            thousands(last_row as usize)
                        ));
                        continue;
                    }
                    last_row = number;
                    if done.cells.iter().all(|(_, raw)| is_blank(raw)) {
                        continue;
                    }
                    let first = *first_row.get_or_insert(number);
                    if (number - first) as usize >= max_rows {
                        out.more_rows = true;
                        break;
                    }
                    out.rows.push(done);
                }
                "sheetData" => break,
                _ => {}
            },
            Event::Eof => {
                if in_data {
                    problems.add("The sheet's XML ends part way through its rows.".to_owned());
                }
                break;
            }
            _ => {}
        }
    }
    // Only the shared strings and styles of the rows kept are needed.
    for (_, raw) in out.rows.iter().flat_map(|r| &r.cells) {
        match raw {
            Raw::Shared(i) => {
                out.shared.insert(*i);
            }
            Raw::Number(_, Some(_)) => out.styled = true,
            _ => {}
        }
    }
    Ok(out)
}

fn append(cell: &mut Option<CellState>, collecting: Collecting, text: &str, entity: Option<&str>) {
    let Some(cell) = cell.as_mut() else { return };
    let target = match collecting {
        Collecting::None => return,
        Collecting::Value => cell.value.as_mut(),
        Collecting::Inline => cell.run.as_mut().map(|(text, _)| text),
    };
    if let Some(target) = target {
        match entity {
            Some(name) => target.push_ref(name),
            None => target.push(text),
        }
    }
}

/// Whether a `t` keeps its spaces (`xml:space="preserve"`).
fn preserves(e: &BytesStart<'_>) -> bool {
    attribute(e, "xml:space").as_deref() == Some("preserve")
}

/// A string's `t` as calamine reads it: trimmed of ASCII spaces, tabs and line ends
/// unless it keeps its spaces, then Excel's escapes undone.
fn finish_run(text: &str, preserve: bool) -> String {
    if preserve {
        unescape_excel(text)
    } else {
        unescape_excel(text.trim_matches([' ', '\t', '\r', '\n']))
    }
}

fn is_blank(raw: &Raw) -> bool {
    match raw {
        Raw::Text(t) | Raw::Error(t) => t.is_empty(),
        _ => false,
    }
}

/// A cell's value, as calamine reads it from its type (`t`) and value (`v` or `is`).
fn finish_cell(cell: CellState, problems: &mut Problems) -> Option<Raw> {
    if let Some(inline) = cell.inline {
        return Some(Raw::Text(inline.text));
    }
    if matches!(cell.kind.as_deref(), Some("inlineStr" | "is")) {
        // calamine ignores an inline string's `v`.
        return None;
    }
    let value = cell.value?.text;
    Some(match cell.kind.as_deref() {
        Some("s" | "b" | "n") | None if value.is_empty() => return None,
        // As calamine, a shared string index that isn't a number is the first string.
        Some("s") => Raw::Shared(value.trim().parse().unwrap_or(0)),
        Some("b") => match value.trim() {
            "" => return None,
            "0" => Raw::Bool(false),
            "1" => Raw::Bool(true),
            v if v.eq_ignore_ascii_case("false") => Raw::Bool(false),
            v if v.eq_ignore_ascii_case("true") => Raw::Bool(true),
            _ => {
                problems.add(format!(
                    "A cell holds {value:?} as TRUE or FALSE, which it isn't; it's shown as text."
                ));
                Raw::Text(value)
            }
        },
        Some("e") => Raw::Error(value),
        Some("str") => Raw::Text(unescape_excel(&value)),
        Some("d") => Raw::Text(value),
        Some("n") => {
            if let Ok(n) = value.trim().parse::<f64>() {
                Raw::Number(n, cell.style)
            } else {
                problems.add(format!(
                    "A cell holds {value:?} as a number, which it isn't; it's shown as text."
                ));
                Raw::Text(value)
            }
        }
        None => match value.trim().parse::<f64>() {
            Ok(n) => Raw::Number(n, cell.style),
            Err(_) => Raw::Text(value),
        },
        Some(other) => {
            problems.add(format!(
                "A cell has a type ({other}) workbooks don't use; it's shown as text."
            ));
            Raw::Text(value)
        }
    })
}

/// The shared strings needed, by index, and how many there are if all were read.
#[derive(Default)]
struct Strings {
    found: BTreeMap<usize, String>,
    /// How many there are, once read to the end.
    count: Option<usize>,
}

fn read_strings(part: Part<'_>, needed: &BTreeSet<usize>) -> Result<Strings, String> {
    let last = needed.iter().next_back().copied().unwrap_or(0);
    let mut xml = Xml::new(part);
    let mut strings = Strings::default();
    let mut index = 0usize;
    let mut current: Option<Text> = None;
    let mut run: Option<(Text, bool)> = None;
    let mut in_t = false;
    let mut in_phonetic = false;
    loop {
        match xml.next()? {
            Event::Start(e) => match e.local_name().as_ref() {
                "si" => {
                    if index > last || index >= MAX_SHARED_STRINGS {
                        return Ok(strings);
                    }
                    current = needed.contains(&index).then(Text::default);
                }
                "rPh" => in_phonetic = true,
                "t" if !in_phonetic => {
                    in_t = true;
                    run = Some((Text::default(), preserves(&e)));
                }
                _ => {}
            },
            Event::Text(t) if in_t && current.is_some() => {
                if let Some((text, _)) = run.as_mut() {
                    text.push(&t.xml10_content());
                }
            }
            Event::CData(t) if in_t && current.is_some() => {
                if let Some((text, _)) = run.as_mut() {
                    text.push(&t.xml10_content());
                }
            }
            Event::GeneralRef(r) if in_t && current.is_some() => {
                if let Some((text, _)) = run.as_mut() {
                    text.push_ref(&r.into_inner());
                }
            }
            Event::End(e) => match e.local_name().as_ref() {
                "t" if in_t => {
                    in_t = false;
                    if let (Some((text, preserve)), Some(string)) = (run.take(), current.as_mut()) {
                        string.push(&finish_run(&text.text, preserve));
                    }
                }
                "rPh" => in_phonetic = false,
                "si" => {
                    if let Some(text) = current.take() {
                        strings.found.insert(index, text.text);
                    }
                    index += 1;
                }
                _ => {}
            },
            Event::Eof => {
                strings.count = Some(index);
                return Ok(strings);
            }
            _ => {}
        }
    }
}

/// How a number is shown: as calamine classes a cell format.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Format {
    Other,
    DateTime,
    TimeDelta,
}

/// Each cell format's (`cellXfs`) class, in order.
fn read_formats(part: Part<'_>) -> Result<Vec<Format>, String> {
    let mut xml = Xml::new(part);
    let mut custom: BTreeMap<String, String> = BTreeMap::new();
    let mut formats = Vec::new();
    let mut in_xfs = false;
    loop {
        match xml.next()? {
            Event::Start(e) => match e.local_name().as_ref() {
                "numFmt" => {
                    let id = attribute(&e, "numFmtId").unwrap_or_default();
                    let code = attribute(&e, "formatCode").unwrap_or_default();
                    if !code.is_empty() && custom.len() < MAX_FORMATS {
                        custom.insert(id, code);
                    }
                }
                "cellXfs" => in_xfs = true,
                "xf" if in_xfs && formats.len() < MAX_FORMATS => {
                    formats.push(attribute(&e, "numFmtId").map_or(Format::Other, |id| {
                        custom
                            .get(&id)
                            .map_or_else(|| builtin_format(&id), |code| custom_format(code))
                    }));
                }
                _ => {}
            },
            Event::End(e) if e.local_name().as_ref() == "cellXfs" => in_xfs = false,
            Event::Eof => return Ok(formats),
            _ => {}
        }
    }
}

/// calamine's `builtin_format_by_id` (0.37, as python-calamine 0.9.1 builds on).
fn builtin_format(id: &str) -> Format {
    match id {
        "14" | "15" | "16" | "17" | "18" | "19" | "20" | "21" | "22" | "45" | "47" => {
            Format::DateTime
        }
        // East Asian dates.
        "27" | "28" | "29" | "30" | "31" | "32" | "33" | "34" | "35" | "36" | "50" | "51"
        | "52" | "53" | "54" | "55" | "56" | "57" | "58" => Format::DateTime,
        "46" => Format::TimeDelta,
        _ => Format::Other,
    }
}

/// calamine's `detect_custom_number_format` (0.37), step for step: a date or time token (d, m,
/// h, y, s) in the first section outside quotes, escapes and brackets, or an elapsed time
/// in brackets (`[h]`).
fn custom_format(format: &str) -> Format {
    let mut escaped = false;
    let mut quoted = false;
    let mut brackets = 0u8;
    let mut prev = ' ';
    let mut hms = false;
    let mut ap = false;
    for s in format.chars() {
        if escaped {
            escaped = false;
        } else if s == '_' || s == '\\' || s == '*' {
            escaped = true;
        } else if quoted {
            if s == '"' {
                quoted = false;
            }
        } else if s == '"' {
            quoted = true;
        } else if s == ';' {
            return Format::Other;
        } else if s == '[' {
            brackets = brackets.saturating_add(1);
        } else if s == ']' && brackets == 1 && hms {
            return Format::TimeDelta;
        } else if s == ']' {
            brackets = brackets.saturating_sub(1);
        } else if matches!(s, 'a' | 'A') && !ap && brackets == 0 {
            ap = true;
        } else if brackets == 0
            && ((ap && matches!(s, 'p' | 'm' | '/' | 'P' | 'M'))
                || (!ap && matches!(s, 'd' | 'm' | 'h' | 'y' | 's' | 'D' | 'M' | 'H' | 'Y' | 'S')))
        {
            return Format::DateTime;
        } else if !(hms && s.eq_ignore_ascii_case(&prev)) {
            hms = prev == '[' && matches!(s, 'm' | 'h' | 's' | 'M' | 'H' | 'S');
        }
        prev = s;
    }
    Format::Other
}

#[allow(clippy::too_many_arguments, clippy::too_many_lines)] // The sheet's rows to a table.
fn lay_out(
    read: &SheetRows,
    strings: &Strings,
    formats: &[Format],
    date1904: bool,
    max_columns: usize,
    sheets: Vec<Sheet>,
    sheet: usize,
    mut problems: Problems,
) -> Table {
    let first_row = read.rows.first().map_or(1, |r| r.number);
    let last_row = read.rows.last().map_or(first_row, |r| r.number);
    let cells = read
        .rows
        .iter()
        .flat_map(|r| r.cells.iter().map(|(c, _)| *c));
    let first_column = cells.clone().min().unwrap_or(1);
    let last_column = cells.max().unwrap_or(0);
    let columns = if read.rows.is_empty() {
        0
    } else {
        (last_column - first_column + 1) as usize
    };
    let width = columns.min(max_columns.min(COLUMN_LIMIT));
    let mut height = if read.rows.is_empty() {
        0
    } else {
        (last_row - first_row + 1) as usize
    };
    let mut fewer_rows = false;
    if width > 0 && height.saturating_mul(width) > MAX_GRID_CELLS {
        height = MAX_GRID_CELLS / width;
        fewer_rows = true;
    }
    if let Some((c1, r1, c2, r2)) = read.dimension {
        let outside = read.rows.iter().any(|r| {
            r.number < r1.min(r2)
                || r.number > r1.max(r2)
                || r.cells
                    .iter()
                    .any(|(c, _)| *c < c1.min(c2) || *c > c1.max(c2))
        });
        if outside {
            problems.add(format!(
                "Its dimension says {}{}:{}{}, but it has cells outside it.",
                column_name(c1),
                r1,
                column_name(c2),
                r2
            ));
        }
    }
    let mut rows = vec![vec![String::new(); width]; height];
    for raw_row in &read.rows {
        let Some(row) = rows.get_mut((raw_row.number - first_row) as usize) else {
            break;
        };
        for (column, raw) in &raw_row.cells {
            let Some(slot) = row.get_mut((column - first_column) as usize) else {
                continue;
            };
            *slot = match raw {
                Raw::Shared(i) => {
                    if let Some(text) = strings.found.get(i) {
                        text.clone()
                    } else {
                        let have = match strings.count {
                            Some(n) => format!("the workbook has {}", thousands(n)),
                            None if *i >= MAX_SHARED_STRINGS => {
                                format!("only the first {} are read", thousands(MAX_SHARED_STRINGS))
                            }
                            None => "the workbook has fewer".to_owned(),
                        };
                        problems.add(format!(
                            "Cell {}{} refers to shared string {}, but {have}.",
                            column_name(*column),
                            raw_row.number,
                            thousands(*i)
                        ));
                        String::new()
                    }
                }
                Raw::Text(t) | Raw::Error(t) => t.clone(),
                Raw::Bool(b) => if *b { "TRUE" } else { "FALSE" }.to_owned(),
                Raw::Number(n, style) => {
                    let format = match style {
                        None => Some(Format::Other),
                        Some(s) => formats.get(*s).copied(),
                    };
                    match format {
                        Some(Format::DateTime) => {
                            date_text(*n, date1904).unwrap_or_else(|| number_text(*n))
                        }
                        Some(Format::TimeDelta) => duration_text(*n),
                        _ => number_text(*n),
                    }
                }
            };
        }
    }
    let shown = grid(rows, width);
    Table {
        sheets,
        sheet,
        rows: shown.rows,
        first_row,
        first_column: if read.rows.is_empty() {
            1
        } else {
            first_column
        },
        columns,
        more_rows: read.more_rows || fewer_rows,
        more_columns: columns > width,
        cut_cells: shown.cut_cells,
        encoding: "UTF-8".to_owned(),
        problems: problems.finish(),
    }
}

/// A number as the importer's `text_of` writes it: a whole number under 10^15 without a
/// decimal point, otherwise Python's `repr`, its shortest round-trip form (`0.1`,
/// `1.5e-10`, `1e+16`).
#[allow(clippy::cast_possible_truncation, clippy::float_cmp)]
pub(super) fn number_text(v: f64) -> String {
    if v.is_finite() && v.fract() == 0.0 && v.abs() < 1e15 {
        return format!("{}", v as i64);
    }
    python_repr(v)
}

/// Python's `repr` of a float.
fn python_repr(v: f64) -> String {
    if v.is_nan() {
        return "nan".to_owned();
    }
    if v.is_infinite() {
        return if v > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    if v == 0.0 {
        return if v.is_sign_negative() { "-0.0" } else { "0.0" }.to_owned();
    }
    let scientific = format!("{:e}", v.abs());
    let (mantissa, exponent) = scientific.split_once('e').unwrap_or((&scientific, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let sign = if v < 0.0 { "-" } else { "" };
    if (-4..16).contains(&exponent) {
        let point = usize::try_from(exponent + 1).unwrap_or(0);
        if exponent >= 0 {
            let mut whole = digits.clone();
            while whole.len() < point {
                whole.push('0');
            }
            let (int, frac) = whole.split_at(point);
            let frac = if frac.is_empty() { "0" } else { frac };
            format!("{sign}{int}.{frac}")
        } else {
            let zeros = "0".repeat(usize::try_from(-exponent - 1).unwrap_or(0));
            format!("{sign}0.{zeros}{digits}")
        }
    } else {
        let exp_sign = if exponent < 0 { '-' } else { '+' };
        format!("{sign}{mantissa}e{exp_sign}{:02}", exponent.abs())
    }
}

const DAY_MS: i64 = 86_400_000;

/// Days from 1970-01-01 to a civil date (Howard Hinnant's algorithm).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// The civil date of days from 1970-01-01.
fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn clock(ms: i64) -> String {
    let (h, m, s, rest) = (ms / 3_600_000, ms / 60_000 % 60, ms / 1000 % 60, ms % 1000);
    if rest == 0 {
        format!("{h:02}:{m:02}:{s:02}")
    } else {
        format!("{h:02}:{m:02}:{s:02}.{:06}", rest * 1000)
    }
}

/// A date serial as the importer writes it, through calamine and Python: from 1899-12-30
/// (1900 system, where serials under 60 count from a day later, so both 59 and 60, Lotus's
/// 1900-02-29, are 1900-02-28) or 1904-01-01, to the millisecond; a serial under 1 is a time
/// of day (`HH:MM:SS`), a whole day a date (`YYYY-MM-DD`), else `YYYY-MM-DDTHH:MM:SS`, with
/// microseconds where there are any. `None` past the years 1 to 9999.
pub(super) fn date_text(serial: f64, date1904: bool) -> Option<String> {
    let mut f = if date1904 { serial + 1462.0 } else { serial };
    if f < 60.0 {
        f += 1.0;
    }
    let ms = f * 86_400_000.0;
    if !ms.is_finite() || ms.abs() > 1e18 {
        return None;
    }
    #[allow(clippy::cast_possible_truncation)]
    let ms = ms.round() as i64;
    let days = ms.div_euclid(DAY_MS);
    let of_day = ms.rem_euclid(DAY_MS);
    let (y, m, d) = civil_from_days(days_from_civil(1899, 12, 30) + days);
    if !(1..=9999).contains(&y) {
        return None;
    }
    Some(if serial < 1.0 {
        clock(of_day)
    } else if of_day == 0 {
        format!("{y:04}-{m:02}-{d:02}")
    } else {
        format!("{y:04}-{m:02}-{d:02}T{}", clock(of_day))
    })
}

/// An elapsed time (`[h]:mm:ss`) as Python writes a `timedelta`: `1 day, 12:00:00`.
pub(super) fn duration_text(serial: f64) -> String {
    let ms = serial * 86_400_000.0;
    if !ms.is_finite() || ms.abs() > 1e17 {
        return number_text(serial);
    }
    #[allow(clippy::cast_possible_truncation)]
    let ms = ms.round() as i64;
    let days = ms.div_euclid(DAY_MS);
    let rest = ms.rem_euclid(DAY_MS);
    let (h, m, s, frac) = (
        rest / 3_600_000,
        rest / 60_000 % 60,
        rest / 1000 % 60,
        rest % 1000,
    );
    let mut out = String::new();
    if days != 0 {
        out = format!("{days} day{}, ", if days.abs() == 1 { "" } else { "s" });
    }
    let _ = write!(out, "{h}:{m:02}:{s:02}");
    if frac != 0 {
        let _ = write!(out, ".{:06}", frac * 1000);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numbers_as_python_writes_them() {
        for (v, text) in [
            (1.0, "1"),
            (0.1, "0.1"),
            (1.5e-10, "1.5e-10"),
            (1e16, "1e+16"),
            (1e15, "1000000000000000.0"),
            (-2.5, "-2.5"),
            (123.456, "123.456"),
            (1e-5, "1e-05"),
            (0.0001, "0.0001"),
            (-0.0, "0"),
            (1.234_567_890_123_456_8e17, "1.2345678901234568e+17"),
            (999_999_999_999_999.0, "999999999999999"),
        ] {
            assert_eq!(number_text(v), text, "{v}");
        }
    }

    #[test]
    fn dates_as_calamine_reads_them() {
        // Checked against python-calamine 0.9.1 reading openpyxl's workbooks.
        assert_eq!(date_text(0.0, false).as_deref(), Some("00:00:00"));
        assert_eq!(date_text(0.5, false).as_deref(), Some("12:00:00"));
        assert_eq!(date_text(1.0, false).as_deref(), Some("1900-01-01"));
        assert_eq!(date_text(59.0, false).as_deref(), Some("1900-02-28"));
        assert_eq!(date_text(60.0, false).as_deref(), Some("1900-02-28"));
        assert_eq!(date_text(61.0, false).as_deref(), Some("1900-03-01"));
        assert_eq!(
            date_text(1.5, false).as_deref(),
            Some("1900-01-01T12:00:00")
        );
        assert_eq!(
            date_text(45_000.123_456_789, false).as_deref(),
            Some("2023-03-15T02:57:46.667000")
        );
        assert_eq!(
            date_text(2_958_465.999_99, false).as_deref(),
            Some("9999-12-31T23:59:59.136000")
        );
        assert_eq!(date_text(1.0, true).as_deref(), Some("1904-01-02"));
        assert_eq!(
            date_text(1.25, true).as_deref(),
            Some("1904-01-02T06:00:00")
        );
        assert_eq!(date_text(-1.0, true).as_deref(), Some("00:00:00"));
        assert_eq!(date_text(2_958_465.0, true), None);
        assert_eq!(duration_text(1.5), "1 day, 12:00:00");
        assert_eq!(duration_text(0.25), "6:00:00");
        assert_eq!(duration_text(61.0), "61 days, 0:00:00");
    }

    #[test]
    fn formats_as_calamine_classes_them() {
        assert_eq!(custom_format("yyyy-mm-dd"), Format::DateTime);
        assert_eq!(custom_format("[h]:mm:ss"), Format::TimeDelta);
        assert_eq!(custom_format("0.00"), Format::Other);
        assert_eq!(custom_format("#,##0 \"days\""), Format::Other);
        assert_eq!(custom_format("[Red]0.00"), Format::Other);
        assert_eq!(custom_format("0.00;[Red]-0.00"), Format::Other);
        assert_eq!(custom_format("General"), Format::Other);
        assert_eq!(custom_format("h:mm AM/PM"), Format::DateTime);
        assert_eq!(builtin_format("14"), Format::DateTime);
        assert_eq!(builtin_format("46"), Format::TimeDelta);
        assert_eq!(builtin_format("2"), Format::Other);
    }

    #[test]
    fn references() {
        assert_eq!(cell_ref("A1"), (Some(1), Some(1)));
        assert_eq!(cell_ref("AB12"), (Some(28), Some(12)));
        assert_eq!(cell_ref("XFD1048576"), (Some(16_384), Some(1_048_576)));
        assert_eq!(column_name(16_384), "XFD");
        assert_eq!(column_name(28), "AB");
        assert_eq!(resolve("worksheets/sheet1.xml"), "xl/worksheets/sheet1.xml");
        assert_eq!(
            resolve("/xl/worksheets/sheet2.xml"),
            "xl/worksheets/sheet2.xml"
        );
        assert_eq!(resolve("../xl/a.xml"), "xl/a.xml");
        assert_eq!(unescape_excel("a_x000D_b_x005F_x000D_"), "a\rb_x000D_");
    }
}
