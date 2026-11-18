//! Parsers for Iki's web app, compiled to WebAssembly and run in a Web Worker.
//!
//! The browser hands the module a file's bytes and gets JSON back: the records of a FASTA
//! file, and, when they're the rows of an alignment, how well each column is conserved.
//! They're Rust so that the same code can run natively when Iki has a desktop app (ADR
//! 0088); in the browser, the worker they run in has no page to reach. It also renders
//! apps' text and expressions (ADR 0090), which the server checks with this same module.

use serde::{Deserialize, Serialize};

pub mod expressions;
pub mod rules;
pub mod templates;

/// The most a file may be: the web app refuses larger ones before sending them.
pub const MAX_BYTES: usize = 64 * 1024 * 1024;

/// One FASTA record.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Record {
    /// The header's first word.
    pub id: String,
    /// The rest of the header.
    pub description: String,
    /// Its residues, upper case, with `-` for gaps (`.` is read as one).
    pub residues: String,
}

/// Why a file couldn't be read, and where.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Problem {
    /// What's wrong, for a person.
    pub message: String,
    /// The line it's on, from 1, if it's on one.
    pub line: Option<usize>,
}

impl Problem {
    fn at(line: usize, message: String) -> Self {
        Self {
            message,
            line: Some(line),
        }
    }
}

/// Reads a FASTA file: records each start with a `>` header line, then lines of residues.
/// Blank lines and `;` comment lines are skipped.
///
/// # Errors
///
/// If there's text before the first header, a character that isn't a residue, a gap or a
/// stop, or no records at all.
pub fn parse_fasta(text: &str) -> Result<Vec<Record>, Problem> {
    let mut records: Vec<Record> = Vec::new();
    for (index, raw) in text.lines().enumerate() {
        let line = index + 1;
        let content = raw.trim_end_matches('\r');
        if let Some(header) = content.strip_prefix('>') {
            let header = header.trim();
            let (id, description) = header
                .split_once(char::is_whitespace)
                .unwrap_or((header, ""));
            records.push(Record {
                id: id.to_owned(),
                description: description.trim().to_owned(),
                residues: String::new(),
            });
            continue;
        }
        let content = content.trim();
        if content.is_empty() || content.starts_with(';') {
            continue;
        }
        let Some(record) = records.last_mut() else {
            return Err(Problem::at(
                line,
                format!(
                    "Line {line} comes before the first record's header (a line starting with >)."
                ),
            ));
        };
        for (column, c) in content.chars().enumerate() {
            match c {
                c if c.is_ascii_alphabetic() => record.residues.push(c.to_ascii_uppercase()),
                '-' | '.' => record.residues.push('-'),
                '*' => record.residues.push('*'),
                c if c.is_whitespace() => {}
                other => {
                    return Err(Problem::at(
                        line,
                        format!(
                            "Line {line} has {other:?} at column {}, which isn't a residue, a gap or a stop.",
                            column + 1
                        ),
                    ));
                }
            }
        }
    }
    if records.is_empty() {
        return Err(Problem {
            message: "It has no records: a FASTA file's start with a line beginning with >."
                .to_owned(),
            line: None,
        });
    }
    Ok(records)
}

/// What the residues are.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Alphabet {
    /// Bases: A, C, G, T or U, and N.
    Nucleotide,
    /// Anything else.
    Protein,
}

fn alphabet(records: &[Record]) -> Alphabet {
    let bases = records
        .iter()
        .flat_map(|r| r.residues.bytes())
        .all(|b| matches!(b, b'A' | b'C' | b'G' | b'T' | b'U' | b'N' | b'-' | b'*'));
    if bases {
        Alphabet::Nucleotide
    } else {
        Alphabet::Protein
    }
}

/// One row of an alignment.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Row {
    /// The record's first word: for an alignment in Iki, its sequence's HRID.
    pub id: String,
    /// The rest of its header.
    pub description: String,
    /// Its residues and gaps, one per column.
    pub aligned: String,
    /// How many residues it has, gaps left out.
    pub residues: usize,
}

