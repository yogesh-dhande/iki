//! JCAMP-DX spectra (IUPAC's JCAMP-DX 4.24 and 5.01): labelled data records, with the
//! spectrum as `##XYDATA=(X++(Y..Y))` in AFFN or the compressed ASDF forms (SQZ, DIF, DUP),
//! `##XYPOINTS=(XY..XY)` or `##PEAK TABLE=(XY..XY)`, in one block or several (`##BLOCKS`).
//!
//! A file is lines of labelled data records, `##LABEL=value`, with `$$` starting a comment.
//! Labels are compared as the specification says: case, spaces, `-`, `/` and `_` ignored, so
//! `##DATA TYPE`, `##DATATYPE` and `##Data_Type` are one label. `##TITLE=` opens a block and
//! `##END=` closes it; a link block (`##BLOCKS=`) holds others. Each data table read is one
//! [`Block`].
//!
//! In `(X++(Y..Y))` tables each line starts with an abscissa, which times `##XFACTOR` is the
//! first point's X; the others step by (`##LASTX` − `##FIRSTX`) / (`##NPOINTS` − 1), or by
//! `##DELTAX`. In DIF form, a line's first Y repeats the last Y of the line before, as a
//! check: it's dropped, and if it doesn't agree that's said, with the line.
//!
//! `##NTUPLES` (as NMR spectra and FIDs are often written): each page whose table is
//! `(X++(Y..Y))` or `(XY..XY)` is read as a block, named for its page (`N=1`). Imaginary
//! parts are left out, as are the pages of 2D spectra, and both are said.

use std::borrow::Cow;

use serde::Serialize;

/// The most bytes a JCAMP-DX file may be to be read.
pub const MAX_BYTES: usize = 64 * 1024 * 1024;
/// The most points read across all its blocks.
pub const MAX_POINTS: usize = 2_000_000;
/// The most blocks read.
pub const MAX_BLOCKS: usize = 64;
/// The longest line read: the specification keeps them to 80 characters, but some writers
/// put a whole peak table on one.
pub const MAX_LINE: usize = 1024 * 1024;
/// How deeply blocks may nest. A link block holding data blocks is two deep.
pub const MAX_DEPTH: usize = 8;
/// The most problems listed; past it, how many more there were.
const MAX_PROBLEMS: usize = 50;

/// A JCAMP-DX file's spectra.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Spectra {
    /// The file's (or its link block's) `##TITLE`.
    pub title: Option<String>,
    /// Its spectra, one per data block.
    pub blocks: Vec<Block>,
    /// Whether blocks or points were left out past the limits.
    pub truncated: bool,
    /// Things worth saying that didn't stop the read, such as a Y check value that didn't
    /// agree, with the line it's on.
    pub problems: Vec<String>,
}

/// One spectrum.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Block {
    /// Its `##TITLE`.
    pub title: Option<String>,
    /// `##DATA TYPE`, such as `INFRARED SPECTRUM` or `MASS SPECTRUM`.
    pub data_type: Option<String>,
    /// `##XUNITS`, such as `1/CM`, `NANOMETERS` or `M/Z`.
    pub x_units: Option<String>,
    /// `##YUNITS`, such as `TRANSMITTANCE`, `ABSORBANCE` or `RELATIVE ABUNDANCE`.
    pub y_units: Option<String>,
    /// `xy` for a continuous spectrum, drawn as a line; `peaks` for a peak table or
    /// `XYPOINTS` of a mass spectrum, drawn as sticks.
    pub kind: BlockKind,
    /// The X values, scaled by `##XFACTOR` and in file order.
    pub x: Vec<f64>,
    /// The Y values, scaled by `##YFACTOR`.
    pub y: Vec<f64>,
    /// How many points the block declares (`##NPOINTS`), if it does.
    pub declared_points: Option<usize>,
}

/// How a block's points are drawn.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BlockKind {
    /// A continuous spectrum.
    Xy,
    /// Separate peaks.
    Peaks,
}

/// Reads a JCAMP-DX file.
///
/// # Errors
///
/// A message for a person if it isn't JCAMP-DX, or has no spectrum Iki can read.
pub fn read(bytes: &[u8]) -> Result<Spectra, String> {
    if bytes.len() > MAX_BYTES {
        return Err(format!(
            "It's {} bytes, bigger than the {} a JCAMP-DX preview reads.",
            count(bytes.len()),
            count(MAX_BYTES)
        ));
    }
    let text = decode(bytes);
    if text.trim().is_empty() {
        return Err("The file is empty.".to_owned());
    }
    let mut reader = Reader::default();
    for (index, line) in lines(&text).enumerate() {
        reader.line(index + 1, line)?;
    }
    reader.finish()
}

/// UTF-8 if it is, else Latin-1, as older instruments wrote.
fn decode(bytes: &[u8]) -> Cow<'_, str> {
    let bytes = bytes.strip_prefix(b"\xEF\xBB\xBF").unwrap_or(bytes);
    match std::str::from_utf8(bytes) {
        Ok(text) => Cow::Borrowed(text),
        Err(_) => Cow::Owned(bytes.iter().map(|byte| char::from(*byte)).collect()),
    }
}

/// Lines ended by LF, CRLF, or (as on old Macs) CR alone.
fn lines(text: &str) -> Box<dyn Iterator<Item = &str> + '_> {
    if text.contains('\n') {
        Box::new(text.split('\n').map(|line| line.trim_end_matches('\r')))
    } else {
        Box::new(text.split('\r'))
    }
}

/// A label as the specification compares them: upper case, without spaces, `-`, `/` or `_`.
fn normalise(label: &str) -> String {
    label
        .chars()
        .filter(|c| !matches!(c, ' ' | '\t' | '-' | '/' | '_'))
        .map(|c| c.to_ascii_uppercase())
        .collect()
}

/// What's before a `$$` comment.
fn uncommented(line: &str) -> &str {
    line.find("$$").map_or(line, |at| &line[..at])
}

#[derive(Default)]
struct Problems {
    list: Vec<String>,
    more: usize,
}

impl Problems {
    fn push(&mut self, problem: String) {
        if self.list.len() < MAX_PROBLEMS {
            self.list.push(problem);
        } else {
            self.more += 1;
        }
    }

    fn into_list(mut self) -> Vec<String> {
        if self.more > 0 {
            self.list.push(format!(
                "…and {} more problems like these.",
                count(self.more)
            ));
        }
        self.list
    }
}

/// A block's header: what its data tables need.
#[derive(Default)]
struct Header {
    title: Option<String>,
    data_type: Option<String>,
    x_units: Option<String>,
    y_units: Option<String>,
    first_x: Option<f64>,
    last_x: Option<f64>,
    delta_x: Option<f64>,
    x_factor: Option<f64>,
    y_factor: Option<f64>,
    points: Option<usize>,
    ntuples: Option<Ntuples>,
}

