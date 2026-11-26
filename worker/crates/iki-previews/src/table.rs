//! Tables: CSV and TSV (RFC 4180, with the delimiter given), and Office Open XML workbooks
//! (XLSX and XLSM, ECMA-376), read by hand: the ZIP's central directory, then the workbook's
//! sheets, shared strings and styles, inflating no part past a bound, so a workbook that
//! inflates to gigabytes (a ZIP bomb) stops at its first rows. Macros are never run: they're
//! a part this doesn't open.

use serde::Serialize;

/// The most characters a cell keeps; longer ones end in `…` and are counted.
pub const MAX_CELL_CHARS: usize = 10_000;
/// The most bytes any one workbook part may inflate to.
pub const MAX_PART_BYTES: usize = 256 * 1024 * 1024;
/// The most shared strings read.
pub const MAX_SHARED_STRINGS: usize = 2_000_000;

/// The first rows of a table.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Table {
    /// A workbook's sheets, in its order; empty for CSV.
    pub sheets: Vec<Sheet>,
    /// Which sheet this is, from 0.
    pub sheet: usize,
    /// Its rows, each its cells as text: numbers as written (CSV) or in their shortest
    /// round-trip form (XLSX), dates and times in ISO 8601, booleans `TRUE` and `FALSE`,
    /// errors as Excel writes them (`#N/A`), and a formula's cached value. Rows are padded
    /// to the same length, at most `max_columns`.
    pub rows: Vec<Vec<String>>,
    /// The sheet's row number (from 1) of `rows[0]`: a sheet's data can start further down.
    pub first_row: u32,
    /// Its column number (from 1) of each row's first cell.
    pub first_column: u32,
    /// How many columns the widest row has, before the limit.
    pub columns: usize,
    /// Whether rows were left out: past `max_rows`, or past the bytes given (CSV).
    pub more_rows: bool,
    /// Whether columns were left out past `max_columns`.
    pub more_columns: bool,
    /// How many cells were cut at [`MAX_CELL_CHARS`].
    pub cut_cells: usize,
    /// How the text was decoded: `UTF-8`, or `windows-1252` for CSV that isn't UTF-8.
    pub encoding: String,
    /// Things worth saying that didn't stop the read, such as a row with more cells than
    /// the header (CSV), or a sheet's dimension it didn't keep to.
    pub problems: Vec<String>,
}

/// A workbook's sheet.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Sheet {
    /// Its name.
    pub name: String,
    /// Whether it's hidden (`hidden` or `veryHidden` in the workbook).
    pub hidden: bool,
}

/// The first rows of CSV or TSV. `complete` says whether `bytes` are the whole file; if not,
/// the last record, which may be cut part way, is left out.
///
/// The text is read as the server's importer reads it: UTF-8 (with or without its byte
/// order mark), UTF-16 with its mark, or else Windows-1252. A `delimiter` of 0 means find
/// it as the importer does, among comma, semicolon, tab and bar. At most 1,000,000 rows
/// and 16,384 columns are kept whatever is asked, and at most 2,000,000 cells in all.
///
/// # Errors
///
/// A message for a person if it can't be read as delimited text.
pub fn csv(
    bytes: &[u8],
    delimiter: u8,
    max_rows: usize,
    max_columns: usize,
    complete: bool,
) -> Result<Table, String> {
    text::read(bytes, delimiter, max_rows, max_columns, complete)
}

/// The first rows of a workbook's sheet `sheet` (from 0), with its sheets.
///
/// Sheets are the workbook's worksheets, as the importer lists them (chart sheets aren't
/// counted). `first_row` and `first_column` are the first row and column with anything in
/// them: rows and columns before are left out, not padded. At most 1,000,000 rows and
/// 16,384 columns are kept whatever is asked, and at most 2,000,000 cells in all.
///
/// # Errors
///
/// A message for a person if it isn't an XLSX workbook, it's encrypted, or the sheet
/// doesn't exist.
pub fn xlsx(
    bytes: &[u8],
    sheet: usize,
    max_rows: usize,
    max_columns: usize,
) -> Result<Table, String> {
    workbook::read(bytes, sheet, max_rows, max_columns)
}