/// An alignment's rows, and how well each of its columns is conserved.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Alignment {
    /// Its rows, in the file's order.
    pub rows: Vec<Row>,
    /// How many columns it has.
    pub length: usize,
    /// What its residues are.
    pub alphabet: Alphabet,
    /// A mark per column, as Clustal draws it: `*` where every row has the same residue,
    /// `:` where they're all in one of Clustal's strongly similar groups, `.` where they're
    /// all in a weakly similar one, and a space otherwise (or where any row has a gap).
    /// Groups are for proteins; nucleotides get `*` alone.
    pub conservation: String,
    /// Each column's commonest residue, or `-` where every row has a gap.
    pub consensus: String,
    /// For each column, the share of rows (in percent, rounded down) that have its
    /// consensus residue: how much a multiple alignment agrees there, gaps and all.
    pub agreement: Vec<u8>,
    /// Columns where every row has the same residue.
    pub identical: usize,
    /// Columns where any row has a gap.
    pub gapped: usize,
}

/// Clustal's strongly similar groups of amino acids (Clustal X, `:`).
const STRONG: [&str; 9] = [
    "STA", "NEQK", "NHQK", "NDEQ", "QHRK", "MILV", "MILF", "HY", "FYW",
];

/// Clustal's weakly similar groups (`.`).
const WEAK: [&str; 11] = [
    "CSA", "ATV", "SAG", "STNK", "STPA", "SGND", "SNDEQK", "NDEQHK", "NEQHRK", "FVLIM", "HFY",
];

fn mark(column: &[u8], alphabet: Alphabet) -> char {
    if column.contains(&b'-') {
        return ' ';
    }
    let first = column[0];
    if column.iter().all(|&b| b == first) {
        return '*';
    }
    if alphabet == Alphabet::Nucleotide {
        return ' ';
    }
    let within = |groups: &[&str]| {
        groups
            .iter()
            .any(|g| column.iter().all(|b| g.as_bytes().contains(b)))
    };
    if within(&STRONG) {
        ':'
    } else if within(&WEAK) {
        '.'
    } else {
        ' '
    }
}

/// The column's commonest residue (the first in the alphabet, on a tie), and how many
/// rows have it; `-` and 0 when every row has a gap.
fn consensus(column: &[u8]) -> (u8, usize) {
    let mut counts = [0usize; 256];
    for &b in column {
        counts[usize::from(b)] += 1;
    }
    counts[usize::from(b'-')] = 0;
    let (residue, count) = counts
        .iter()
        .enumerate()
        .rev()
        .max_by_key(|&(_, &count)| count)
        .map_or((b'-', 0), |(residue, &count)| {
            (u8::try_from(residue).unwrap_or(b'-'), count)
        });
    if count == 0 {
        (b'-', 0)
    } else {
        (residue, count)
    }
}

/// The records as an alignment, if they're one: two or more rows, all the same length.
#[must_use]
pub fn layout(records: &[Record]) -> Option<Alignment> {
    let length = records.first()?.residues.len();
    if records.len() < 2 || length == 0 || records.iter().any(|r| r.residues.len() != length) {
        return None;
    }
    let alphabet = alphabet(records);
    let rows_bytes: Vec<&[u8]> = records.iter().map(|r| r.residues.as_bytes()).collect();
    let mut conservation = String::with_capacity(length);
    let mut consensus_line = String::with_capacity(length);
    let mut agreement = Vec::with_capacity(length);
    let (mut identical, mut gapped) = (0, 0);
    let mut column = Vec::with_capacity(records.len());
    for at in 0..length {
        column.clear();
        column.extend(rows_bytes.iter().map(|r| r[at]));
        let m = mark(&column, alphabet);
        if m == '*' {
            identical += 1;
        }
        if column.contains(&b'-') {
            gapped += 1;
        }
        conservation.push(m);
        let (residue, count) = consensus(&column);
        consensus_line.push(char::from(residue));
        agreement.push(u8::try_from(count * 100 / column.len()).unwrap_or(100));
    }
    let rows = records
        .iter()
        .map(|r| Row {
            id: r.id.clone(),
            description: r.description.clone(),
            aligned: r.residues.clone(),
            residues: r.residues.bytes().filter(|&b| b != b'-').count(),
        })
        .collect();
    Some(Alignment {
        rows,
        length,
        alphabet,
        conservation,
        consensus: consensus_line,
        agreement,
        identical,
        gapped,
    })
}