/// An `##NTUPLES` table's variables, one entry per symbol.
#[derive(Default)]
struct Ntuples {
    name: String,
    names: Vec<String>,
    symbols: Vec<String>,
    units: Vec<String>,
    dims: Vec<Option<usize>>,
    first: Vec<Option<f64>>,
    last: Vec<Option<f64>>,
    factor: Vec<Option<f64>>,
    page: Option<String>,
    page_points: Option<usize>,
    said_imaginary: bool,
    said_2d: bool,
}

fn list(value: &str) -> Vec<String> {
    value
        .split(',')
        .map(|item| item.trim().to_owned())
        .collect()
}

fn number_in(value: &str) -> Option<f64> {
    let token = value
        .split([' ', '\t', ','])
        .find(|token| !token.is_empty())?;
    token.parse::<f64>().ok().filter(|value| value.is_finite())
}

/// How a table's numbers are laid out.
enum Layout {
    /// `(X++(Y..Y))`: an abscissa, then ordinates at a fixed step.
    Ordinates {
        x_factor: f64,
        y_factor: f64,
        first_x: Option<f64>,
        last_x: Option<f64>,
        delta_x: Option<f64>,
    },
    /// `(XY..XY)` or `(XYW..XYW)`: groups of `size` values, X and Y first.
    Groups {
        size: usize,
        x_factor: f64,
        y_factor: f64,
    },
}

struct Table<'a> {
    /// The line of its `##` record.
    line: usize,
    block: Block,
    layout: Layout,
    lines: Vec<(usize, &'a str)>,
}

#[derive(Default)]
struct Reader<'a> {
    title: Option<String>,
    seen_title: bool,
    stack: Vec<Header>,
    table: Option<Table<'a>>,
    blocks: Vec<Block>,
    points: usize,
    truncated: bool,
    said_blocks: bool,
    problems: Problems,
}

impl<'a> Reader<'a> {
    fn line(&mut self, number: usize, raw: &'a str) -> Result<(), String> {
        if raw.len() > MAX_LINE {
            self.problems.push(format!(
                "Line {number} is longer than {} bytes and is left out.",
                count(MAX_LINE)
            ));
            return Ok(());
        }
        let line = uncommented(raw);
        let Some(record) = line.trim_start().strip_prefix("##") else {
            if let Some(table) = &mut self.table
                && !line.trim().is_empty()
            {
                table.lines.push((number, line));
            }
            return Ok(());
        };
        self.finish_table();
        let Some((label, value)) = record.split_once('=') else {
            self.problems.push(format!(
                "Line {number} starts a record with ## but has no \"=\"; it's left out."
            ));
            return Ok(());
        };
        let label = normalise(label);
        let value = value.trim();
        if !self.seen_title && label != "TITLE" {
            return Err(format!(
                "This isn't a JCAMP-DX file: its first record (line {number}) is ##{label}=, \
                 not ##TITLE=."
            ));
        }
        self.record(number, &label, value);
        Ok(())
    }

    fn record(&mut self, number: usize, label: &str, value: &'a str) {
        let text = || (!value.is_empty()).then(|| value.to_owned());
        if label == "TITLE" {
            self.seen_title = true;
            if self.title.is_none() {
                self.title = text();
            }
            if self.stack.len() >= MAX_DEPTH {
                self.problems.push(format!(
                    "Line {number}: blocks are nested more than {MAX_DEPTH} deep; this one is \
                     read as the next block at that depth."
                ));
                self.stack.pop();
            }
            self.stack.push(Header {
                title: text(),
                ..Header::default()
            });
            return;
        }
        if label == "END" {
            self.stack.pop();
            return;
        }
        let Some(header) = self.stack.last_mut() else {
            self.problems.push(format!(
                "Line {number}: ##{label}= is outside any block (after its ##END=) and is \
                 left out."
            ));
            return;
        };
        let numeric = |target: &mut Option<f64>, problems: &mut Problems| match number_in(value) {
            Some(parsed) => *target = Some(parsed),
            None => problems.push(format!(
                "Line {number}: ##{label}= \"{value}\" isn't a number; it's ignored."
            )),
        };
        match label {
            "DATATYPE" => header.data_type = text(),
            "XUNITS" => header.x_units = text(),
            "YUNITS" => header.y_units = text(),
            "FIRSTX" => numeric(&mut header.first_x, &mut self.problems),
            "LASTX" => numeric(&mut header.last_x, &mut self.problems),
            "DELTAX" => numeric(&mut header.delta_x, &mut self.problems),
            "XFACTOR" => numeric(&mut header.x_factor, &mut self.problems),
            "YFACTOR" => numeric(&mut header.y_factor, &mut self.problems),
            "NPOINTS" => {
                let points = value.split_whitespace().next().and_then(|n| n.parse().ok());
                if points.is_none() {
                    self.problems.push(format!(
                        "Line {number}: ##NPOINTS= \"{value}\" isn't a count; it's ignored."
                    ));
                }
                match &mut header.ntuples {
                    Some(ntuples) => ntuples.page_points = points,
                    None => header.points = points,
                }
            }
            "NTUPLES" => {
                header.ntuples = Some(Ntuples {
                    name: value.to_owned(),
                    ..Ntuples::default()
                });
            }
            "ENDNTUPLES" => header.ntuples = None,
            "VARNAME" | "SYMBOL" | "UNITS" | "VARDIM" | "FIRST" | "LAST" | "FACTOR" | "PAGE" => {
                if let Some(ntuples) = &mut header.ntuples {
                    match label {
                        "VARNAME" => ntuples.names = list(value),
                        "SYMBOL" => {
                            ntuples.symbols = list(value)
                                .into_iter()
                                .map(|symbol| symbol.to_ascii_uppercase())
                                .collect();
                        }
                        "UNITS" => ntuples.units = list(value),
                        "VARDIM" => {
                            ntuples.dims = list(value).iter().map(|n| n.parse().ok()).collect();
                        }
                        "FIRST" => {
                            ntuples.first = list(value).iter().map(|n| number_in(n)).collect();
                        }
                        "LAST" => ntuples.last = list(value).iter().map(|n| number_in(n)).collect(),
                        "FACTOR" => {
                            ntuples.factor = list(value).iter().map(|n| number_in(n)).collect();
                        }
                        _ => {
                            ntuples.page = text();
                            ntuples.page_points = None;
                        }
                    }
                }
            }
            "XYDATA" | "XYPOINTS" | "PEAKTABLE" => self.start_table(number, label, value),
            "DATATABLE" => self.start_page(number, value),
            _ => {}
        }
    }

    /// Whether another table may be read; says so once when it may not.
    fn room(&mut self) -> bool {
        if self.blocks.len() < MAX_BLOCKS && self.points < MAX_POINTS {
            return true;
        }
        self.truncated = true;
        if !self.said_blocks {
            self.said_blocks = true;
            self.problems.push(if self.blocks.len() >= MAX_BLOCKS {
                format!("Only the first {MAX_BLOCKS} spectra are read.")
            } else {
                format!("Only the first {} points are read.", count(MAX_POINTS))
            });
        }
        false
    }