mod text;
mod workbook;
mod zip;

/// The most rows kept, whatever is asked.
const ROW_LIMIT: usize = 1_000_000;
/// The most columns kept, whatever is asked: Excel's 16,384.
const COLUMN_LIMIT: usize = 16_384;
/// The most cells kept in all, rows times columns.
const MAX_GRID_CELLS: usize = 2_000_000;
/// The most problems listed; past it, one more says how many weren't.
const MAX_PROBLEMS: usize = 20;
/// The longest cell the importer reads (Excel's limit).
const IMPORTER_MAX_CELL: usize = 32_767;
/// The most columns the importer reads.
const IMPORTER_MAX_COLUMNS: usize = 1_000;
/// The OLE compound file signature: an encrypted workbook, or a legacy `.xls` one.
const OLE: &[u8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1";
/// The importer's message for one, word for word.
const OLE_MESSAGE: &str = "It's encrypted, or an Excel 97\u{2013}2003 workbook (.xls). Iki reads \
    .xlsx workbooks without a password: in Excel, remove the password (File > Info > \
    Protect Workbook) and save it as an Excel Workbook (.xlsx).";

/// Problems, bounded.
#[derive(Debug, Default)]
struct Problems {
    listed: Vec<String>,
    more: usize,
}

impl Problems {
    fn add(&mut self, problem: String) {
        if self.listed.len() < MAX_PROBLEMS {
            if !self.listed.contains(&problem) {
                self.listed.push(problem);
            }
        } else {
            self.more += 1;
        }
    }

    fn finish(mut self) -> Vec<String> {
        if self.more > 0 {
            self.listed.push(format!(
                "And {} more problems like these.",
                thousands(self.more)
            ));
        }
        self.listed
    }
}

/// `n` with thousands separators, as people read it.
fn thousands(n: usize) -> String {
    let digits = n.to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// A cell's text cut to [`MAX_CELL_CHARS`] characters and `…`, and whether it was.
fn cut(text: String) -> (String, bool) {
    match text.char_indices().nth(MAX_CELL_CHARS) {
        Some((at, _)) => {
            let mut kept = text;
            kept.truncate(at);
            kept.push('\u{2026}');
            (kept, true)
        }
        None => (text, false),
    }
}

/// The rows kept, padded to the widest and cut to `max_columns` and [`MAX_GRID_CELLS`]:
/// the rows, how wide the widest was, whether columns were cut, whether rows were (for
/// the cells' bound), and how many cells were cut at [`MAX_CELL_CHARS`].
struct Grid {
    rows: Vec<Vec<String>>,
    columns: usize,
    more_columns: bool,
    fewer_rows: bool,
    cut_cells: usize,
}

fn grid(mut rows: Vec<Vec<String>>, max_columns: usize) -> Grid {
    let columns = rows.iter().map(Vec::len).max().unwrap_or(0);
    let width = columns.min(max_columns.min(COLUMN_LIMIT));
    let mut fewer_rows = false;
    if width > 0 && rows.len().saturating_mul(width) > MAX_GRID_CELLS {
        rows.truncate(MAX_GRID_CELLS / width);
        fewer_rows = true;
    }
    let mut cut_cells = 0;
    for row in &mut rows {
        row.truncate(width);
        row.resize(width, String::new());
        for cell in row.iter_mut() {
            let (kept, was_cut) = cut(std::mem::take(cell));
            *cell = kept;
            cut_cells += usize::from(was_cut);
        }
    }
    Grid {
        rows,
        columns,
        more_columns: columns > width,
        fewer_rows,
        cut_cells,
    }
}