/// A record, as listed when the file isn't an alignment.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Listed {
    /// The header's first word.
    pub id: String,
    /// The rest of the header.
    pub description: String,
    /// How many residues it has, gaps left out.
    pub length: usize,
}

/// What the web app gets back.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Reading {
    /// The file is an alignment.
    Alignment(Alignment),
    /// The file's records, which aren't an alignment.
    Sequences {
        /// What its residues are.
        alphabet: Alphabet,
        /// Its records.
        records: Vec<Listed>,
    },
    /// The file couldn't be read.
    Error(Problem),
}

/// Reads a file's bytes as FASTA, and lays its records out as an alignment if they're one.
#[must_use]
pub fn read(bytes: &[u8]) -> Reading {
    if bytes.len() > MAX_BYTES {
        return Reading::Error(Problem {
            message: format!(
                "It's over {} MiB, the most Iki reads in a browser.",
                MAX_BYTES >> 20
            ),
            line: None,
        });
    }
    let Ok(text) = std::str::from_utf8(bytes) else {
        return Reading::Error(Problem {
            message: "It isn't text (UTF-8), as a FASTA file is.".to_owned(),
            line: None,
        });
    };
    let records = match parse_fasta(text) {
        Ok(records) => records,
        Err(problem) => return Reading::Error(problem),
    };
    if let Some(alignment) = layout(&records) {
        return Reading::Alignment(alignment);
    }
    Reading::Sequences {
        alphabet: alphabet(&records),
        records: records
            .into_iter()
            .map(|r| Listed {
                length: r.residues.bytes().filter(|&b| b != b'-').count(),
                id: r.id,
                description: r.description,
            })
            .collect(),
    }
}

/// [`read`], as JSON.
#[must_use]
pub fn read_json(bytes: &[u8]) -> Vec<u8> {
    serde_json::to_vec(&read(bytes)).unwrap_or_else(|_| {
        br#"{"kind":"error","message":"The reading couldn't be written out.","line":null}"#.to_vec()
    })
}

/// What [`check_json`] reads.
#[derive(Deserialize)]
struct CheckRequest {
    templates: Vec<String>,
}

fn failed(message: &str) -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({ "error": message }))
        .unwrap_or_else(|_| br#"{"error":"It couldn't be written out."}"#.to_vec())
}

/// [`templates::check`], from and to JSON: `{"templates": [...]}` in, `{"checked":
/// [...]}` out, or `{"error": ...}` if what came in isn't that.
#[must_use]
pub fn check_json(bytes: &[u8]) -> Vec<u8> {
    match serde_json::from_slice::<CheckRequest>(bytes) {
        Ok(request) => serde_json::to_vec(
            &serde_json::json!({ "checked": templates::check(&request.templates) }),
        )
        .unwrap_or_else(|_| failed("The check couldn't be written out.")),
        Err(error) => failed(&format!("The request isn't one: {error}.")),
    }
}

/// [`templates::render`], from and to JSON: a [`templates::Rendering`] in, `{"values":
/// [...]}` out (each template's values, one per row or just one), or `{"error": ...}`.
#[must_use]
pub fn render_json(bytes: &[u8]) -> Vec<u8> {
    match serde_json::from_slice::<templates::Rendering>(bytes) {
        Ok(rendering) => {
            serde_json::to_vec(&serde_json::json!({ "values": templates::render(&rendering) }))
                .unwrap_or_else(|_| failed("The values couldn't be written out."))
        }
        Err(error) => failed(&format!("The request isn't one: {error}.")),
    }
}

/// [`rules::check`], from and to JSON: a [`rules::CheckRequest`] in, `{"checked":
/// [...]}` out, or `{"error": ...}` if what came in isn't one.
#[must_use]
pub fn check_rules_json(bytes: &[u8]) -> Vec<u8> {
    match serde_json::from_slice::<rules::CheckRequest>(bytes) {
        Ok(request) => {
            serde_json::to_vec(&serde_json::json!({ "checked": rules::check(&request) }))
                .unwrap_or_else(|_| failed("The check couldn't be written out."))
        }
        Err(error) => failed(&format!("The request isn't one: {error}.")),
    }
}