    fn start_table(&mut self, number: usize, label: &str, value: &'a str) {
        if !self.room() {
            return;
        }
        let Some(header) = self.stack.last() else {
            return;
        };
        let (form, rest) = split_form(value);
        let x_factor = header.x_factor.unwrap_or(1.0);
        let y_factor = header.y_factor.unwrap_or(1.0);
        let mass = header
            .data_type
            .as_deref()
            .is_some_and(|kind| kind.to_ascii_uppercase().contains("MASS"));
        let (layout, kind) = match (label, parse_form(form)) {
            ("XYDATA", Some(Form::Ordinates)) => (
                Layout::Ordinates {
                    x_factor,
                    y_factor,
                    first_x: header.first_x,
                    last_x: header.last_x,
                    delta_x: header.delta_x,
                },
                BlockKind::Xy,
            ),
            ("XYPOINTS" | "PEAKTABLE", Some(Form::Groups(size))) => (
                Layout::Groups {
                    size,
                    x_factor,
                    y_factor,
                },
                if label == "PEAKTABLE" || mass {
                    BlockKind::Peaks
                } else {
                    BlockKind::Xy
                },
            ),
            _ => {
                self.problems.push(format!(
                    "Line {number}: ##{label}= {value} isn't a table form previews read \
                     ((X++(Y..Y)) for XYDATA; (XY..XY) or (XYW..XYW) for XYPOINTS and PEAK \
                     TABLE); it's left out."
                ));
                return;
            }
        };
        let block = Block {
            title: header.title.clone(),
            data_type: header.data_type.clone(),
            x_units: header.x_units.clone(),
            y_units: header.y_units.clone(),
            kind,
            x: Vec::new(),
            y: Vec::new(),
            declared_points: header.points,
        };
        let mut lines = Vec::new();
        if !rest.trim().is_empty() {
            lines.push((number, rest));
        }
        self.table = Some(Table {
            line: number,
            block,
            layout,
            lines,
        });
    }

    /// An `##NTUPLES` page's `##DATA TABLE=`.
    fn start_page(&mut self, number: usize, value: &'a str) {
        let Some(header) = self.stack.last_mut() else {
            return;
        };
        match page_table(header, number, value) {
            Ok(Some(table)) => {
                if self.room() {
                    self.table = Some(table);
                }
            }
            Ok(None) => {}
            Err(problem) => self.problems.push(problem),
        }
    }

    fn finish_table(&mut self) {
        let Some(table) = self.table.take() else {
            return;
        };
        let budget = MAX_POINTS.saturating_sub(self.points);
        let Table {
            line,
            mut block,
            layout,
            lines,
        } = table;
        let full = match layout {
            Layout::Ordinates {
                x_factor,
                y_factor,
                first_x,
                last_x,
                delta_x,
            } => ordinates(
                &mut block,
                &lines,
                &Scale {
                    line,
                    x_factor,
                    y_factor,
                    first_x,
                    last_x,
                    delta_x,
                },
                budget,
                &mut self.problems,
            ),
            Layout::Groups {
                size,
                x_factor,
                y_factor,
            } => groups(
                &mut block,
                &lines,
                (size, x_factor, y_factor),
                budget,
                &mut self.problems,
            ),
        };
        let name = block.title.as_deref().unwrap_or("untitled");
        if full {
            self.truncated = true;
            self.problems.push(format!(
                "\"{name}\" is cut short: only the first {} points across the file are read.",
                count(MAX_POINTS)
            ));
        } else if let Some(declared) = block.declared_points
            && declared != block.x.len()
        {
            self.problems.push(format!(
                "Line {line}: \"{name}\" says it has {} points (##NPOINTS) but its data has {}.",
                count(declared),
                count(block.x.len())
            ));
        }
        if block.x.is_empty() {
            self.problems.push(format!(
                "Line {line}: \"{name}\" has no points; it's left out."
            ));
            return;
        }
        self.points += block.x.len();
        self.blocks.push(block);
    }

    fn finish(mut self) -> Result<Spectra, String> {
        self.finish_table();
        if !self.seen_title {
            return Err("This isn't a JCAMP-DX file: it has no ##TITLE= record.".to_owned());
        }
        if !self.stack.is_empty() {
            self.problems
                .push("The file ends before its last ##END=; it may be cut short.".to_owned());
        }
        if self.blocks.is_empty() {
            let problems = self.problems.into_list();
            let why = if problems.is_empty() {
                " It has no ##XYDATA=, ##XYPOINTS= or ##PEAK TABLE=.".to_owned()
            } else {
                format!(" {}", problems.join(" "))
            };
            return Err(format!(
                "There's no spectrum in this JCAMP-DX file that a preview can draw.{why}"
            ));
        }
        Ok(Spectra {
            title: self.title,
            blocks: self.blocks,
            truncated: self.truncated,
            problems: self.problems.into_list(),
        })
    }
}

/// The table an `##NTUPLES` page's `##DATA TABLE=` starts: none if it's left out (an
/// imaginary part, or a 2D spectrum's page, said once), or why it can't be read.
fn page_table<'a>(
    header: &mut Header,
    number: usize,
    value: &str,
) -> Result<Option<Table<'a>>, String> {
    let Some(ntuples) = &mut header.ntuples else {
        return Err(format!(
            "Line {number}: ##DATA TABLE= is outside an ##NTUPLES table; it's left out."
        ));
    };
    let page = ntuples.page.clone().unwrap_or_default();
    let name = ntuples.name.to_ascii_uppercase();
    if name.contains("ND ")
        || name.contains("2D")
        || page.trim_start().to_ascii_uppercase().starts_with("F1")
    {
        if ntuples.said_2d {
            return Ok(None);
        }
        ntuples.said_2d = true;
        return Err(format!(
            "Line {number}: \"{}\" is a 2D spectrum, which previews don't draw yet; its pages \
             are left out.",
            ntuples.name
        ));
    }
    let (form, rest) = split_form(value);
    let Some((form, x_symbol, y_symbol)) = parse_form_symbols(form) else {
        return Err(format!(
            "Line {number}: ##DATA TABLE= {value} isn't a table form previews read; page \
             {page} is left out."
        ));
    };
    let index = |symbol: &str| ntuples.symbols.iter().position(|s| s == symbol);
    let (Some(ix), Some(iy)) = (index(&x_symbol), index(&y_symbol)) else {
        return Err(format!(
            "Line {number}: ##DATA TABLE= {value} names variables that ##SYMBOL= doesn't list; \
             page {page} is left out."
        ));
    };
    let y_name = ntuples.names.get(iy).cloned().unwrap_or_default();
    if y_symbol == "I" || y_name.to_ascii_uppercase().contains("IMAG") {
        if ntuples.said_imaginary {
            return Ok(None);
        }
        ntuples.said_imaginary = true;
        return Err(format!(
            "Line {number}: the imaginary part ({}) is left out; the real part is drawn.",
            if y_name.is_empty() {
                &y_symbol
            } else {
                &y_name
            }
        ));
    }
    let unit = |at: usize| {
        ntuples
            .units
            .get(at)
            .filter(|unit| !unit.is_empty())
            .cloned()
    };
    let data_type = header
        .data_type
        .clone()
        .unwrap_or_else(|| ntuples.name.clone());
    let (layout, kind) = page_layout(ntuples, &form, rest, &data_type, (ix, iy));
    let title = match (&header.title, page.is_empty()) {
        (Some(title), false) => Some(format!("{title} ({page})")),
        (Some(title), true) => Some(title.clone()),
        (None, false) => Some(page.clone()),
        (None, true) => None,
    };
    let block = Block {
        title,
        data_type: Some(data_type),
        x_units: unit(ix),
        y_units: unit(iy),
        kind,
        x: Vec::new(),
        y: Vec::new(),
        declared_points: ntuples
            .page_points
            .or_else(|| ntuples.dims.get(ix).copied().flatten()),
    };
    Ok(Some(Table {
        line: number,
        block,
        layout,
        lines: Vec::new(),
    }))
}