/// [`rules::evaluate`], from and to JSON: a [`rules::EvaluateRequest`] in, `{"results":
/// [...]}` out (each context's outcomes, one per expression), or `{"error": ...}`.
#[must_use]
pub fn evaluate_rules_json(bytes: &[u8]) -> Vec<u8> {
    match serde_json::from_slice::<rules::EvaluateRequest>(bytes) {
        Ok(request) => {
            serde_json::to_vec(&serde_json::json!({ "results": rules::evaluate(&request) }))
                .unwrap_or_else(|_| failed("The outcomes couldn't be written out."))
        }
        Err(error) => failed(&format!("The request isn't one: {error}.")),
    }
}

/// The module's exports, for the web app's worker. It asks for a buffer of the file's size,
/// writes the file into it, calls `iki_read`, then reads the JSON from `iki_output` and
/// `iki_output_len`. The buffers live in this thread's state, so no export takes a pointer
/// from JavaScript.
#[allow(unsafe_code)] // `no_mangle` alone: the exports' names. No unsafe blocks.
pub mod exports {
    use std::cell::RefCell;

    thread_local! {
        static INPUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
        static OUTPUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
    }

    /// A buffer of `len` bytes for the file, and where it is in the module's memory.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_input(len: usize) -> *mut u8 {
        INPUT.with_borrow_mut(|input| {
            *input = vec![0; len.min(super::MAX_BYTES + 1)];
            input.as_mut_ptr()
        })
    }

    /// Reads the file in the input buffer, and frees it.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_read() {
        let bytes = INPUT.with_borrow_mut(std::mem::take);
        let json = super::read_json(&bytes);
        OUTPUT.with_borrow_mut(|output| *output = json);
    }

    /// Checks the templates in the input buffer (see [`super::check_json`]), and frees it.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_check() {
        let bytes = INPUT.with_borrow_mut(std::mem::take);
        let json = super::check_json(&bytes);
        OUTPUT.with_borrow_mut(|output| *output = json);
    }

    /// Renders the templates in the input buffer (see [`super::render_json`]), and frees it.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_render() {
        let bytes = INPUT.with_borrow_mut(std::mem::take);
        let json = super::render_json(&bytes);
        OUTPUT.with_borrow_mut(|output| *output = json);
    }

    /// Checks the rules in the input buffer (see [`super::check_rules_json`]), and frees it.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_check_rules() {
        let bytes = INPUT.with_borrow_mut(std::mem::take);
        let json = super::check_rules_json(&bytes);
        OUTPUT.with_borrow_mut(|output| *output = json);
    }

    /// Evaluates the rules in the input buffer (see [`super::evaluate_rules_json`]), and
    /// frees it.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_evaluate_rules() {
        let bytes = INPUT.with_borrow_mut(std::mem::take);
        let json = super::evaluate_rules_json(&bytes);
        OUTPUT.with_borrow_mut(|output| *output = json);
    }

    /// Where the last reading's JSON is.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_output() -> *const u8 {
        OUTPUT.with_borrow(Vec::as_ptr)
    }

    /// How long the last reading's JSON is.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_output_len() -> usize {
        OUTPUT.with_borrow(Vec::len)
    }

    /// Writes into the input buffer, as the browser does through the module's memory.
    #[cfg(test)]
    pub(super) fn write_input(bytes: &[u8]) {
        INPUT.with_borrow_mut(|input| input.copy_from_slice(bytes));
    }

    /// The last reading's JSON, as the browser reads it from the module's memory.
    #[cfg(test)]
    pub(super) fn output() -> Vec<u8> {
        OUTPUT.with_borrow(Clone::clone)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn records(text: &str) -> Vec<Record> {
        match parse_fasta(text) {
            Ok(records) => records,
            Err(problem) => unreachable!("{problem:?}"),
        }
    }

    #[test]
    fn reads_records_with_their_headers_and_residues() {
        let read = records(
            ">sp|P69905|HBA_HUMAN Hemoglobin subunit alpha\r\nmvls\nPADK\n\n;old comment\n>two\nAC-G.T*\n",
        );
        assert_eq!(read.len(), 2);
        assert_eq!(read[0].id, "sp|P69905|HBA_HUMAN");
        assert_eq!(read[0].description, "Hemoglobin subunit alpha");
        assert_eq!(read[0].residues, "MVLSPADK");
        assert_eq!(read[1].description, "");
        assert_eq!(read[1].residues, "AC-G-T*");
    }

    #[test]
    fn says_what_it_cant_read_and_where() {
        assert_eq!(
            parse_fasta("ACGT\n>x\n"),
            Err(Problem::at(
                1,
                "Line 1 comes before the first record's header (a line starting with >).".into()
            ))
        );
        let Err(problem) = parse_fasta(">x\nAC\nA1G\n") else {
            unreachable!()
        };
        assert_eq!(problem.line, Some(3));
        assert_eq!(
            problem.message,
            "Line 3 has '1' at column 2, which isn't a residue, a gap or a stop."
        );
        assert!(parse_fasta("\n\n").is_err());
        assert!(matches!(read(&[0xff, 0xfe]), Reading::Error(_)));
    }

    #[test]
    fn lays_out_an_alignment_with_clustal_marks() {
        // Columns: identical (M), strong (I/L in MILV), weak (S/G in SAG), none (W/D), gap.
        let aligned =
            layout(&records(">a\nMISW-K\n>b\nMLGDAK\n")).unwrap_or_else(|| unreachable!());
        assert_eq!(aligned.conservation, "*:.  *");
        assert_eq!(
            (aligned.identical, aligned.gapped, aligned.length),
            (2, 1, 6)
        );
        assert_eq!(aligned.alphabet, Alphabet::Protein);
        assert_eq!(aligned.rows[0].residues, 5);
    }

    #[test]
    fn says_how_much_each_column_of_a_multiple_alignment_agrees() {
        let aligned = layout(&records(">a\nMKV-\n>b\nMRV-\n>c\nMRL-\n>d\nM-LA\n"))
            .unwrap_or_else(|| unreachable!());
        // R beats K (2 to 1); V and L tie, and the first in the alphabet is taken.
        assert_eq!(aligned.consensus, "MRLA");
        assert_eq!(aligned.agreement, vec![100, 50, 50, 25]);
        // V and L are both in Clustal's MILV group; the last column has gaps.
        assert_eq!(aligned.conservation, "* : ");
    }

    #[test]
    fn nucleotides_are_marked_only_where_identical() {
        let aligned = layout(&records(">a\nACGT\n>b\nATGA\n")).unwrap_or_else(|| unreachable!());
        assert_eq!(aligned.alphabet, Alphabet::Nucleotide);
        assert_eq!(aligned.conservation, "* * ");
    }

    #[test]
    fn records_of_different_lengths_are_listed_not_aligned() {
        let reading = read(b">a\nMKV\n>b\nMK-VL\n");
        assert_eq!(
            reading,
            Reading::Sequences {
                alphabet: Alphabet::Protein,
                records: vec![
                    Listed {
                        id: "a".into(),
                        description: String::new(),
                        length: 3
                    },
                    Listed {
                        id: "b".into(),
                        description: String::new(),
                        length: 4
                    },
                ],
            }
        );
        assert!(layout(&records(">only\nMKV\n")).is_none());
    }

    #[test]
    fn its_json_says_what_kind_of_reading_it_is() {
        let json: serde_json::Value =
            serde_json::from_slice(&read_json(b">a\nMK\n>b\nMR\n")).unwrap_or_default();
        assert_eq!(json["kind"], "alignment");
        assert_eq!(json["conservation"], "*:");
        assert_eq!(json["agreement"], serde_json::json!([100, 50]));
        let error: serde_json::Value =
            serde_json::from_slice(&read_json(b"oops")).unwrap_or_default();
        assert_eq!(error["kind"], "error");
        assert_eq!(error["line"], 1);
    }

    #[test]
    fn the_exports_read_what_was_written_to_their_buffer() {
        let file = b">a\nMK\n>b\nMK\n";
        assert!(!exports::iki_input(file.len()).is_null());
        exports::write_input(file);
        exports::iki_read();
        assert_eq!(exports::iki_output_len(), exports::output().len());
        assert_eq!(exports::output(), read_json(file));
        assert!(!exports::iki_output().is_null());
    }

    #[test]
    fn the_exports_check_and_render_templates_as_json() {
        let request = br#"{"templates": ["{{ size(picked) + 1 }}", "{{ picked[ }}"]}"#;
        exports::iki_input(request.len());
        exports::write_input(request);
        exports::iki_check();
        let checked: serde_json::Value =
            serde_json::from_slice(&exports::output()).unwrap_or_default();
        assert_eq!(
            checked["checked"][0],
            serde_json::json!({"names": ["picked"], "fields": [], "whole": true})
        );
        assert!(checked["checked"][1]["error"].is_string());

        let request =
            br#"{"templates": ["{{ size(picked) + 1 }} picked"], "variables": {"picked": [1, 2]}}"#;
        exports::iki_input(request.len());
        exports::write_input(request);
        exports::iki_render();
        assert_eq!(exports::output(), br#"{"values":[[{"value":"3 picked"}]]}"#);
    }

    #[test]
    fn the_exports_check_and_evaluate_rules_as_json() {
        let variables = serde_json::json!({
            "record": {"type": "object", "label": "Sample", "fields": {
                "purity": {"type": "double"},
                "status": {"type": "string", "label": "Status", "values": ["received", "approved"]},
            }},
        });
        let expressions = serde_json::json!([
            {"source": "record.purity >= 95.0", "kind": "condition"},
            {"source": "record.status == 'aproved'", "kind": "condition"},
            {"source": "Purity {{ record.purity }}", "kind": "message"},
        ]);
        let request = serde_json::to_vec(
            &serde_json::json!({"variables": variables, "expressions": expressions}),
        )
        .unwrap_or_default();
        exports::iki_input(request.len());
        exports::write_input(&request);
        exports::iki_check_rules();
        let checked: serde_json::Value =
            serde_json::from_slice(&exports::output()).unwrap_or_default();
        assert_eq!(
            checked["checked"][0],
            serde_json::json!({"reads": {"record": ["purity"]}})
        );
        assert_eq!(
            checked["checked"][1],
            serde_json::json!({
                "error": "`aproved` isn't one of Status's values.",
                "hint": "Did you mean `approved`? It's one of `received`, `approved`.",
                "line": 1,
                "column": 18,
            })
        );
        assert_eq!(
            checked["checked"][2],
            serde_json::json!({"reads": {"record": ["purity"]}})
        );

        let request = serde_json::to_vec(&serde_json::json!({
            "variables": variables,
            "expressions": expressions,
            "contexts": [{"record": {"purity": 96, "status": "approved"}}, {"record": {}}],
        }))
        .unwrap_or_default();
        exports::iki_input(request.len());
        exports::write_input(&request);
        exports::iki_evaluate_rules();
        let results: serde_json::Value =
            serde_json::from_slice(&exports::output()).unwrap_or_default();
        assert_eq!(results["results"][0][0], serde_json::json!({"value": true}));
        assert_eq!(
            results["results"][0][2],
            serde_json::json!({"text": "Purity 96"})
        );
        assert_eq!(results["results"][1][0]["empty"], true);
        assert_eq!(results["results"][1][1]["empty"], false);
        assert_eq!(
            results["results"][1][2],
            serde_json::json!({"text": "Purity "})
        );

        let answer: serde_json::Value =
            serde_json::from_slice(&check_rules_json(br#"{"expressions": 1}"#)).unwrap_or_default();
        assert!(answer["error"].is_string());
        let answer: serde_json::Value =
            serde_json::from_slice(&evaluate_rules_json(b"[]")).unwrap_or_default();
        assert!(answer["error"].is_string());
    }

    #[test]
    fn a_request_that_isnt_one_says_so() {
        let answer: serde_json::Value =
            serde_json::from_slice(&check_json(b"[1]")).unwrap_or_default();
        assert!(
            answer["error"]
                .as_str()
                .unwrap_or_default()
                .starts_with("The request isn't one")
        );
        let answer: serde_json::Value =
            serde_json::from_slice(&render_json(b"{}")).unwrap_or_default();
        assert!(answer["error"].is_string());
    }
}