/// How an `##NTUPLES` page's table is laid out, from its variables' factors, first and last
/// values: X is variable `ix`, Y is `iy`.
fn page_layout(
    ntuples: &Ntuples,
    form: &Form,
    rest: &str,
    data_type: &str,
    (ix, iy): (usize, usize),
) -> (Layout, BlockKind) {
    let pick = |values: &[Option<f64>], at: usize| values.get(at).copied().flatten();
    let x_factor = pick(&ntuples.factor, ix).unwrap_or(1.0);
    let y_factor = pick(&ntuples.factor, iy).unwrap_or(1.0);
    match form {
        Form::Ordinates => (
            Layout::Ordinates {
                x_factor,
                y_factor,
                first_x: pick(&ntuples.first, ix),
                last_x: pick(&ntuples.last, ix),
                delta_x: None,
            },
            BlockKind::Xy,
        ),
        Form::Groups(size) => {
            let peaks = rest.to_ascii_uppercase().contains("PEAK")
                || data_type.to_ascii_uppercase().contains("MASS");
            (
                Layout::Groups {
                    size: *size,
                    x_factor,
                    y_factor,
                },
                if peaks {
                    BlockKind::Peaks
                } else {
                    BlockKind::Xy
                },
            )
        }
    }
}

/// A table form's parenthesised part, and what follows it.
fn split_form(value: &str) -> (&str, &str) {
    let mut depth = 0usize;
    for (at, c) in value.char_indices() {
        match c {
            '(' => depth += 1,
            ')' => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    return value.split_at(at + 1);
                }
            }
            _ => {}
        }
    }
    (value, "")
}

enum Form {
    Ordinates,
    Groups(usize),
}

fn parse_form(form: &str) -> Option<Form> {
    parse_form_symbols(form).map(|(form, _, _)| form)
}

/// `(X++(Y..Y))` → ordinates of Y at steps of X; `(XY..XY)` and `(XYW..XYW)` → groups.
fn parse_form_symbols(form: &str) -> Option<(Form, String, String)> {
    let form: String = form
        .chars()
        .filter(|c| !c.is_whitespace())
        .map(|c| c.to_ascii_uppercase())
        .collect();
    let inner = form.strip_prefix('(')?.strip_suffix(')')?;
    if let Some((x, rest)) = inner.split_once("++") {
        let (y, again) = rest
            .strip_prefix('(')?
            .strip_suffix(')')?
            .split_once("..")?;
        if x.is_empty() || y.is_empty() || y != again || !x.chars().all(char::is_alphanumeric) {
            return None;
        }
        return Some((Form::Ordinates, x.to_owned(), y.to_owned()));
    }
    let (group, again) = inner.split_once("..")?;
    if group != again || !(group == "XY" || group == "XYW") {
        return None;
    }
    Some((Form::Groups(group.len()), "X".to_owned(), "Y".to_owned()))
}

/// A number in a data line.
#[derive(Debug, Clone, Copy, PartialEq)]
enum Token {
    /// An absolute value: AFFN, or SQZ.
    Value(f64),
    /// A difference from the value before (DIF).
    Dif(f64),
    /// The value (or difference) before, this many times in all (DUP).
    Dup(usize),
    /// `?`: a value that wasn't measured.
    Missing,
}

/// Whether a character is one of ASDF's, other than `E` and `e`, which are also exponents.
fn is_asdf(c: u8) -> bool {
    matches!(c, b'@' | b'A'..=b'D' | b'F'..=b'I' | b'a'..=b'd' | b'f'..=b'i' | b'%' | b'J'..=b'R' | b'j'..=b'r' | b'S'..=b'Z' | b's')
}

/// An ASDF character's kind and leading digit.
fn asdf_lead(c: u8) -> Option<(char, i32)> {
    Some(match c {
        b'@' => ('v', 0),
        b'A'..=b'I' => ('v', i32::from(c - b'A') + 1),
        b'a'..=b'i' => ('v', -(i32::from(c - b'a') + 1)),
        b'%' => ('d', 0),
        b'J'..=b'R' => ('d', i32::from(c - b'J') + 1),
        b'j'..=b'r' => ('d', -(i32::from(c - b'j') + 1)),
        b'S'..=b'Z' => ('u', i32::from(c - b'S') + 1),
        b's' => ('u', 9),
        _ => return None,
    })
}

/// Splits a data line into numbers. `asdf` says whether `E` and `e` are SQZ digits (they
/// are in compressed tables) or exponents (in AFFN).
fn tokenize(line: &str, asdf: bool, out: &mut Vec<Token>) -> Result<(), String> {
    out.clear();
    let bytes = line.as_bytes();
    let mut at = 0;
    while let Some(&c) = bytes.get(at) {
        match c {
            b' ' | b'\t' | b',' | b';' => at += 1,
            b'?' => {
                out.push(Token::Missing);
                at += 1;
            }
            b'+' | b'-' | b'.' | b'0'..=b'9' => {
                let end = affn_end(bytes, at, !asdf);
                let text = &line[at..end];
                let value = text
                    .parse::<f64>()
                    .map_err(|_| format!("\"{text}\" (column {}) isn't a number", at + 1))?;
                out.push(Token::Value(value));
                at = end;
            }
            _ => {
                let lead = if asdf { asdf_lead(c) } else { None };
                let Some((kind, digit)) = lead else {
                    let shown = line[at..].chars().next().unwrap_or('?');
                    return Err(format!(
                        "\"{shown}\" (column {}) isn't part of a number",
                        at + 1
                    ));
                };
                let mut end = at + 1;
                while bytes
                    .get(end)
                    .is_some_and(|b| b.is_ascii_digit() || (*b == b'.' && kind != 'u'))
                {
                    end += 1;
                }
                let digits = format!("{}{}", digit.abs(), &line[at + 1..end]);
                if kind == 'u' {
                    let times = digits.parse::<usize>().unwrap_or(usize::MAX);
                    out.push(Token::Dup(times));
                } else {
                    let magnitude = digits.parse::<f64>().map_err(|_| {
                        format!("\"{}\" (column {}) isn't a number", &line[at..end], at + 1)
                    })?;
                    let value = if digit < 0 { -magnitude } else { magnitude };
                    out.push(if kind == 'v' {
                        Token::Value(value)
                    } else {
                        Token::Dif(value)
                    });
                }
                at = end;
            }
        }
    }
    Ok(())
}

/// Where an AFFN number that starts at `at` ends.
fn affn_end(bytes: &[u8], at: usize, exponents: bool) -> usize {
    let mut end = at;
    if matches!(bytes.get(end), Some(b'+' | b'-')) {
        end += 1;
    }
    while bytes
        .get(end)
        .is_some_and(|b| b.is_ascii_digit() || *b == b'.')
    {
        end += 1;
    }
    if exponents && matches!(bytes.get(end), Some(b'E' | b'e')) {
        let mut exponent = end + 1;
        if matches!(bytes.get(exponent), Some(b'+' | b'-')) {
            exponent += 1;
        }
        if bytes.get(exponent).is_some_and(u8::is_ascii_digit) {
            while bytes.get(exponent).is_some_and(u8::is_ascii_digit) {
                exponent += 1;
            }
            end = exponent;
        }
    }
    end
}

/// What places an `(X++(Y..Y))` table's points.
struct Scale {
    line: usize,
    x_factor: f64,
    y_factor: f64,
    first_x: Option<f64>,
    last_x: Option<f64>,
    delta_x: Option<f64>,
}

/// Reads an `(X++(Y..Y))` table into `block`; true if it stopped at `budget` points.
#[allow(clippy::too_many_lines)]
fn ordinates(
    block: &mut Block,
    lines: &[(usize, &str)],
    scale: &Scale,
    budget: usize,
    problems: &mut Problems,
) -> bool {
    let name = block.title.clone().unwrap_or_else(|| "untitled".to_owned());
    let asdf = lines.iter().any(|(_, line)| line.bytes().any(is_asdf));
    let declared = block.declared_points;
    let step = match (scale.first_x, scale.last_x, declared, scale.delta_x) {
        (Some(first), Some(last), Some(points), _) if points >= 2 => {
            #[allow(clippy::cast_precision_loss)]
            let gaps = (points - 1) as f64;
            (last - first) / gaps
        }
        (_, _, _, Some(delta)) => delta,
        (_, _, Some(1), _) => 0.0,
        _ => {
            problems.push(format!(
                "Line {}: \"{name}\" doesn't say how far apart its points are (##FIRSTX, \
                 ##LASTX and ##NPOINTS, or ##DELTAX); it's left out.",
                scale.line
            ));
            return false;
        }
    };

    // The step's sign follows the data: some writers swap ##FIRSTX and ##LASTX.
    let mut tokens = Vec::new();
    let mut abscissas = Vec::with_capacity(2);
    for (_, line) in lines {
        if tokenize(line, asdf, &mut tokens).is_ok()
            && let Some(Token::Value(x)) = tokens.first()
            && abscissas.last() != Some(x)
        {
            abscissas.push(*x);
        }
        if abscissas.len() == 2 {
            break;
        }
    }
    let mut step = step;
    let mut flipped = false;
    if let [first, second] = abscissas[..]
        && (second - first) * step < 0.0
    {
        step = -step;
        flipped = true;
        problems.push(format!(
            "Line {}: \"{name}\"'s ##FIRSTX and ##LASTX run the other way from its data; its X \
             values follow the data.",
            scale.line
        ));
    }

    let mut last_y: Option<f64> = None;
    let mut ended_in_dif = false;
    let mut not_finite = NotFinite::default();
    let mut ys: Vec<f64> = Vec::new();
    for (number, line) in lines {
        if let Err(message) = tokenize(line, asdf, &mut tokens) {
            problems.push(format!("Line {number}: {message}; the line is left out."));
            ended_in_dif = false;
            continue;
        }
        let Some((first, rest)) = tokens.split_first() else {
            continue;
        };
        let Token::Value(x_raw) = *first else {
            problems.push(format!(
                "Line {number} doesn't start with an X value; it's left out."
            ));
            continue;
        };
        ys.clear();
        // The last operation, for DUP to repeat: None, a value, or a difference.
        let mut last_dif: Option<Option<f64>> = None;
        let mut starts_absolute = false;
        let mut broken = false;
        for (index, token) in rest.iter().enumerate() {
            match *token {
                Token::Value(value) => {
                    ys.push(value);
                    last_dif = Some(None);
                }
                Token::Missing => {
                    ys.push(f64::NAN);
                    last_dif = Some(None);
                }
                Token::Dif(difference) => {
                    let Some(base) = ys.last().copied().or(last_y) else {
                        problems.push(format!(
                            "Line {number}: a difference (DIF) has no value before it; the line \
                             is left out."
                        ));
                        broken = true;
                        break;
                    };
                    ys.push(base + difference);
                    last_dif = Some(Some(difference));
                }
                Token::Dup(times) => {
                    let (Some(previous), Some(operation)) = (ys.last().copied(), last_dif) else {
                        problems.push(format!(
                            "Line {number}: a repeat (DUP) has nothing before it to repeat; the \
                             line is left out."
                        ));
                        broken = true;
                        break;
                    };
                    // Never more than the points still allowed.
                    let repeats = times.saturating_sub(1).min(budget.saturating_add(1));
                    let mut value = previous;
                    for _ in 0..repeats {
                        value += operation.unwrap_or(0.0);
                        ys.push(value);
                    }
                }
            }
            if index == 0 {
                starts_absolute = matches!(token, Token::Value(_) | Token::Missing);
            }
        }
        if broken {
            ended_in_dif = false;
            continue;
        }
        // In DIF form, a line's first Y repeats the line before's last Y, as a check.
        let mut skip = 0;
        if ended_in_dif && starts_absolute {
            if let (Some(check), Some(last)) = (ys.first(), last_y) {
                let tolerance = 1e-9 * last.abs().max(1.0);
                if (check - last).abs() > tolerance {
                    problems.push(format!(
                        "Line {number}: its first Y ({check}) should repeat the last Y of the \
                         line before ({last}) as a check, but doesn't; the data may be damaged \
                         around here."
                    ));
                }
            }
            skip = 1;
        }
        ended_in_dif = matches!(last_dif, Some(Some(_)));
        if let Some(last) = ys.last() {
            last_y = Some(*last);
        }
        let x0 = x_raw * scale.x_factor;
        for (index, y) in ys.iter().enumerate().skip(skip) {
            #[allow(clippy::cast_precision_loss)]
            let x = x0 + index as f64 * step;
            let y = y * scale.y_factor;
            if !x.is_finite() || !y.is_finite() {
                not_finite.add(*number);
                continue;
            }
            if block.x.len() >= budget {
                not_finite.say(problems, &name);
                return true;
            }
            block.x.push(x);
            block.y.push(y);
        }
    }
    not_finite.say(problems, &name);
    if let (Some(first), Some(x0), false) = (scale.first_x, block.x.first(), flipped) {
        let tolerance = step.abs().max(1e-9 * first.abs().max(1.0));
        if (x0 - first).abs() > tolerance {
            problems.push(format!(
                "Line {}: \"{name}\"'s first X is {x0} but its ##FIRSTX says {first}.",
                scale.line
            ));
        }
    }
    false
}

/// How many values were missing or not finite, and the line of the first.
#[derive(Default)]
struct NotFinite {
    values: usize,
    first_line: usize,
}

impl NotFinite {
    fn add(&mut self, line: usize) {
        if self.values == 0 {
            self.first_line = line;
        }
        self.values += 1;
    }

    fn say(&self, problems: &mut Problems, name: &str) {
        if self.values > 0 {
            problems.push(format!(
                "Line {}: \"{name}\" has values that are missing (?) or not finite numbers, {} \
                 in all from this line on; they're left out.",
                self.first_line,
                count(self.values)
            ));
        }
    }
}

/// Reads an `(XY..XY)` or `(XYW..XYW)` table into `block`; true if it stopped at `budget`.
fn groups(
    block: &mut Block,
    lines: &[(usize, &str)],
    (size, x_factor, y_factor): (usize, f64, f64),
    budget: usize,
    problems: &mut Problems,
) -> bool {
    let name = block.title.clone().unwrap_or_else(|| "untitled".to_owned());
    let mut tokens = Vec::new();
    let mut group: Vec<f64> = Vec::with_capacity(size);
    let mut not_finite = NotFinite::default();
    for (number, line) in lines {
        if let Err(message) = tokenize(line, false, &mut tokens) {
            problems.push(format!("Line {number}: {message}; the line is left out."));
            continue;
        }
        for token in &tokens {
            group.push(match token {
                Token::Value(value) => *value,
                _ => f64::NAN,
            });
            if group.len() < size {
                continue;
            }
            let x = group[0] * x_factor;
            let y = group[1] * y_factor;
            group.clear();
            if !x.is_finite() || !y.is_finite() {
                not_finite.add(*number);
                continue;
            }
            if block.x.len() >= budget {
                not_finite.say(problems, &name);
                return true;
            }
            block.x.push(x);
            block.y.push(y);
        }
    }
    not_finite.say(problems, &name);
    if !group.is_empty() {
        problems.push(format!(
            "\"{name}\" ends with an incomplete point ({} of {size} values); it's left out.",
            group.len()
        ));
    }
    false
}

/// A count with thousands separators, as people read them.
fn count(value: usize) -> String {
    let digits = value.to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (index, digit) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            out.push(',');
        }
        out.push(digit);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fmt::Write as _;

    fn tokens(line: &str, asdf: bool) -> Vec<Token> {
        let mut out = Vec::new();
        tokenize(line, asdf, &mut out).unwrap();
        out
    }

    #[test]
    fn labels_ignore_case_spaces_and_punctuation() {
        assert_eq!(normalise("DATA TYPE"), "DATATYPE");
        assert_eq!(normalise("Data_Type"), "DATATYPE");
        assert_eq!(normalise("end ntuples"), "ENDNTUPLES");
        assert_eq!(normalise("VAR-NAME"), "VARNAME");
        assert_eq!(normalise("PEAK TABLE"), "PEAKTABLE");
    }

    #[test]
    fn tokenizes_affn_pac_sqz_dif_and_dup() {
        use Token::{Dif, Dup, Missing, Value};
        assert_eq!(
            tokens("4000 1.5E3 -2e-1,3;?", false),
            vec![
                Value(4000.0),
                Value(1500.0),
                Value(-0.2),
                Value(3.0),
                Missing
            ]
        );
        // PAC: signs separate numbers.
        assert_eq!(
            tokens("4000+10160-10159+10159", true),
            vec![
                Value(4000.0),
                Value(10160.0),
                Value(-10159.0),
                Value(10159.0)
            ]
        );
        // SQZ: @ is 0, A–I are 1–9, a–i are −1 to −9, then digits follow.
        assert_eq!(
            tokens("1@A1b23I", true),
            vec![
                Value(1.0),
                Value(0.0),
                Value(11.0),
                Value(-223.0),
                Value(9.0)
            ]
        );
        // DIF: % is 0, J–R are 1–9, j–r are −1 to −9.
        assert_eq!(tokens("%J5r1", true), vec![Dif(0.0), Dif(15.0), Dif(-91.0)]);
        // DUP: S–Z are 1–8, s is 9.
        assert_eq!(tokens("A0S2s", true), vec![Value(10.0), Dup(12), Dup(9)]);
        // In ASDF, E is SQZ 5, not an exponent.
        assert_eq!(tokens("1E3", true), vec![Value(1.0), Value(53.0)]);
    }

    #[test]
    fn an_unknown_character_is_named_with_its_column() {
        let mut out = Vec::new();
        let error = tokenize("100 2x", false, &mut out).unwrap_err();
        assert!(error.contains("\"x\" (column 6)"), "{error}");
    }

    /// One run of ordinates, written in each of the forms: AFFN, SQZ, SQZ with DUP, DIF,
    /// and DIF with DUP.
    const FORMS: [&str; 5] = [
        "1 1000 1020 1030 1030 1030 1010 900",
        "1A000A020A030A030A030A010I00",
        "1A000A020A030UA010I00",
        "1A000K0J0%%k0j10",
        "1A000K0J0%Tk0j10",
    ];

    fn table(data: &str) -> String {
        format!(
            "##TITLE=forms\n##JCAMP-DX=4.24\n##DATA TYPE=UV-VIS SPECTRUM\n##XUNITS=NANOMETERS\n\
             ##YUNITS=ABSORBANCE\n##XFACTOR=1\n##YFACTOR=0.001\n##FIRSTX=1\n##LASTX=7\n\
             ##NPOINTS=7\n##XYDATA=(X++(Y..Y))\n{data}\n##END=\n"
        )
    }

    #[test]
    fn every_compression_gives_the_same_points() {
        let expected = vec![1.0, 1.02, 1.03, 1.03, 1.03, 1.01, 0.9];
        for form in FORMS {
            let spectra = read(table(form).as_bytes()).unwrap();
            let block = &spectra.blocks[0];
            assert_eq!(block.x, vec![1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0], "{form}");
            for (got, want) in block.y.iter().zip(&expected) {
                assert!((got - want).abs() < 1e-12, "{form}: {got} vs {want}");
            }
            assert_eq!(block.y.len(), 7, "{form}");
            assert!(
                spectra.problems.is_empty(),
                "{form}: {:?}",
                spectra.problems
            );
            assert_eq!(block.kind, BlockKind::Xy);
            assert_eq!(block.declared_points, Some(7));
            assert_eq!(block.x_units.as_deref(), Some("NANOMETERS"));
        }
    }

    #[test]
    fn drops_dif_check_values_and_says_when_they_disagree() {
        // Line 2's first Y (1030) repeats line 1's last, as a check.
        let good = table("1A000K0J0\n3A030%%k0j10");
        let spectra = read(good.as_bytes()).unwrap();
        assert_eq!(spectra.blocks[0].y.len(), 7);
        assert!(spectra.problems.is_empty(), "{:?}", spectra.problems);
        assert!((spectra.blocks[0].y[6] - 0.9).abs() < 1e-12);

        let bad = table("1A000K0J0\n3A031%%k0j10");
        let spectra = read(bad.as_bytes()).unwrap();
        assert_eq!(spectra.blocks[0].y.len(), 7);
        let problem = spectra
            .problems
            .iter()
            .find(|p| p.contains("check"))
            .unwrap();
        assert!(problem.starts_with("Line 13:"), "{problem}");
        assert!(
            problem.contains("1031") && problem.contains("1030"),
            "{problem}"
        );
    }

    #[test]
    fn affn_lines_have_no_check_values() {
        let spectra = read(table("1 1000 1020 1030\n4 1030 1030 1010 900").as_bytes()).unwrap();
        assert_eq!(spectra.blocks[0].y.len(), 7);
        assert!((spectra.blocks[0].x[3] - 4.0).abs() < 1e-12);
    }

    #[test]
    fn x_follows_the_data_when_firstx_and_lastx_are_swapped() {
        let swapped = table("7 900 1010\n5 1030 1030 1030\n2 1020 1000")
            .replace("##FIRSTX=1", "##FIRSTX=7")
            .replace("##LASTX=7", "##LASTX=1");
        let spectra = read(swapped.as_bytes()).unwrap();
        assert_eq!(spectra.blocks[0].x, vec![7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0]);
        assert!(spectra.problems.is_empty(), "{:?}", spectra.problems);

        // As one Bruker writer does: FIRSTX and LASTX ascending, the data descending.
        let backwards = table("7 900 1010\n5 1030 1030 1030\n2 1020 1000");
        let spectra = read(backwards.as_bytes()).unwrap();
        assert_eq!(spectra.blocks[0].x, vec![7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0]);
        assert!(
            spectra.problems[0].contains("run the other way"),
            "{:?}",
            spectra.problems
        );
    }

    #[test]
    fn peak_tables_are_sticks_and_take_x_and_y_of_xyw() {
        let file = "##TITLE=peaks\n##DATA TYPE=INFRARED PEAK TABLE\n##XFACTOR=2\n##YFACTOR=10\n\
                    ##NPOINTS=3\n##PEAK TABLE=(XYW..XYW)\n100,5,1 200,6,1\n300,7,1\n##END=";
        let spectra = read(file.as_bytes()).unwrap();
        let block = &spectra.blocks[0];
        assert_eq!(block.kind, BlockKind::Peaks);
        assert_eq!(block.x, vec![200.0, 400.0, 600.0]);
        assert_eq!(block.y, vec![50.0, 60.0, 70.0]);
        assert!(spectra.problems.is_empty(), "{:?}", spectra.problems);
    }

    #[test]
    fn xypoints_of_a_mass_spectrum_are_sticks_and_others_lines() {
        let mass =
            "##TITLE=ms\n##DATA TYPE=MASS SPECTRUM\n##XYPOINTS=(XY..XY)\n41,520; 43,1000\n##END=";
        assert_eq!(
            read(mass.as_bytes()).unwrap().blocks[0].kind,
            BlockKind::Peaks
        );
        let uv = "##TITLE=uv\n##DATA TYPE=UV/VIS SPECTRUM\n##XYPOINTS=(XY..XY)\n200,0.1\n201,0.2\n##END=";
        let spectra = read(uv.as_bytes()).unwrap();
        assert_eq!(spectra.blocks[0].kind, BlockKind::Xy);
        assert_eq!(spectra.blocks[0].y, vec![0.1, 0.2]);
    }

    #[test]
    fn reads_each_block_of_a_compound_file() {
        let file = "##TITLE=link\n##JCAMP-DX=4.24\n##DATA TYPE=LINK\n##BLOCKS=2\n\
                    ##TITLE=one\n##BLOCK_ID=1\n##DATA TYPE=MASS SPECTRUM\n##PEAK TABLE=(XY..XY)\n1,2\n##END=\n\
                    ##TITLE=two\n##BLOCK_ID=2\n##DATA TYPE=INFRARED SPECTRUM\n##FIRSTX=1\n##LASTX=3\n##NPOINTS=3\n\
                    ##XYDATA=(X++(Y..Y))\n1 5 6 7\n##END=\n##END=\n";
        let spectra = read(file.as_bytes()).unwrap();
        assert_eq!(spectra.title.as_deref(), Some("link"));
        assert_eq!(spectra.blocks.len(), 2);
        assert_eq!(spectra.blocks[0].title.as_deref(), Some("one"));
        assert_eq!(spectra.blocks[0].kind, BlockKind::Peaks);
        assert_eq!(spectra.blocks[1].title.as_deref(), Some("two"));
        assert_eq!(spectra.blocks[1].y, vec![5.0, 6.0, 7.0]);
        assert!(spectra.problems.is_empty(), "{:?}", spectra.problems);
    }

    #[test]
    fn reads_ntuples_real_pages_and_says_what_it_left_out() {
        let file = "##TITLE=fid\n##DATA TYPE=NMR FID\n##NTUPLES=NMR FID\n\
                    ##VAR_NAME=TIME, FID/REAL, FID/IMAG\n##SYMBOL=X, R, I\n\
                    ##VAR_DIM=3, 3, 3\n##UNITS=SECONDS, ARBITRARY UNITS, ARBITRARY UNITS\n\
                    ##FIRST=0, 1, 1\n##LAST=0.2, 3, 3\n##FACTOR=0.1, 2, 2\n\
                    ##PAGE=N=1\n##DATA TABLE=(X++(R..R)), XYDATA\n0 1 2 3\n\
                    ##PAGE=N=2\n##DATA TABLE=(X++(I..I)), XYDATA\n0 4 5 6\n\
                    ##END NTUPLES=NMR FID\n##END=";
        let spectra = read(file.as_bytes()).unwrap();
        assert_eq!(spectra.blocks.len(), 1);
        let block = &spectra.blocks[0];
        assert_eq!(block.title.as_deref(), Some("fid (N=1)"));
        assert_eq!(block.x_units.as_deref(), Some("SECONDS"));
        assert_eq!(block.y, vec![2.0, 4.0, 6.0]);
        assert!((block.x[2] - 0.2).abs() < 1e-12);
        assert_eq!(block.declared_points, Some(3));
        assert_eq!(spectra.problems.len(), 1, "{:?}", spectra.problems);
        assert!(spectra.problems[0].contains("imaginary"));
    }

    #[test]
    fn two_d_ntuples_are_left_out_and_said() {
        let file = "##TITLE=hsqc\n##NTUPLES=nD NMR SPECTRUM\n##SYMBOL=F1, F2, Y\n\
                    ##PAGE=F1=4006.1\n##DATA TABLE=(F2++(Y..Y)), PROFILE\n0 1 2\n##END NTUPLES=\n##END=";
        let error = read(file.as_bytes()).unwrap_err();
        assert!(error.contains("2D spectrum"), "{error}");
    }

    #[test]
    fn refuses_what_isnt_jcamp() {
        assert!(read(b"").unwrap_err().contains("empty"));
        assert!(
            read(b"wavenumber,absorbance\n400,0.1\n")
                .unwrap_err()
                .contains("no ##TITLE=")
        );
        let error = read(b"##JCAMP-DX=4.24\n##TITLE=x\n").unwrap_err();
        assert!(
            error.contains("line 1") && error.contains("##JCAMPDX="),
            "{error}"
        );
        let error = read(b"##TITLE=nothing here\n##END=\n").unwrap_err();
        assert!(error.contains("no spectrum"), "{error}");
        assert!(
            read(&vec![b' '; MAX_BYTES + 1])
                .unwrap_err()
                .contains("bigger than")
        );
    }

    #[test]
    fn bad_numbers_are_problems_with_their_line() {
        let file = table("1 1000 1020 1030\n4 10x30 1030\n5 1030 1010 900");
        let spectra = read(file.as_bytes()).unwrap();
        assert_eq!(spectra.blocks[0].y.len(), 6);
        assert!(
            spectra
                .problems
                .iter()
                .any(|p| p.starts_with("Line 13:") && p.contains("column 5")),
            "{:?}",
            spectra.problems
        );
        assert!(
            spectra
                .problems
                .iter()
                .any(|p| p.contains("says it has 7 points"))
        );
    }

    #[test]
    fn infinity_and_missing_values_are_left_out() {
        let file = table("1 1000 ? 1030 1E400\n5 1010 900");
        let spectra = read(file.as_bytes()).unwrap();
        assert!(spectra.blocks[0].y.iter().all(|y| y.is_finite()));
        assert_eq!(spectra.blocks[0].y, vec![1.0, 1.03, 1.01, 0.9]);
        assert_eq!(spectra.blocks[0].x, vec![1.0, 3.0, 5.0, 6.0]);
        assert!(
            spectra
                .problems
                .iter()
                .any(|p| p.starts_with("Line 12:") && p.contains("2 in all")),
            "{:?}",
            spectra.problems
        );
    }

    #[test]
    fn a_huge_dup_count_stops_at_the_point_limit() {
        // One line claiming a value repeated 10¹⁸ times.
        let file = table("1 1000S999999999999999999");
        let spectra = read(file.as_bytes()).unwrap();
        assert!(spectra.truncated);
        assert_eq!(spectra.blocks[0].y.len(), MAX_POINTS);
    }

    #[test]
    fn points_past_the_limit_across_blocks_are_cut() {
        let mut file = String::from("##TITLE=many\n##BLOCKS=3\n");
        let line: String = std::iter::repeat_n(" 1", 1000).collect();
        for block in 0..3 {
            write!(file, "##TITLE=b{block}\n##DELTAX=1\n##XYDATA=(X++(Y..Y))\n").unwrap();
            for row in 0..800 {
                writeln!(file, "{}{line}", row * 1000).unwrap();
            }
            file.push_str("##END=\n");
        }
        file.push_str("##END=\n");
        let spectra = read(file.as_bytes()).unwrap();
        assert!(spectra.truncated);
        let total: usize = spectra.blocks.iter().map(|b| b.x.len()).sum();
        assert_eq!(total, MAX_POINTS);
        assert_eq!(spectra.blocks.len(), 3);
        assert_eq!(spectra.blocks[2].x.len(), MAX_POINTS - 1_600_000);
    }

    #[test]
    fn blocks_past_the_limit_are_left_out() {
        let mut file = String::from("##TITLE=many\n");
        for block in 0..(MAX_BLOCKS + 5) {
            write!(
                file,
                "##TITLE=b{block}\n##PEAK TABLE=(XY..XY)\n1,1\n##END=\n"
            )
            .unwrap();
        }
        file.push_str("##END=\n");
        let spectra = read(file.as_bytes()).unwrap();
        assert!(spectra.truncated);
        assert_eq!(spectra.blocks.len(), MAX_BLOCKS);
    }

    #[test]
    fn deep_nesting_and_long_lines_are_bounded() {
        let mut file = String::new();
        for depth in 0..10_000 {
            writeln!(file, "##TITLE=t{depth}").unwrap();
        }
        file.push_str("##PEAK TABLE=(XY..XY)\n1,1\n");
        let spectra = read(file.as_bytes()).unwrap();
        assert_eq!(spectra.blocks.len(), 1);
        assert!(spectra.problems.iter().any(|p| p.contains("nested")));
        assert!(spectra.problems.len() <= MAX_PROBLEMS + 1);
        assert!(spectra.problems.last().unwrap().contains("more problems"));

        let mut file = String::from("##TITLE=long\n##PEAK TABLE=(XY..XY)\n1,1\n");
        file.push_str(&"9".repeat(MAX_LINE + 1));
        file.push_str("\n2,2\n");
        let spectra = read(file.as_bytes()).unwrap();
        assert_eq!(spectra.blocks[0].x, vec![1.0, 2.0]);
        assert!(
            spectra
                .problems
                .iter()
                .any(|p| p.starts_with("Line 4 is longer than"))
        );
    }

    #[test]
    fn latin1_text_and_cr_line_ends_are_read() {
        let file =
            b"##TITLE=Ac\xe9tone\r##DATA TYPE=MASS SPECTRUM\r##PEAK TABLE=(XY..XY)\r43,100\r##END=";
        let spectra = read(file).unwrap();
        assert_eq!(spectra.title.as_deref(), Some("Acétone"));
        assert_eq!(spectra.blocks[0].x, vec![43.0]);
    }

    #[test]
    fn comments_are_ignored() {
        let file = table("1 1000 1020 1030 $$ first line\n$$ just a comment\n4 1030 1030 1010 900");
        let spectra = read(file.as_bytes()).unwrap();
        assert_eq!(spectra.blocks[0].y.len(), 7);
    }

    #[test]
    fn every_truncation_of_a_table_is_a_reading_or_an_error() {
        let file = table(FORMS[3]);
        for length in 0..file.len() {
            let _ = read(&file.as_bytes()[..length]);
        }
    }
}
