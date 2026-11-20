//! HELM, the Pistoia Alliance's Hierarchical Editing Language for Macromolecules: reading
//! it, checking it, and writing its canonical form, which is an identity's (ADR 0143).
//!
//! The grammar is the HELM 2 specification's, version 2.04 (Pistoia Alliance, 1 November
//! 2018, <https://github.com/PistoiaHELM/HELMNotationStandard>):
//!
//! ```text
//! ListOfSimplePolymers$ListOfConnections$ListOfPolymerGroups$ExtendedAnnotation$V2.0
//! ```
//!
//! A simple polymer is `RNA1{R(A)P.R(C)P}`: its type (`PEPTIDE`, `RNA`, `CHEM` or `BLOB`)
//! and number, then its monomers. A multi-character monomer ID is in brackets (`[dR]`), a
//! branch monomer (a nucleotide's base) in parentheses after the backbone monomer it hangs
//! from, and `.` separates monomer units. `(A,G)` is one of several monomers, `(A+G)` a
//! mixture of them, each with an optional ratio (`A:0.5`); `'3'` or `'2-5'` after a unit
//! repeats it; `"…"` after a monomer, a unit or a polymer annotates it. A connection is
//! `RNA1,CHEM1,1:R1-1:R1`: a bond between two monomers' attachment points, each monomer by
//! its position (counting every monomer, a branch's too, from 1), or `pair` for the
//! hydrogen bonds of a base pair. A HELM 1 string has no `V2.0` and keeps base pairs in its
//! third section, and polymers' annotations in its fourth; it's read as HELM 2.
//!
//! The canonical form is what's the same however one molecule is written: monomer IDs
//! bracketed only when they need to be, repeats of a fixed count written out, the members
//! of a list in order, no annotations, the polymers in order of their notation (and the
//! connections that tie identical polymers), numbered again from 1, each connection's ends
//! and the connections in order, and `V2.0`.

use std::collections::{BTreeMap, HashMap};
use std::fmt::Write as _;

use serde::Serialize;

/// The longest HELM read, in bytes.
pub const MAX_LENGTH: usize = 1_000_000;
/// The most monomers a HELM may have, its repeats written out.
pub const MAX_MONOMERS: usize = 100_000;
/// The most ways of numbering identical polymers tried, to find the canonical one.
const MAX_ORDERINGS: usize = 40_320;

/// Why a HELM string isn't HELM, and where: `at` counts characters from 0.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Problem {
    /// What's wrong, for a person.
    pub message: String,
    /// The character it's at, from 0.
    pub at: usize,
}

/// A simple polymer's type.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum PolymerType {
    /// Amino acids.
    Peptide,
    /// Nucleotides, DNA as well as RNA.
    Rna,
    /// A chemical structure outside the other types: one monomer.
    Chem,
    /// Something HELM doesn't describe the structure of, such as a bead.
    Blob,
}

impl PolymerType {
    fn name(self) -> &'static str {
        match self {
            Self::Blob => "BLOB",
            Self::Chem => "CHEM",
            Self::Peptide => "PEPTIDE",
            Self::Rna => "RNA",
        }
    }
}

/// A polymer's ID, such as `RNA1`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct PolymerId {
    /// Its type.
    pub kind: PolymerType,
    /// Its number, from 1.
    pub number: u32,
}

impl std::fmt::Display for PolymerId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}{}", self.kind.name(), self.number)
    }
}

/// A monomer, as its ID or in-line SMILES.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Monomer {
    /// Its ID, without brackets, or its SMILES.
    pub id: String,
    /// It's given in-line, as SMILES with its attachment points, rather than by ID.
    pub smiles: bool,
}

/// One of a unit's parts.
#[derive(Debug, Clone, PartialEq)]
pub enum Element {
    /// A monomer.
    Monomer(Monomer),
    /// A branch monomer, hanging from the backbone monomer before it: a nucleotide's base.
    Branch(Box<Element>),
    /// One of several (`,`), or a mixture of them (`+`), each with its ratio if it's given.
    List {
        /// A mixture, not one of them.
        mixture: bool,
        /// Each member's elements, and its ratio.
        members: Vec<(Vec<Element>, Option<String>)>,
    },
    /// Units grouped to be repeated.
    Group(Vec<Unit>),
}

/// A repeat count: one number, or a range.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Repeat {
    /// The fewest.
    pub from: u32,
    /// The most.
    pub to: u32,
}

/// A monomer unit: what `.` separates, such as a nucleotide (`R(A)P`).
#[derive(Debug, Clone, PartialEq)]
pub struct Unit {
    /// Its monomers, in order.
    pub elements: Vec<Element>,
    /// How many times it's repeated, if it is.
    pub repeat: Option<Repeat>,
}

/// A simple polymer.
#[derive(Debug, Clone, PartialEq)]
pub struct Polymer {
    /// Its ID.
    pub id: PolymerId,
    /// Its units, or for a BLOB what it is.
    pub units: Vec<Unit>,
    /// A BLOB's text.
    pub blob: Option<String>,
}

/// Where a connection's end is: a monomer's position, or a monomer ID or list of them when
/// it's not known which, or `?`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Position {
    /// A monomer's position in its polymer, from 1.
    At(usize),
    /// Any of these, as written.
    Unknown(String),
}

/// One end of a connection.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct End {
    /// The polymer, or polymer group (`G1`), it's in.
    pub polymer: String,
    /// The monomer.
    pub position: Position,
    /// Its attachment point (`R1`), `pair` for a base pair's hydrogen bonds, or `?`.
    pub attachment: String,
}

/// A bond between two monomers, or a base pair.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Connection {
    /// One end.
    pub from: End,
    /// The other.
    pub to: End,
}

/// A polymer group: polymers (or groups) that are one of them, or a mixture.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Group {
    /// Its number (`G1`).
    pub number: u32,
    /// A mixture, not one of them.
    pub mixture: bool,
    /// Its members, by ID, each with its ratio.
    pub members: Vec<(String, Option<String>)>,
}

/// A HELM string, read.
#[derive(Debug, Clone, PartialEq)]
pub struct Helm {
    /// Its simple polymers.
    pub polymers: Vec<Polymer>,
    /// Its connections and base pairs.
    pub connections: Vec<Connection>,
    /// Its polymer groups.
    pub groups: Vec<Group>,
    /// It was HELM 1: no `V2.0`.
    pub version1: bool,
}

// --- Reading --------------------------------------------------------------------------------

struct Reader<'a> {
    text: &'a str,
    bytes: &'a [u8],
    at: usize,
    end: usize,
}

type Read<T> = Result<T, Problem>;

fn problem(text: &str, at: usize, message: impl Into<String>) -> Problem {
    Problem {
        message: message.into(),
        at: text[..at.min(text.len())].chars().count(),
    }
}

impl<'a> Reader<'a> {
    fn new(text: &'a str, start: usize, end: usize) -> Self {
        Self {
            text,
            bytes: text.as_bytes(),
            at: start,
            end,
        }
    }

    fn peek(&self) -> Option<u8> {
        (self.at < self.end).then(|| self.bytes[self.at])
    }

    fn fail<T>(&self, message: impl Into<String>) -> Read<T> {
        Err(problem(self.text, self.at, message))
    }

    fn expect(&mut self, c: u8, what: &str) -> Read<()> {
        if self.peek() == Some(c) {
            self.at += 1;
            Ok(())
        } else {
            let found = self.found();
            self.fail(format!("Expected {what}, but found {found}."))
        }
    }

    fn found(&self) -> String {
        match self.text[self.at.min(self.end)..self.end].chars().next() {
            Some(c) => format!("“{c}”"),
            None => "the end".to_owned(),
        }
    }

    fn digits(&mut self) -> Option<&'a str> {
        let start = self.at;
        while self.peek().is_some_and(|c| c.is_ascii_digit()) {
            self.at += 1;
        }
        (self.at > start).then(|| &self.text[start..self.at])
    }

    /// A ratio after `:`, such as `0.5`, written without trailing zeros.
    fn ratio(&mut self) -> Read<Option<String>> {
        if self.peek() != Some(b':') {
            return Ok(None);
        }
        self.at += 1;
        let Some(whole) = self.digits() else {
            return self.fail("A ratio after “:” must be a number, such as 0.5.");
        };
        let mut number = whole.trim_start_matches('0').to_owned();
        if number.is_empty() {
            number.push('0');
        }
        if self.peek() == Some(b'.') && self.bytes.get(self.at + 1).is_some_and(u8::is_ascii_digit)
        {
            self.at += 1;
            let fraction = self.digits().unwrap_or_default().trim_end_matches('0');
            if !fraction.is_empty() {
                number.push('.');
                number.push_str(fraction);
            }
        }
        Ok(Some(number))
    }

    /// Text between quotes: an annotation, which the canonical form leaves out.
    fn annotation(&mut self) -> Read<()> {
        if self.peek() != Some(b'"') {
            return Ok(());
        }
        let start = self.at;
        self.at += 1;
        while let Some(c) = self.peek() {
            self.at += 1;
            if c == b'"' {
                return Ok(());
            }
        }
        Err(problem(
            self.text,
            start,
            "This annotation's quotation mark isn't closed.",
        ))
    }

    /// The text between matching brackets, from just after `[`.
    fn bracketed(&mut self) -> Read<&'a str> {
        let open = self.at - 1;
        let start = self.at;
        let mut depth = 1;
        while let Some(c) = self.peek() {
            match c {
                b'[' => depth += 1,
                b']' => {
                    depth -= 1;
                    if depth == 0 {
                        let inside = &self.text[start..self.at];
                        self.at += 1;
                        return Ok(inside);
                    }
                }
                _ => {}
            }
            self.at += 1;
        }
        Err(problem(self.text, open, "This “[” isn't closed."))
    }

    fn monomer(&mut self) -> Read<Monomer> {
        let start = self.at;
        match self.peek() {
            Some(b'[') => {
                self.at += 1;
                let inside = self.bracketed()?;
                if inside.is_empty() {
                    return Err(problem(
                        self.text,
                        start,
                        "A monomer's ID can't be empty: “[]”.",
                    ));
                }
                let smiles = inside.contains('*') || inside.contains("|$");
                if let Some(c) = inside
                    .chars()
                    .find(|c| "$|{}.,:+\"'()[]".contains(*c) || c.is_whitespace())
                    .filter(|_| !smiles)
                {
                    return Err(problem(
                        self.text,
                        start,
                        format!(
                            "The monomer ID “{inside}” has “{c}” in it, which HELM keeps for \
                                 its own use."
                        ),
                    ));
                }
                Ok(Monomer {
                    id: inside.to_owned(),
                    smiles,
                })
            }
            Some(c) if c.is_ascii_alphanumeric() || c == b'*' || c == b'_' => {
                self.at += 1;
                Ok(Monomer {
                    id: (c as char).to_string(),
                    smiles: false,
                })
            }
            _ => {
                let found = self.found();
                self.fail(format!(
                    "Expected a monomer: its ID, in brackets if it's longer than one character, \
                     such as A or [dR]. Found {found}."
                ))
            }
        }
    }

    /// The elements of a unit, up to what ends it.
    fn elements(&mut self, kind: PolymerType, inside_list: bool) -> Read<Vec<Element>> {
        let mut elements: Vec<Element> = Vec::new();
        loop {
            match self.peek() {
                Some(b'(') => {
                    let open = self.at;
                    self.at += 1;
                    let element = self.parenthesised(kind)?;
                    self.expect(b')', "“)”")?;
                    let follows_monomer = matches!(elements.last(), Some(Element::Monomer(_)));
                    if kind == PolymerType::Rna && follows_monomer {
                        // A base, hanging from the sugar before it.
                        let branch = match element {
                            Element::Group(mut units)
                                if units.len() == 1
                                    && units[0].repeat.is_none()
                                    && units[0].elements.len() == 1 =>
                            {
                                units.remove(0).elements.remove(0)
                            }
                            Element::Group(_) => {
                                return Err(problem(
                                    self.text,
                                    open,
                                    "A branch holds one monomer, or a list of them.",
                                ));
                            }
                            other => other,
                        };
                        elements.push(Element::Branch(Box::new(branch)));
                    } else if !elements.is_empty() && !matches!(element, Element::List { .. }) {
                        return Err(problem(
                            self.text,
                            open,
                            if kind == PolymerType::Rna {
                                "A branch hangs from the monomer before it, such as the base \
                                 in R(A)P."
                            } else {
                                "Only RNA has branch monomers: separate a PEPTIDE's monomers \
                                 with “.”."
                            },
                        ));
                    } else {
                        elements.push(element);
                    }
                }
                Some(b'[' | b'*' | b'_') => elements.push(Element::Monomer(self.monomer()?)),
                Some(c) if c.is_ascii_alphanumeric() => {
                    elements.push(Element::Monomer(self.monomer()?));
                }
                _ => break,
            }
            self.annotation()?;
            if kind != PolymerType::Rna && !inside_list && elements.len() > 1 {
                return self.fail(format!(
                    "A {} polymer's monomers are separated by “.”.",
                    kind.name()
                ));
            }
        }
        if elements.is_empty() {
            let found = self.found();
            return self.fail(format!("Expected a monomer, but found {found}."));
        }
        if matches!(elements.first(), Some(Element::Branch(_))) {
            return self.fail("A unit can't start with a branch.");
        }
        Ok(elements)
    }

    /// What's between parentheses that don't hold a branch alone: a list of monomers, or
    /// units grouped to repeat.
    fn parenthesised(&mut self, kind: PolymerType) -> Read<Element> {
        let first = self.elements(kind, true)?;
        let ratio = self.ratio()?;
        let Some(sign @ (b',' | b'+')) = self.peek() else {
            if ratio.is_some() {
                return self.fail("A ratio is for a list's members, such as (A:1+G:1).");
            }
            let repeat = self.repeat()?;
            self.annotation()?;
            let mut units = vec![Unit {
                elements: first,
                repeat,
            }];
            while self.peek() == Some(b'.') {
                self.at += 1;
                units.push(self.unit(kind)?);
            }
            return Ok(Element::Group(units));
        };
        let mixture = sign == b'+';
        let mut members = vec![(first, ratio)];
        while let Some(next @ (b',' | b'+')) = self.peek() {
            if (next == b'+') != mixture {
                return self.fail(
                    "A list is one of its members (“,”) or a mixture of them (“+”), not both.",
                );
            }
            self.at += 1;
            let elements = self.elements(kind, true)?;
            let ratio = self.ratio()?;
            members.push((elements, ratio));
        }
        Ok(Element::List { mixture, members })
    }

    fn repeat(&mut self) -> Read<Option<Repeat>> {
        if self.peek() != Some(b'\'') {
            return Ok(None);
        }
        let start = self.at;
        self.at += 1;
        let number = |r: &mut Self| -> Read<u32> {
            match r.digits().map(str::parse::<u32>) {
                Some(Ok(n)) if n > 0 => Ok(n),
                _ => Err(problem(
                    r.text,
                    start,
                    "A repeat is a count, or a range, between single quotes, such as '3' or \
                     '2-5'.",
                )),
            }
        };
        let from = number(self)?;
        let to = if self.peek() == Some(b'-') {
            self.at += 1;
            number(self)?
        } else {
            from
        };
        self.expect(b'\'', "the repeat's closing “'”")?;
        if to < from {
            return Err(problem(
                self.text,
                start,
                "A repeat's range runs from the fewer to the more, such as '2-5'.",
            ));
        }
        Ok(Some(Repeat { from, to }))
    }

    fn unit(&mut self, kind: PolymerType) -> Read<Unit> {
        let elements = self.elements(kind, false)?;
        let repeat = self.repeat()?;
        self.annotation()?;
        Ok(Unit { elements, repeat })
    }

    fn polymer_id(&mut self) -> Read<PolymerId> {
        let start = self.at;
        let rest = &self.text[self.at..self.end];
        let kind = [
            ("PEPTIDE", PolymerType::Peptide),
            ("RNA", PolymerType::Rna),
            ("CHEM", PolymerType::Chem),
            ("BLOB", PolymerType::Blob),
        ]
        .into_iter()
        .find(|(name, _)| rest.starts_with(name));
        let Some((name, kind)) = kind else {
            return self.fail(
                "Expected a polymer's ID: its type (PEPTIDE, RNA, CHEM or BLOB) and number, \
                 such as RNA1.",
            );
        };
        self.at += name.len();
        match self.digits().map(str::parse::<u32>) {
            Some(Ok(number)) if number > 0 => Ok(PolymerId { kind, number }),
            _ => Err(problem(
                self.text,
                start,
                format!("A {name} polymer's ID needs its number, from 1, such as {name}1."),
            )),
        }
    }

    fn polymer(&mut self) -> Read<Polymer> {
        let start = self.at;
        let id = self.polymer_id()?;
        self.expect(b'{', "“{” after the polymer's ID")?;
        let mut polymer = Polymer {
            id,
            units: Vec::new(),
            blob: None,
        };
        if id.kind == PolymerType::Blob {
            let from = self.at;
            while self.peek().is_some_and(|c| c != b'}') {
                self.at += 1;
            }
            polymer.blob = Some(self.text[from..self.at].to_owned());
        } else {
            polymer.units.push(self.unit(id.kind)?);
            while self.peek() == Some(b'.') {
                self.at += 1;
                polymer.units.push(self.unit(id.kind)?);
            }
        }
        if self.peek() != Some(b'}') {
            let found = self.found();
            return self.fail(format!(
                "Expected “.” between units, or “}}” to end {id}, but found {found}."
            ));
        }
        self.at += 1;
        self.annotation()?;
        if id.kind == PolymerType::Chem && (polymer.units.len() != 1 || chem_problem(&polymer)) {
            return Err(problem(
                self.text,
                start,
                format!(
                    "{id} is a CHEM polymer, which holds one monomer: connect several with \
                     connections."
                ),
            ));
        }
        Ok(polymer)
    }

    fn end(&mut self) -> Read<End> {
        let polymer = self.connection_member()?;
        Ok(End {
            polymer,
            position: Position::At(0),
            attachment: String::new(),
        })
    }

    /// A polymer's or a group's ID.
    fn connection_member(&mut self) -> Read<String> {
        if self.peek() == Some(b'G') {
            let start = self.at;
            self.at += 1;
            return match self.digits().map(str::parse::<u32>) {
                Some(Ok(n)) if n > 0 => Ok(format!("G{n}")),
                _ => Err(problem(
                    self.text,
                    start,
                    "A group's ID is G and its number, such as G1.",
                )),
            };
        }
        Ok(self.polymer_id()?.to_string())
    }

    fn position(&mut self) -> Read<Position> {
        let start = self.at;
        if let Some(digits) = self.digits() {
            return match digits.parse::<usize>() {
                Ok(n) if n > 0 => Ok(Position::At(n)),
                _ => Err(problem(
                    self.text,
                    start,
                    "A monomer's position counts from 1.",
                )),
            };
        }
        if self.peek() == Some(b'?') {
            self.at += 1;
            return Ok(Position::Unknown("?".to_owned()));
        }
        if self.peek() == Some(b'(') {
            let mut depth = 0;
            while let Some(c) = self.peek() {
                self.at += 1;
                match c {
                    b'(' => depth += 1,
                    b')' => {
                        depth -= 1;
                        if depth == 0 {
                            return Ok(Position::Unknown(self.text[start..self.at].to_owned()));
                        }
                    }
                    _ => {}
                }
            }
            return Err(problem(self.text, start, "This “(” isn't closed."));
        }
        let monomer = self.monomer()?;
        Ok(Position::Unknown(write_monomer(&monomer)))
    }

    fn attachment(&mut self) -> Read<String> {
        let start = self.at;
        let rest = &self.text[self.at..self.end];
        if rest.starts_with("pair") {
            self.at += 4;
            return Ok("pair".to_owned());
        }
        if self.peek() == Some(b'?') {
            self.at += 1;
            return Ok("?".to_owned());
        }
        if self.peek() == Some(b'R') {
            self.at += 1;
            if let Some(Ok(n @ 1..)) = self.digits().map(str::parse::<u32>) {
                return Ok(format!("R{n}"));
            }
        }
        Err(problem(
            self.text,
            start,
            "An attachment point is R and its number, such as R1, or pair for a base pair, or ?.",
        ))
    }

    fn connection(&mut self) -> Read<Connection> {
        let mut from = self.end()?;
        self.expect(b',', "“,” after the connection's first polymer")?;
        let mut to = self.end()?;
        self.expect(b',', "“,” before the connection's monomers")?;
        from.position = self.position()?;
        self.expect(b':', "“:” before the attachment point")?;
        from.attachment = self.attachment()?;
        self.expect(b'-', "“-” between the connection's two ends")?;
        to.position = self.position()?;
        self.expect(b':', "“:” before the attachment point")?;
        to.attachment = self.attachment()?;
        self.annotation()?;
        Ok(Connection { from, to })
    }

    fn group(&mut self) -> Read<Group> {
        let start = self.at;
        self.expect(b'G', "a group's ID, such as G1")?;
        let number = match self.digits().map(str::parse::<u32>) {
            Some(Ok(n)) if n > 0 => n,
            _ => {
                return Err(problem(
                    self.text,
                    start,
                    "A group's ID is G and its number, such as G1.",
                ));
            }
        };
        self.expect(b'(', "“(” after the group's ID")?;
        let mut members = vec![(self.connection_member()?, self.ratio()?)];
        let mut mixture = None;
        while let Some(sign @ (b',' | b'+')) = self.peek() {
            if mixture.is_some_and(|m| m != (sign == b'+')) {
                return self
                    .fail("A group is one of its members (“,”) or a mixture (“+”), not both.");
            }
            mixture = Some(sign == b'+');
            self.at += 1;
            members.push((self.connection_member()?, self.ratio()?));
        }
        self.expect(b')', "“)” to end the group")?;
        self.annotation()?;
        Ok(Group {
            number,
            mixture: mixture.unwrap_or(true),
            members,
        })
    }

    fn list_of<T>(&mut self, mut item: impl FnMut(&mut Self) -> Read<T>) -> Read<Vec<T>> {
        let mut items = Vec::new();
        if self.at >= self.end {
            return Ok(items);
        }
        items.push(item(self)?);
        while self.peek() == Some(b'|') {
            self.at += 1;
            items.push(item(self)?);
        }
        if self.at < self.end {
            let found = self.found();
            return self.fail(format!(
                "Expected “|” or the section's “$”, but found {found}."
            ));
        }
        Ok(items)
    }
}

fn chem_problem(polymer: &Polymer) -> bool {
    polymer.units.iter().any(|u| {
        u.repeat.is_some() || u.elements.len() != 1 || matches!(u.elements[0], Element::Group(_))
    })
}

/// Where each of the four `$` that end HELM's sections is, outside brackets, braces and
/// quotes (in-line SMILES can hold `$`).
fn sections(text: &str) -> Read<Vec<usize>> {
    let mut found = Vec::new();
    let (mut squares, mut curlies, mut quoted) = (Vec::new(), Vec::new(), false);
    for (i, c) in text.bytes().enumerate() {
        match c {
            b'"' => quoted = !quoted,
            _ if quoted => {}
            b'[' => squares.push(i),
            b']' => {
                if squares.pop().is_none() {
                    return Err(problem(text, i, "This “]” closes no “[”."));
                }
            }
            b'{' if squares.is_empty() => curlies.push(i),
            b'}' if squares.is_empty() => {
                curlies.pop();
            }
            b'$' if squares.is_empty() && curlies.is_empty() => found.push(i),
            _ => {}
        }
    }
    if let Some(&open) = squares.first() {
        return Err(problem(text, open, "This “[” isn't closed."));
    }
    if let Some(&open) = curlies.first() {
        return Err(problem(text, open, "This “{” isn't closed."));
    }
    if found.len() < 4 {
        return Err(problem(
            text,
            text.len(),
            format!(
                "HELM has four sections, each ended by “$”: its polymers, connections, groups \
                 and annotation, as in RNA1{{R(A)P.R(C)}}$$$$. This has {}.",
                found.len()
            ),
        ));
    }
    if found.len() > 4 {
        return Err(problem(
            text,
            found[4],
            "HELM has four sections: this “$” is a fifth.",
        ));
    }
    Ok(found)
}

/// Reads HELM (2, or 1), checking it.
///
/// # Errors
///
/// What isn't HELM, and where.
pub fn read(text: &str) -> Read<Helm> {
    if text.len() > MAX_LENGTH {
        return Err(problem(
            text,
            0,
            format!("It's over {MAX_LENGTH} characters, the most HELM Iki reads."),
        ));
    }
    if text.trim().is_empty() {
        return Err(problem(
            text,
            0,
            "It's empty: HELM starts with a polymer, such as RNA1{R(A)P}.",
        ));
    }
    // Annotations, and in-line SMILES (as CXSMILES), may hold spaces; nothing else may.
    let (mut quoted, mut square) = (false, 0_i32);
    for (i, c) in text.char_indices() {
        if c == '"' {
            quoted = !quoted;
        } else if c == '[' && !quoted {
            square += 1;
        } else if c == ']' && !quoted {
            square -= 1;
        } else if !quoted && (c.is_control() || (square == 0 && c.is_whitespace())) {
            let what = match c {
                '\n' | '\r' => "a line break",
                c if c.is_whitespace() => "a space",
                _ => "a control character",
            };
            return Err(problem(
                text,
                i,
                format!("HELM has no spaces or line breaks: this is {what}."),
            ));
        }
    }
    let ends = sections(text)?;
    let version = &text[ends[3] + 1..];
    let version1 = match version {
        "" => true,
        "V2.0" => false,
        _ => {
            return Err(problem(
                text,
                ends[3] + 1,
                format!(
                    "After the fourth “$” comes V2.0 for HELM 2, or nothing for HELM 1, not “{version}”."
                ),
            ));
        }
    };
    let mut reader = Reader::new(text, 0, ends[0]);
    if reader.at >= reader.end {
        return Err(problem(
            text,
            0,
            "It has no polymers: HELM starts with one, such as RNA1{R(A)P}.",
        ));
    }
    let polymers = reader.list_of(Reader::polymer)?;
    let mut reader = Reader::new(text, ends[0] + 1, ends[1]);
    let mut connections = reader.list_of(Reader::connection)?;
    let mut reader = Reader::new(text, ends[1] + 1, ends[2]);
    let groups = if version1 {
        // HELM 1's third section is its base pairs.
        connections.extend(reader.list_of(Reader::connection)?);
        Vec::new()
    } else {
        reader.list_of(Reader::group)?
    };
    let annotation = &text[ends[2] + 1..ends[3]];
    if !version1
        && !annotation.is_empty()
        && serde_json::from_str::<serde_json::Value>(annotation).is_err()
    {
        return Err(problem(
            text,
            ends[2] + 1,
            "HELM 2's fourth section, its annotation, is JSON.",
        ));
    }
    let helm = Helm {
        polymers,
        connections,
        groups,
        version1,
    };
    check(text, &helm, ends[0] + 1)?;
    Ok(helm)
}

/// What the grammar alone doesn't: IDs used once, connections naming polymers (and
/// positions) that are there.
fn check(text: &str, helm: &Helm, connections_at: usize) -> Read<()> {
    let mut seen = HashMap::new();
    let mut total = 0_usize;
    for polymer in &helm.polymers {
        if seen
            .insert(polymer.id.to_string(), count(&polymer.units))
            .is_some()
        {
            return Err(problem(
                text,
                0,
                format!("There are two polymers called {}.", polymer.id),
            ));
        }
        total = total.saturating_add(expanded(&polymer.units));
    }
    if total > MAX_MONOMERS {
        return Err(problem(
            text,
            0,
            format!(
                "It has {total} monomers, its repeats written out: more than the {MAX_MONOMERS} Iki reads."
            ),
        ));
    }
    let groups: Vec<String> = helm
        .groups
        .iter()
        .map(|g| format!("G{}", g.number))
        .collect();
    for (i, g) in groups.iter().enumerate() {
        if groups[..i].contains(g) {
            return Err(problem(
                text,
                0,
                format!("There are two groups called {g}."),
            ));
        }
    }
    for group in &helm.groups {
        for (member, _) in &group.members {
            if !seen.contains_key(member) && !groups.contains(member) {
                return Err(problem(
                    text,
                    0,
                    format!(
                        "G{} names {member}, which isn't one of its polymers or groups.",
                        group.number
                    ),
                ));
            }
        }
    }
    for connection in &helm.connections {
        for end in [&connection.from, &connection.to] {
            let known = seen.get(&end.polymer);
            if known.is_none() && !groups.contains(&end.polymer) {
                return Err(problem(
                    text,
                    connections_at,
                    format!(
                        "A connection names {}, which isn't one of its polymers.",
                        end.polymer
                    ),
                ));
            }
            if let (Some(&monomers), Position::At(n)) = (known, &end.position)
                && *n > monomers
            {
                return Err(problem(
                    text,
                    connections_at,
                    format!(
                        "A connection names monomer {n} of {}, which has {monomers}.",
                        end.polymer
                    ),
                ));
            }
        }
        if (connection.from.attachment == "pair") != (connection.to.attachment == "pair") {
            return Err(problem(
                text,
                connections_at,
                "A base pair is “pair” at both ends, such as RNA1,RNA2,2:pair-5:pair.",
            ));
        }
    }
    Ok(())
}

/// How many monomers the units have, as connections count them: each written once, a list
/// or a group of units as one (HELM 2.04, 5.2.3, as its reference parser counts them).
fn count(units: &[Unit]) -> usize {
    units
        .iter()
        .map(|u| u.elements.iter().map(element_count).sum::<usize>())
        .sum()
}

fn element_count(element: &Element) -> usize {
    match element {
        Element::Monomer(_) | Element::List { .. } | Element::Group(_) => 1,
        Element::Branch(inner) => element_count(inner),
    }
}

/// How many monomers the units have with their repeats written out (the most of a range).
fn expanded(units: &[Unit]) -> usize {
    units
        .iter()
        .map(|u| {
            let one: usize = u
                .elements
                .iter()
                .map(|e| match e {
                    Element::Group(inner) => expanded(inner),
                    other => element_count(other),
                })
                .sum();
            one.saturating_mul(u.repeat.map_or(1, |r| r.to as usize))
        })
        .fold(0, usize::saturating_add)
}

// --- Writing --------------------------------------------------------------------------------

fn write_monomer(monomer: &Monomer) -> String {
    let single = monomer.id.chars().count() == 1 && !monomer.smiles;
    if single {
        monomer.id.clone()
    } else {
        format!("[{}]", monomer.id)
    }
}

fn write_elements(elements: &[Element], out: &mut String) {
    for element in elements {
        write_element(element, out);
    }
}

fn write_element(element: &Element, out: &mut String) {
    match element {
        Element::Monomer(m) => out.push_str(&write_monomer(m)),
        Element::Branch(inner) => {
            out.push('(');
            match inner.as_ref() {
                Element::List { .. } => {
                    // Written with its own parentheses: drop the branch's.
                    out.pop();
                    write_element(inner, out);
                }
                other => {
                    write_element(other, out);
                    out.push(')');
                }
            }
        }
        Element::List { mixture, members } => {
            let mut written: Vec<String> = members
                .iter()
                .map(|(elements, ratio)| {
                    let mut text = String::new();
                    write_elements(elements, &mut text);
                    if let Some(r) = ratio {
                        let _ = write!(text, ":{r}");
                    }
                    text
                })
                .collect();
            written.sort();
            out.push('(');
            out.push_str(&written.join(if *mixture { "+" } else { "," }));
            out.push(')');
        }
        Element::Group(units) => {
            out.push('(');
            write_units(units, out);
            out.push(')');
        }
    }
}

fn write_units(units: &[Unit], out: &mut String) {
    for (i, unit) in units.iter().enumerate() {
        if i > 0 {
            out.push('.');
        }
        write_elements(&unit.elements, out);
        if let Some(r) = unit.repeat {
            if r.from == r.to {
                let _ = write!(out, "'{}'", r.from);
            } else {
                let _ = write!(out, "'{}-{}'", r.from, r.to);
            }
        }
    }
}

/// Where a monomer, by its position as written, is in the canonical form.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Place {
    /// At this position, from 1, before a ring is turned.
    At(usize),
    /// Inside a repeat, which has no one place once it's written out.
    Repeated,
    /// A missing monomer (`_`), which the canonical form leaves out.
    Missing,
}

/// The units with every repeat of a fixed count written out, missing monomers left out
/// (if `drop_missing`) and lists in order, and where each monomer (by its position as
/// written, from 1) is now.
fn expand(units: &[Unit], drop_missing: bool) -> (Vec<Unit>, Vec<Place>) {
    let mut out = Vec::new();
    let mut places = Vec::new();
    let mut at = 0_usize;
    expand_into(units, &mut out, &mut places, &mut at, false, drop_missing);
    (out, places)
}

/// A missing monomer (`_`), on its own or as a branch. HELM's ambiguous monomers say it
/// adds nothing there (`A._.G` is `A.G`, and `R(_)P` is `RP`), unlike `_` in a list, which
/// says a monomer may be missing.
fn is_missing(element: &Element) -> bool {
    match element {
        Element::Monomer(m) => !m.smiles && m.id == "_",
        Element::Branch(inner) => is_missing(inner),
        _ => false,
    }
}

fn expand_into(
    units: &[Unit],
    out: &mut Vec<Unit>,
    places: &mut Vec<Place>,
    at: &mut usize,
    repeated: bool,
    drop_missing: bool,
) {
    for unit in units {
        let times = match unit.repeat {
            Some(r) if r.from == r.to => r.from as usize,
            _ => 1,
        };
        let ranged = unit.repeat.is_some_and(|r| r.from != r.to);
        let inside = repeated || times > 1 || ranged;
        let flat = unit.elements.len() == 1 && matches!(unit.elements[0], Element::Group(_));
        if flat && !ranged {
            // A group of units repeated a fixed number of times: its units, that many times.
            // It's one position as written, and has no one place once it's written out
            // unless it's a single monomer, once.
            if let Element::Group(inner) = &unit.elements[0] {
                let before = *at;
                for _ in 0..times {
                    expand_into(inner, out, &mut Vec::new(), at, true, drop_missing);
                }
                places.push(if !inside && *at == before + 1 {
                    Place::At(*at)
                } else {
                    Place::Repeated
                });
            }
            continue;
        }
        // Each element is one position as written. A unit that's only `_` is left out, and
        // so is a branch that's `_`.
        let whole = drop_missing && unit.elements.len() == 1 && is_missing(&unit.elements[0]);
        let mut kept: Vec<Element> = Vec::new();
        for element in &unit.elements {
            let branch = matches!(element, Element::Branch(_));
            if whole || (drop_missing && branch && is_missing(element)) {
                places.push(Place::Missing);
                continue;
            }
            kept.push(normalise(element, drop_missing));
            places.push(if inside {
                Place::Repeated
            } else {
                Place::At(*at + kept.len())
            });
        }
        if kept.is_empty() {
            continue;
        }
        let n = kept.len();
        for _ in 0..times {
            out.push(Unit {
                elements: kept.clone(),
                repeat: if ranged { unit.repeat } else { None },
            });
            *at += n;
        }
    }
}

/// An element as the canonical form writes it. A list without ratios has each member
/// once, as HELM's mixtures say `(A+A+L+L+A+V)` "is exactly the same as" `(A+L+V)`, and a
/// list of one monomer is that monomer; a group repeated a range of counts has its own
/// repeats written out.
fn normalise(element: &Element, drop_missing: bool) -> Element {
    match element {
        Element::Monomer(_) => element.clone(),
        Element::Branch(inner) => Element::Branch(Box::new(normalise(inner, drop_missing))),
        Element::Group(units) => {
            let (inner, _) = expand(units, drop_missing);
            Element::Group(if inner.is_empty() {
                units.clone()
            } else {
                inner
            })
        }
        Element::List { mixture, members } => {
            let mut members: Vec<(Vec<Element>, Option<String>)> = members
                .iter()
                .map(|(elements, ratio)| {
                    let elements = elements.iter().map(|e| normalise(e, drop_missing));
                    (elements.collect(), ratio.clone())
                })
                .collect();
            if members.iter().any(|(_, ratio)| ratio.is_some()) {
                return Element::List {
                    mixture: *mixture,
                    members,
                };
            }
            let mut seen = std::collections::HashSet::new();
            members.retain(|(elements, _)| {
                let mut text = String::new();
                write_elements(elements, &mut text);
                seen.insert(text)
            });
            if let [(only, _)] = members.as_slice()
                && let [Element::Monomer(m)] = only.as_slice()
            {
                return Element::Monomer(m.clone());
            }
            Element::List {
                mixture: *mixture,
                members,
            }
        }
    }
}

/// A polymer, canonical: its text without its number, where its written positions went,
/// and, if it's a ring, how it may be turned.
struct Written {
    text: String,
    places: Vec<Place>,
    ring: Option<Ring>,
}

/// A polymer whose last backbone monomer is joined to its first, R2 to R1: a ring, such
/// as a head-to-tail cyclic peptide or a cyclic oligonucleotide. It's one molecule from
/// whichever unit it's written from, so it's written from the one whose units read least;
/// a ring that repeats reads least from several, and each is tried, as identical polymers'
/// numberings are, for the connections that read least.
struct Ring {
    /// How many positions it has.
    total: usize,
    /// Its last backbone monomer's position, unturned.
    last: usize,
    /// Each way it's tried: how many positions, unturned, come before the unit it's
    /// written from, and its last backbone monomer's position once turned.
    turns: Vec<(usize, usize)>,
}

impl Ring {
    /// Where a position, unturned, is once it's turned the `turn`th way.
    fn turned(&self, turn: usize, position: usize) -> usize {
        let before = self.turns[turn].0;
        if position > before {
            position - before
        } else {
            position + self.total - before
        }
    }
}

fn canonical_polymer(polymer: &Polymer, connections: &[Connection]) -> Written {
    let mut text = String::new();
    text.push_str(polymer.id.kind.name());
    text.push('{');
    if let Some(blob) = &polymer.blob {
        text.push_str(blob);
        text.push('}');
        return Written {
            text,
            places: Vec::new(),
            ring: None,
        };
    }
    let (mut units, mut places) = expand(&polymer.units, true);
    if units.is_empty() {
        // Only missing monomers: kept, since a polymer has one.
        (units, places) = expand(&polymer.units, false);
    }
    let ring = ring(polymer, &mut units, &places, connections);
    write_units(&units, &mut text);
    text.push('}');
    Written { text, places, ring }
}

/// The least rotation of `items`: where to start for the sequence that reads least
/// (Zhou Yuan's minimum expression, in linear time).
fn least_rotation(items: &[usize]) -> usize {
    let count = items.len();
    let (mut one, mut other, mut along) = (0, 1, 0);
    while one < count && other < count && along < count {
        let (here, there) = (items[(one + along) % count], items[(other + along) % count]);
        if here == there {
            along += 1;
            continue;
        }
        if here > there {
            one += along + 1;
        } else {
            other += along + 1;
        }
        if one == other {
            other += 1;
        }
        along = 0;
    }
    one.min(other)
}

/// If the polymer is a ring, its units turned to the rotation that reads least, and how
/// it may be turned.
fn ring(
    polymer: &Polymer,
    units: &mut [Unit],
    places: &[Place],
    connections: &[Connection],
) -> Option<Ring> {
    let unrepeated = units
        .iter()
        .all(|u| u.repeat.is_none() && !u.elements.iter().any(|e| matches!(e, Element::Group(_))));
    if !matches!(polymer.id.kind, PolymerType::Peptide | PolymerType::Rna)
        || units.len() < 2
        || !unrepeated
    {
        return None;
    }
    let id = polymer.id.to_string();
    let starts: Vec<usize> = units
        .iter()
        .scan(0, |at, u| {
            let start = *at;
            *at += u.elements.len();
            Some(start)
        })
        .collect();
    let total: usize = units.iter().map(|u| u.elements.len()).sum();
    let last_backbone = |u: usize| {
        let elements = &units[u].elements;
        let at = elements
            .iter()
            .rposition(|e| !matches!(e, Element::Branch(_)))
            .unwrap_or(0);
        starts[u] + at + 1
    };
    let last = last_backbone(units.len() - 1);
    let place = |p: &Position| match p {
        Position::At(n) => match places.get(n - 1) {
            Some(Place::At(now)) => Some(*now),
            _ => None,
        },
        Position::Unknown(_) => None,
    };
    let (mut closed, mut others) = (false, false);
    for c in connections {
        let (from, to) = (c.from.polymer == id, c.to.polymer == id);
        if !from && !to {
            continue;
        }
        let ends = [
            (place(&c.from.position), c.from.attachment.as_str()),
            (place(&c.to.position), c.to.attachment.as_str()),
        ];
        let closing = from
            && to
            && (ends == [(Some(1), "R1"), (Some(last), "R2")]
                || ends == [(Some(last), "R2"), (Some(1), "R1")]);
        if closing {
            closed = true;
        } else {
            others = true;
        }
    }
    if !closed {
        return None;
    }
    let texts: Vec<String> = units
        .iter()
        .map(|u| {
            let mut text = String::new();
            write_units(std::slice::from_ref(u), &mut text);
            text
        })
        .collect();
    let mut sorted: Vec<&String> = texts.iter().collect();
    sorted.sort();
    sorted.dedup();
    let ranks: Vec<usize> = texts
        .iter()
        .map(|t| sorted.binary_search(&t).unwrap_or_default())
        .collect();
    let n = ranks.len();
    let start = least_rotation(&ranks);
    let period = (1..=n)
        .find(|&p| n.is_multiple_of(p) && (0..n).all(|i| ranks[i] == ranks[(i + p) % n]))
        .unwrap_or(n);
    // Only another connection to it can tell its equal rotations apart.
    let tried = if others { n / period } else { 1 };
    let turns = (0..tried)
        .map(|j| {
            let k = (start + j * period) % n;
            let before = starts[k];
            let end = last_backbone((k + n - 1) % n);
            let end = if end > before {
                end - before
            } else {
                end + total - before
            };
            (before, end)
        })
        .collect();
    units.rotate_left(start);
    Some(Ring { total, last, turns })
}

/// The canonical form of HELM already read.
///
/// # Errors
///
/// When it can't be put in order: a connection to a monomer inside a repeat, which has no
/// one position once the repeat is written out, or to a missing one, or too many
/// identical polymers to number.
pub fn canonical(text: &str, helm: &Helm) -> Read<String> {
    Ok(canonical_parts(text, helm)?.0)
}

/// The canonical form, and its connections, each written.
fn canonical_parts(text: &str, helm: &Helm) -> Read<(String, Vec<String>)> {
    let written: Vec<Written> = helm
        .polymers
        .iter()
        .map(|p| canonical_polymer(p, &helm.connections))
        .collect();
    // The polymers in order of their type and notation; identical ones tie.
    let mut order: Vec<usize> = (0..helm.polymers.len()).collect();
    order.sort_by(|&a, &b| {
        (helm.polymers[a].id.kind, &written[a].text)
            .cmp(&(helm.polymers[b].id.kind, &written[b].text))
    });
    let mut ties: Vec<Vec<usize>> = Vec::new();
    for &i in &order {
        match ties.last_mut() {
            Some(last)
                if helm.polymers[last[0]].id.kind == helm.polymers[i].id.kind
                    && written[last[0]].text == written[i].text =>
            {
                last.push(i);
            }
            _ => ties.push(vec![i]),
        }
    }
    let turns: Vec<usize> = written
        .iter()
        .map(|w| w.ring.as_ref().map_or(1, |r| r.turns.len()))
        .collect();
    let orderings = ties
        .iter()
        .map(|t| (1..=t.len()).product::<usize>())
        .chain(turns.iter().copied())
        .try_fold(1_usize, usize::checked_mul)
        .unwrap_or(usize::MAX);
    if orderings > MAX_ORDERINGS {
        return Err(problem(
            text,
            0,
            "It has too many identical polymers, or a ring that repeats too often, for Iki to \
             put their connections in one order.",
        ));
    }
    let mut best: Option<(String, Vec<String>, String)> = None;
    let mut permutation: Vec<Vec<usize>> = ties.clone();
    loop {
        let numbered: Vec<usize> = permutation.iter().flatten().copied().collect();
        let mut turn = vec![0; written.len()];
        loop {
            let (connections, groups) = connections_for(text, helm, &written, &numbered, &turn)?;
            let joined = connections.join("|");
            if best
                .as_ref()
                .is_none_or(|(c, _, g)| (&joined, &groups) < (c, g))
            {
                best = Some((joined, connections, groups));
            }
            if !next_turn(&mut turn, &turns) {
                break;
            }
        }
        if !next_ordering(&mut permutation) {
            break;
        }
    }
    let (joined, connections, groups) = best.unwrap_or_default();
    let polymers: Vec<String> = order
        .iter()
        .scan(BTreeMap::<PolymerType, u32>::new(), |numbers, &i| {
            let kind = helm.polymers[i].id.kind;
            let n = numbers.entry(kind).or_insert(0);
            *n += 1;
            let text = &written[i].text;
            let body = &text[kind.name().len()..];
            Some(format!("{}{}{}", kind.name(), n, body))
        })
        .collect();
    Ok((
        format!("{}${}${}$$V2.0", polymers.join("|"), joined, groups),
        connections,
    ))
}

/// The next way of turning each ring, like an odometer; false after the last.
fn next_turn(turn: &mut [usize], counts: &[usize]) -> bool {
    for (t, &n) in turn.iter_mut().zip(counts).rev() {
        if *t + 1 < n {
            *t += 1;
            return true;
        }
        *t = 0;
    }
    false
}
/// The next arrangement of each tie, in turn, like an odometer; false after the last.
fn next_ordering(ties: &mut [Vec<usize>]) -> bool {
    for tie in ties.iter_mut().rev() {
        if next_permutation(tie) {
            return true;
        }
    }
    false
}

fn next_permutation(items: &mut [usize]) -> bool {
    let n = items.len();
    if n < 2 {
        return false;
    }
    let mut i = n - 1;
    while i > 0 && items[i - 1] >= items[i] {
        i -= 1;
    }
    if i == 0 {
        items.sort_unstable();
        return false;
    }
    let mut j = n - 1;
    while items[j] <= items[i - 1] {
        j -= 1;
    }
    items.swap(i - 1, j);
    items[i..].reverse();
    true
}

/// Each polymer's old ID, and its new type, number and index into the HELM's polymers.
type Names = HashMap<String, (PolymerType, u32, usize)>;
/// Each group's old ID's new one, and each new ID with its members, written.
type Groups = (HashMap<String, String>, Vec<(String, String)>);

/// The groups, numbered in the order of what they hold, and each written: their old IDs'
/// new ones, and each new ID with its members.
fn number_groups(text: &str, helm: &Helm, names: &Names) -> Read<Groups> {
    // Groups take their numbers from the order of what they hold.
    let group_text = |g: &Group, groups: &HashMap<String, String>| -> String {
        let mut members: Vec<String> = g
            .members
            .iter()
            .map(|(m, ratio)| {
                let name = names.get(m).map_or_else(
                    || groups.get(m).cloned().unwrap_or_else(|| m.clone()),
                    |(k, n, _)| format!("{}{}", k.name(), n),
                );
                ratio
                    .as_ref()
                    .map_or(name.clone(), |r| format!("{name}:{r}"))
            })
            .collect();
        members.sort();
        members.join(if g.mixture { "+" } else { "," })
    };
    let mut group_names: HashMap<String, String> = HashMap::new();
    // Groups of polymers first, then groups of those, in turn.
    let mut left: Vec<&Group> = helm.groups.iter().collect();
    let mut ordered: Vec<(String, String)> = Vec::new();
    while !left.is_empty() {
        let (ready, waiting): (Vec<&Group>, Vec<&Group>) = left.iter().partition(|g| {
            g.members
                .iter()
                .all(|(m, _)| names.contains_key(m) || group_names.contains_key(m))
        });
        if ready.is_empty() {
            return Err(problem(text, 0, "Its groups hold one another in a circle."));
        }
        let mut batch: Vec<(String, String)> = ready
            .iter()
            .map(|g| (group_text(g, &group_names), format!("G{}", g.number)))
            .collect();
        batch.sort();
        for (body, old) in batch {
            let new = format!("G{}", ordered.len() + 1);
            group_names.insert(old, new.clone());
            ordered.push((new, body));
        }
        left = waiting;
    }
    Ok((group_names, ordered))
}

/// Where an end's position as written is in the canonical form, before its ring is
/// turned: None for one named some other way, or in a group.
fn unturned(text: &str, names: &Names, written: &[Written], e: &End) -> Read<Option<usize>> {
    let (Position::At(p), Some((_, _, index))) = (&e.position, names.get(&e.polymer)) else {
        return Ok(None);
    };
    match written[*index].places.get(p - 1) {
        Some(Place::At(now)) => Ok(Some(*now)),
        Some(Place::Missing) => Err(problem(
            text,
            0,
            format!(
                "A connection names monomer {p} of {}, which is “_”, a missing monomer: \
                 there's nothing there to connect.",
                e.polymer
            ),
        )),
        _ => Err(problem(
            text,
            0,
            format!(
                "A connection names monomer {p} of {}, which is inside a repeat: write the \
                 repeat out to connect one of its monomers.",
                e.polymer
            ),
        )),
    }
}

/// A connection's ends, each its sort key, polymer, position and attachment point,
/// written with the lesser first, after the key it sorts by and a NUL.
fn write_connection(mut ends: [(String, String, String, String); 2]) -> String {
    ends.sort();
    let [a, b] = ends;
    let pos = |p: &String| {
        if p.bytes().all(|c| c.is_ascii_digit()) {
            p.trim_start_matches('0').to_owned()
        } else {
            p.clone()
        }
    };
    let key = [&a.0, &a.2, &a.3, &b.0, &b.2, &b.3]
        .map(String::as_str)
        .join("\u{1}");
    format!(
        "{key}\u{0}{},{},{}:{}-{}:{}",
        a.1,
        b.1,
        pos(&a.2),
        a.3,
        pos(&b.2),
        b.3
    )
}

/// The connections and groups, written, with the polymers numbered in this order (indexes
/// into the HELM's polymers) and each ring turned the `turn[i]`th way.
fn connections_for(
    text: &str,
    helm: &Helm,
    written: &[Written],
    numbered: &[usize],
    turn: &[usize],
) -> Read<(Vec<String>, String)> {
    let mut names: Names = HashMap::new();
    let mut numbers = BTreeMap::<PolymerType, u32>::new();
    for &i in numbered {
        let kind = helm.polymers[i].id.kind;
        let n = numbers.entry(kind).or_insert(0);
        *n += 1;
        names.insert(helm.polymers[i].id.to_string(), (kind, *n, i));
    }
    let (group_names, ordered) = number_groups(text, helm, &names)?;
    // Each end sorts by its polymer's canonical order (its type's, then its number), its
    // position and its attachment point.
    let end = |e: &End, at: Option<usize>| -> (String, String, String, String) {
        let (key, polymer) = if let Some((k, n, _)) = names.get(&e.polymer) {
            (format!("{}{n:09}", *k as u8), format!("{}{}", k.name(), n))
        } else {
            let name = group_names
                .get(&e.polymer)
                .cloned()
                .unwrap_or_else(|| e.polymer.clone());
            (format!("9{name}"), name)
        };
        let position = match (&e.position, at) {
            (_, Some(now)) => format!("{now:09}"),
            (Position::At(p), None) => format!("{p:09}"),
            (Position::Unknown(u), None) => u.clone(),
        };
        (key, polymer, position, e.attachment.clone())
    };
    let mut connections: Vec<String> = Vec::new();
    for c in &helm.connections {
        let (from, to) = (
            unturned(text, &names, written, &c.from)?,
            unturned(text, &names, written, &c.to)?,
        );
        let ring = names
            .get(&c.from.polymer)
            .filter(|_| c.from.polymer == c.to.polymer)
            .and_then(|(_, _, i)| written[*i].ring.as_ref().map(|r| (*i, r)));
        let ends = match ring {
            // A ring's closing bond, from its new first monomer to its new last.
            Some((i, r))
                if [
                    (from, c.from.attachment.as_str()),
                    (to, c.to.attachment.as_str()),
                ]
                .iter()
                .all(|end| *end == (Some(1), "R1") || *end == (Some(r.last), "R2"))
                    && c.from.attachment != c.to.attachment =>
            {
                let closing = |e: &End| {
                    if e.attachment == "R1" {
                        1
                    } else {
                        r.turns[turn[i]].1
                    }
                };
                [
                    end(&c.from, Some(closing(&c.from))),
                    end(&c.to, Some(closing(&c.to))),
                ]
            }
            _ => {
                let turned = |e: &End, at: Option<usize>| {
                    at.map(|now| {
                        names
                            .get(&e.polymer)
                            .and_then(|(_, _, i)| {
                                written[*i].ring.as_ref().map(|r| r.turned(turn[*i], now))
                            })
                            .unwrap_or(now)
                    })
                };
                [
                    end(&c.from, turned(&c.from, from)),
                    end(&c.to, turned(&c.to, to)),
                ]
            }
        };
        connections.push(write_connection(ends));
    }
    connections.sort();
    connections.dedup();
    let connections: Vec<String> = connections
        .into_iter()
        .map(|c| {
            c.split_once('\u{0}')
                .map_or(c.clone(), |(_, text)| text.to_owned())
        })
        .collect();
    let groups: Vec<String> = ordered
        .iter()
        .map(|(name, body)| format!("{name}({body})"))
        .collect();
    Ok((connections, groups.join("|")))
}
// --- What a person (and Iki's actions) are told ----------------------------------------------

/// A monomer as a reading lists it: its ID, whether it's a branch, and where it is.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Listed {
    /// Its position in its polymer, from 1, as connections count.
    pub position: usize,
    /// Its ID, or SMILES, or a list's members as written.
    pub id: String,
    /// It's a branch monomer: a nucleotide's base.
    pub branch: bool,
    /// It's in-line SMILES.
    pub smiles: bool,
    /// It's one of several, or a mixture.
    pub list: bool,
    /// The unit it's in, from 1.
    pub unit: usize,
}

/// How many amino acids, or nucleotides, a polymer has: the fewest and the most.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Length {
    /// The fewest.
    pub fewest: usize,
    /// The most; none when there's no most, for `*`, any number of monomers.
    pub most: Option<usize>,
}

/// A polymer as a reading lists it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Summary {
    /// Its canonical ID.
    pub id: String,
    /// Its type.
    #[serde(rename = "type")]
    pub kind: PolymerType,
    /// Its units as its canonical form writes them, an RNA's with or without a base (a 5′
    /// phosphate is one).
    pub units: usize,
    /// How many amino acids (PEPTIDE) or nucleotides (RNA: its units with a base) it has:
    /// more than one count when a repeat's is a range, or a monomer is `*` (any number)
    /// or a list with `_` (maybe missing). A CHEM is one.
    pub length: Length,
    /// Its monomers, in order, its repeats written out.
    pub monomers: Vec<Listed>,
    /// It has a repeat whose count is a range, a list of monomers, or monomers that aren't
    /// known: `*`, `_`, a PEPTIDE's `X` or an RNA's `N`.
    pub ambiguous: bool,
}

/// A polymer group, as a reading lists it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct GroupSummary {
    /// Its canonical ID, such as G1.
    pub id: String,
    /// Its members are a mixture (`+`), all there; otherwise it's one of them (`,`).
    pub mixture: bool,
    /// Its members: polymers, or groups, by their canonical IDs.
    pub members: Vec<GroupMember>,
}

/// A polymer group's member.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct GroupMember {
    /// Its canonical ID.
    pub id: String,
    /// Its ratio, as written without trailing zeros, if it's given.
    pub ratio: Option<String>,
}

/// What a reading says: the canonical form, its polymers, and its connections.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Reading {
    /// Its canonical form.
    pub canonical: String,
    /// Its polymers, in canonical order.
    pub polymers: Vec<Summary>,
    /// Its connections, canonical.
    pub connections: Vec<String>,
    /// Its polymer groups, canonical.
    pub groups: Vec<GroupSummary>,
    /// It was written as HELM 1.
    pub version1: bool,
}

type Bounds = (usize, Option<usize>);
const NONE: Bounds = (0, Some(0));
const ONE: Bounds = (1, Some(1));

fn plus(a: Bounds, b: Bounds) -> Bounds {
    (
        a.0.saturating_add(b.0),
        a.1.zip(b.1).map(|(x, y)| x.saturating_add(y)),
    )
}

/// One of them: the fewest of any, and the most.
fn either(each: impl Iterator<Item = Bounds>) -> Bounds {
    each.reduce(|a, b| (a.0.min(b.0), a.1.zip(b.1).map(|(x, y)| x.max(y))))
        .unwrap_or(NONE)
}

fn repeated(one: Bounds, repeat: Option<Repeat>) -> Bounds {
    match repeat {
        None => one,
        Some(r) => (
            one.0.saturating_mul(r.from as usize),
            one.1.map(|m| m.saturating_mul(r.to as usize)),
        ),
    }
}

fn unknown(element: &Element, id: &str) -> bool {
    matches!(element, Element::Monomer(m) if !m.smiles && m.id == id)
}

fn units_bounds(kind: PolymerType, units: &[Unit]) -> Bounds {
    units
        .iter()
        .map(|u| repeated(elements_bounds(kind, &u.elements), u.repeat))
        .fold(NONE, plus)
}

/// How many amino acids, or nucleotides, these elements of a unit are.
fn elements_bounds(kind: PolymerType, elements: &[Element]) -> Bounds {
    match kind {
        PolymerType::Chem => ONE,
        PolymerType::Blob => (0, None),
        PolymerType::Peptide => elements.iter().map(residue_bounds).fold(NONE, plus),
        PolymerType::Rna => match elements {
            [Element::Group(units)] => units_bounds(kind, units),
            [Element::List { members, .. }] => {
                either(members.iter().map(|(m, _)| elements_bounds(kind, m)))
            }
            [only] if unknown(only, "*") => (0, None),
            _ => match elements.iter().find_map(|e| match e {
                Element::Branch(base) => Some(base.as_ref()),
                _ => None,
            }) {
                None => NONE,
                Some(base) if unknown(base, "_") => NONE,
                Some(Element::List { members, .. }) => either(members.iter().map(|(m, _)| {
                    if matches!(m.as_slice(), [only] if unknown(only, "_")) {
                        NONE
                    } else {
                        ONE
                    }
                })),
                Some(_) => ONE,
            },
        },
    }
}

fn residue_bounds(element: &Element) -> Bounds {
    match element {
        e if unknown(e, "*") => (0, None),
        e if unknown(e, "_") => NONE,
        Element::Monomer(_) => ONE,
        Element::Branch(inner) => residue_bounds(inner),
        Element::List { members, .. } => either(
            members
                .iter()
                .map(|(m, _)| m.iter().map(residue_bounds).fold(NONE, plus)),
        ),
        Element::Group(units) => units_bounds(PolymerType::Peptide, units),
    }
}

fn summarise(polymer: &Polymer) -> Summary {
    let mut monomers = Vec::new();
    let mut ambiguous = false;
    let mut position = 0;
    let kind = polymer.id.kind;
    for (u, unit) in polymer.units.iter().enumerate() {
        if unit.repeat.is_some() {
            ambiguous = true;
        }
        list_elements(
            kind,
            &unit.elements,
            u + 1,
            false,
            &mut position,
            &mut monomers,
            &mut ambiguous,
        );
    }
    let (fewest, most) = if polymer.blob.is_some() {
        (0, None)
    } else {
        units_bounds(kind, &polymer.units)
    };
    Summary {
        id: polymer.id.to_string(),
        kind,
        units: polymer.units.len(),
        length: Length { fewest, most },
        monomers,
        ambiguous: ambiguous || polymer.blob.is_some(),
    }
}

/// HELM's ambiguous monomers (its "Peptide and Nucleotide Ambiguous Monomers"): `*`, any
/// number; `_`, a missing one; `X`, an unknown amino acid; `N`, an unknown base. A
/// PEPTIDE's `N` is asparagine.
fn is_unknown(kind: PolymerType, monomer: &Monomer) -> bool {
    !monomer.smiles
        && match monomer.id.as_str() {
            "*" | "_" => true,
            "X" => kind == PolymerType::Peptide,
            "N" => kind == PolymerType::Rna,
            _ => false,
        }
}

fn list_elements(
    kind: PolymerType,
    elements: &[Element],
    unit: usize,
    branch: bool,
    position: &mut usize,
    out: &mut Vec<Listed>,
    ambiguous: &mut bool,
) {
    for element in elements {
        match element {
            Element::Monomer(m) => {
                *position += 1;
                if is_unknown(kind, m) {
                    *ambiguous = true;
                }
                out.push(Listed {
                    position: *position,
                    id: m.id.clone(),
                    branch,
                    smiles: m.smiles,
                    list: false,
                    unit,
                });
            }
            Element::Branch(inner) => {
                list_elements(
                    kind,
                    std::slice::from_ref(inner.as_ref()),
                    unit,
                    true,
                    position,
                    out,
                    ambiguous,
                );
            }
            Element::List { .. } => {
                *position += 1;
                *ambiguous = true;
                let mut text = String::new();
                write_element(element, &mut text);
                out.push(Listed {
                    position: *position,
                    id: text,
                    branch,
                    smiles: false,
                    list: true,
                    unit,
                });
            }
            Element::Group(units) => {
                // One position, as connections count it, for every monomer in it.
                let at = *position + 1;
                let from = out.len();
                for u in units {
                    list_elements(kind, &u.elements, unit, branch, position, out, ambiguous);
                }
                for listed in &mut out[from..] {
                    listed.position = at;
                }
                *position = at;
            }
        }
    }
}

/// Reads HELM, and gives its canonical form and what it holds.
///
/// # Errors
///
/// What isn't HELM, or can't be canonical, and where.
pub fn reading(text: &str) -> Read<Reading> {
    let helm = read(text)?;
    let (canonical_text, connections) = canonical_parts(text, &helm)?;
    // The canonical form, read again, lists the polymers and groups in their canonical
    // order. Its connections are the ones just written: in-line SMILES can hold `$`.
    let again = read(&canonical_text)?;
    let groups = again
        .groups
        .iter()
        .map(|g| GroupSummary {
            id: format!("G{}", g.number),
            mixture: g.mixture,
            members: g
                .members
                .iter()
                .map(|(id, ratio)| GroupMember {
                    id: id.clone(),
                    ratio: ratio.clone(),
                })
                .collect(),
        })
        .collect();
    Ok(Reading {
        canonical: canonical_text,
        polymers: again.polymers.iter().map(summarise).collect(),
        connections,
        groups,
        version1: helm.version1,
    })
}
#[derive(serde::Deserialize)]
struct Request {
    helm: Vec<String>,
}

/// Reads each HELM string in a JSON request (`{"helm": [...]}`): for each, its reading, or
/// `{"error": …, "at": …}`.
#[must_use]
pub fn read_json(request: &[u8]) -> Vec<u8> {
    let answer = match serde_json::from_slice::<Request>(request) {
        Ok(request) => serde_json::json!({
            "readings": request
                .helm
                .iter()
                .map(|h| match reading(h) {
                    Ok(r) => serde_json::to_value(r).unwrap_or_default(),
                    Err(p) => serde_json::json!({"error": p.message, "at": p.at}),
                })
                .collect::<Vec<_>>()
        }),
        Err(e) => serde_json::json!({"error": format!("The request isn't one: {e}")}),
    };
    serde_json::to_vec(&answer).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn canon(text: &str) -> String {
        let read = reading(text);
        assert!(read.is_ok(), "{text}: {read:?}");
        read.unwrap().canonical
    }

    fn refused(text: &str) -> String {
        let read = reading(text);
        assert!(read.is_err(), "{text} was read: {read:?}");
        read.unwrap_err().message
    }

    // The HELM 2.04 specification's own examples (Appendix 3, and sections 5.2 to 5.6).
    #[test]
    fn reads_the_specifications_examples() {
        for text in [
            "RNA1{R(A)P.[mR](U)[sP].R(G)P.R([5meC])P.[dR](T)P.[dR](T)}$$$$",
            "PEPTIDE1{A.R.C.A.A.K.T.C.D.A}$PEPTIDE1,PEPTIDE1,8:R3-3:R3$$$",
            "RNA1{R(A)P.R(C)P.R(G)}|RNA2{R(U)P.R(G)P.R(C)}$RNA1,RNA2,2:pair-8:pair$$$",
            "PEPTIDE1{A.G.(C,T).K}$$$$V2.0",
            "PEPTIDE1{A.G.(C:0.5+T:0.5).K}$$$$V2.0",
            "RNA1{R(A)P.R(N)P.R(G)}$$$$V2.0",
            "PEPTIDE1{A.(G.K)'3'.C}$$$$V2.0",
            "PEPTIDE1{A.G'2-5'.C}$$$$V2.0",
            "CHEM1{[PEG2]}|PEPTIDE1{A.G.C}$CHEM1,PEPTIDE1,1:R1-1:R1$$$V2.0",
            "PEPTIDE1{A.G.C}\"HC\"|PEPTIDE2{K.L}\"LC\"$$$$V2.0",
            "PEPTIDE1{A.G}|PEPTIDE2{K.L}$$G1(PEPTIDE1:1+PEPTIDE2:2)$$V2.0",
            "PEPTIDE1{A.C.K}|CHEM1{[SMCC]}$PEPTIDE1,CHEM1,?:R3-1:R1$$$V2.0",
            "PEPTIDE1{A.C.K}|CHEM1{[SMCC]}$PEPTIDE1,CHEM1,(C,K):R3-1:R1$$$V2.0",
            "BLOB1{Bead}|CHEM1{[SMCC]}$BLOB1,CHEM1,?:?-1:R1$$$V2.0",
            "PEPTIDE1{A.[*N[C@@H](C)C(=O)* |$_R1;;;;;;_R2$|].G}$$$$V2.0",
            "PEPTIDE1{A.G}$$${\"Chain\":\"Heavy\"}$V2.0",
        ] {
            canon(text);
        }
    }

    #[test]
    fn one_molecule_written_two_ways_is_one_canonical_form() {
        let same = [
            // Brackets only where they're needed.
            ("RNA1{[R](A)[P].R(C)}$$$$", "RNA1{R(A)P.R(C)}$$$$V2.0"),
            // A repeat of a fixed count, written out.
            (
                "RNA1{[dR](T)[sP]'3'.[dR](T)}$$$$V2.0",
                "RNA1{[dR](T)[sP].[dR](T)[sP].[dR](T)[sP].[dR](T)}$$$$V2.0",
            ),
            (
                "PEPTIDE1{A.(G.K)'2'.C}$$$$V2.0",
                "PEPTIDE1{A.G.K.G.K.C}$$$$V2.0",
            ),
            // A list's members in order, ratios without trailing zeros.
            (
                "PEPTIDE1{(T:0.50+C:0.5)}$$$$V2.0",
                "PEPTIDE1{(C:0.5+T:0.5)}$$$$V2.0",
            ),
            // Annotations aren't the molecule.
            (
                "RNA1{R(A)P\"x\".R(C)}\"sense\"$$$$V2.0",
                "RNA1{R(A)P.R(C)}$$$$V2.0",
            ),
            // A siRNA's strands in either order, and the pair from either end.
            (
                "RNA1{R(U)P.R(G)}|RNA2{R(C)P.R(A)}$RNA1,RNA2,2:pair-5:pair|RNA1,RNA2,5:pair-2:pair$$$V2.0",
                "RNA1{R(C)P.R(A)}|RNA2{R(U)P.R(G)}$RNA2,RNA1,2:pair-5:pair|RNA1,RNA2,2:pair-5:pair$$$V2.0",
            ),
            // A conjugate's polymers and connection in either order.
            (
                "CHEM1{[5FAM]}|RNA1{R(A)P.R(C)}$CHEM1,RNA1,1:R2-1:R1$$$V2.0",
                "RNA1{R(A)P.R(C)}|CHEM1{[5FAM]}$RNA1,CHEM1,1:R1-1:R2$$$V2.0",
            ),
        ];
        for (a, b) in same {
            assert_eq!(canon(a), canon(b), "{a} and {b}");
        }
        assert_eq!(
            canon("RNA1{[R](A)[P].R(C)}$$$$"),
            "RNA1{R(A)P.R(C)}$$$$V2.0"
        );
        assert_eq!(
            canon("RNA1{R(C)P.R(A)}|CHEM1{[5FAM]}$RNA1,CHEM1,1:R1-1:R2$$$V2.0"),
            "RNA1{R(C)P.R(A)}|CHEM1{[5FAM]}$RNA1,CHEM1,1:R1-1:R2$$$V2.0",
        );
    }

    #[test]
    fn a_modified_molecule_is_another_canonical_form() {
        let plain = canon("RNA1{[dR](A)P.[dR](C)P.[dR](G)}$$$$V2.0");
        for other in [
            "RNA1{[dR](A)[sP].[dR](C)P.[dR](G)}$$$$V2.0",
            "RNA1{[dR](A)P.[dR]([5meC])P.[dR](G)}$$$$V2.0",
            "RNA1{[dR](A)P.[dR](C)P.[dR](G)}|CHEM1{[5Bio]}$RNA1,CHEM1,1:R1-1:R2$$$V2.0",
            "RNA1{[dR](G)P.[dR](C)P.[dR](A)}$$$$V2.0",
        ] {
            assert_ne!(canon(other), plain, "{other}");
        }
    }

    #[test]
    fn identical_strands_are_numbered_so_their_connections_are_least() {
        // A self-complementary duplex: which strand is RNA1 doesn't matter.
        let a = canon(
            "RNA1{R(G)P.R(C)}|RNA2{R(G)P.R(C)}$RNA1,RNA2,1:pair-3:pair|RNA1,RNA2,3:pair-1:pair$$$V2.0",
        );
        let b = canon(
            "RNA1{R(G)P.R(C)}|RNA2{R(G)P.R(C)}$RNA2,RNA1,1:pair-3:pair|RNA2,RNA1,3:pair-1:pair$$$V2.0",
        );
        assert_eq!(a, b);
    }

    #[test]
    fn connections_follow_a_repeat_written_out() {
        assert_eq!(
            canon("PEPTIDE1{A'2'.C}$PEPTIDE1,PEPTIDE1,2:R3-2:R3$$$V2.0"),
            "PEPTIDE1{A.A.C}$PEPTIDE1,PEPTIDE1,3:R3-3:R3$$$V2.0"
        );
        assert!(refused("PEPTIDE1{A'2'.C}$PEPTIDE1,PEPTIDE1,1:R3-2:R3$$$V2.0").contains("repeat"));
        // A group of units is one position as written (HELM 2.04, 5.2.3).
        assert_eq!(
            canon("RNA1{R(A)P.(R(C)P)'2'.R(G)}$RNA1,RNA1,5:R3-1:R1$$$V2.0"),
            "RNA1{R(A)P.R(C)P.R(C)P.R(G)}$RNA1,RNA1,1:R1-10:R3$$$V2.0"
        );
        assert!(
            refused("RNA1{R(A)P.(R(C)P)'2'.R(G)}$RNA1,RNA1,4:R3-1:R1$$$V2.0").contains("repeat")
        );
    }

    #[test]
    fn says_what_isnt_helm_and_where() {
        for (text, says) in [
            ("", "empty"),
            ("RNA1{R(A)P}", "four sections"),
            ("RNA1{R(A)P}$$$$$", "fifth"),
            ("RNA1{R(A)P}$$$$V3", "V2.0"),
            ("DNA1{R(A)P}$$$$V2.0", "PEPTIDE, RNA, CHEM or BLOB"),
            ("RNA{R(A)P}$$$$V2.0", "number"),
            ("RNA1{R(A)P.}$$$$V2.0", "monomer"),
            ("RNA1{R(A)P.[mR}$$$$V2.0", "isn't closed"),
            ("RNA1{R(A)P R(C)}$$$$V2.0", "space"),
            ("RNA1{R(A)P}|RNA1{R(C)}$$$$V2.0", "two polymers called RNA1"),
            ("RNA1{R(A)P}$RNA1,RNA2,1:R1-1:R2$$$V2.0", "RNA2"),
            ("RNA1{R(A)P}$RNA1,RNA1,9:R1-1:R2$$$V2.0", "monomer 9"),
            ("RNA1{R(A)P}$RNA1,RNA1,1:X1-1:R2$$$V2.0", "attachment point"),
            ("PEPTIDE1{A(G)}$$$$V2.0", "Only RNA"),
            ("PEPTIDE1{A.G'5-2'}$$$$V2.0", "range"),
            ("PEPTIDE1{(A,G+C)}$$$$V2.0", "not both"),
            ("CHEM1{[A].[B]}$$$$V2.0", "one monomer"),
            ("PEPTIDE1{A}$$$notjson$V2.0", "JSON"),
        ] {
            let message = refused(text);
            assert!(message.contains(says), "{text}: {message}");
        }
        let p = read("RNA1{R(A)P.Q(").unwrap_err();
        assert!(p.message.contains("“{” isn't closed"), "{}", p.message);
        assert_eq!(p.at, 4);
        let p = read("RNA1{R(A)P.!}$$$$V2.0").unwrap_err();
        assert_eq!(p.at, 11);
    }

    #[test]
    fn lists_each_polymers_monomers_in_order() {
        let r = reading("RNA1{[dR](A)[sP].[dR]([5meC])}$$$$V2.0").unwrap();
        let ids: Vec<(&str, bool, usize)> = r.polymers[0]
            .monomers
            .iter()
            .map(|m| (m.id.as_str(), m.branch, m.unit))
            .collect();
        assert_eq!(
            ids,
            [
                ("dR", false, 1),
                ("A", true, 1),
                ("sP", false, 1),
                ("dR", false, 2),
                ("5meC", true, 2)
            ]
        );
        assert_eq!(r.polymers[0].units, 2);
        assert!(!r.polymers[0].ambiguous);
        assert!(reading("RNA1{R(N)P.R(A,G)}$$$$V2.0").unwrap().polymers[0].ambiguous);
    }

    #[test]
    fn its_connections_are_the_ones_it_has_not_text_in_inline_smiles() {
        // HELM 2.04's in-line CXSMILES example: its atom labels ($_R1;;;;;;_R2$) hold `$`,
        // and it has no connections.
        let smiles = "PEPTIDE1{A.[*N[C@@H](C)C(=O)* |$_R1;;;;;;_R2$|].G}";
        assert!(
            reading(&format!("{smiles}$$$$V2.0"))
                .unwrap()
                .connections
                .is_empty()
        );
        let joined = format!("{smiles}|CHEM1{{[5FAM]}}$PEPTIDE1,CHEM1,3:R2-1:R1$$$V2.0");
        assert_eq!(
            reading(&joined).unwrap().connections,
            ["PEPTIDE1,CHEM1,3:R2-1:R1"]
        );
    }

    #[test]
    fn unknown_monomers_are_ambiguous_where_they_mean_unknown() {
        // Pistoia's ambiguous monomers: X, one unknown amino acid; N, one unknown base.
        let ambiguous = |text: &str| reading(text).unwrap().polymers[0].ambiguous;
        assert!(ambiguous("PEPTIDE1{A.X.C}$$$$V2.0"));
        assert!(ambiguous("PEPTIDE1{A.X'3'.C}$$$$V2.0"));
        assert!(ambiguous("RNA1{R(N)P.R(C)}$$$$V2.0"));
        assert!(ambiguous("RNA1{R(N)}$$$$V2.0"));
        assert!(ambiguous("PEPTIDE1{A.(G,K).C}$$$$V2.0"));
        // A peptide's N is asparagine, and a known monomer is known.
        assert!(!ambiguous("PEPTIDE1{A.N.C}$$$$V2.0"));
        assert!(!ambiguous("RNA1{R(A)P.R(C)}$$$$V2.0"));
    }

    #[test]
    fn a_missing_monomer_and_a_repeated_member_add_nothing() {
        // Pistoia: PEPTIDE1{H.E.L._.H.E.L.M} is PEPTIDE1{H.E.L.H.E.L.M}, R(_)P is RP, and
        // (A+A+L+L+A+V) "is exactly the same as" (A+L+V).
        for (one, other) in [
            (
                "PEPTIDE1{H.E.L._.H.E.L.M}$$$$V2.0",
                "PEPTIDE1{H.E.L.H.E.L.M}$$$$V2.0",
            ),
            (
                "RNA1{R(T)P.R(_)P.R(T)P}$$$$V2.0",
                "RNA1{R(T)P.RP.R(T)P}$$$$V2.0",
            ),
            (
                "PEPTIDE1{(A+A+L+L+A+V)}$$$$V2.0",
                "PEPTIDE1{(A+L+V)}$$$$V2.0",
            ),
            ("PEPTIDE1{G.(A,A,L)}$$$$V2.0", "PEPTIDE1{G.(A,L)}$$$$V2.0"),
            ("PEPTIDE1{G.(A+A)}$$$$V2.0", "PEPTIDE1{G.A}$$$$V2.0"),
            ("RNA1{R(A,A)P.R(C)}$$$$V2.0", "RNA1{R(A)P.R(C)}$$$$V2.0"),
            (
                "PEPTIDE1{A.(G._)'2-3'.C}$$$$V2.0",
                "PEPTIDE1{A.(G)'2-3'.C}$$$$V2.0",
            ),
            // Connections follow the monomers that are there.
            (
                "PEPTIDE1{A._.C.C}$PEPTIDE1,PEPTIDE1,3:R3-4:R3$$$V2.0",
                "PEPTIDE1{A.C.C}$PEPTIDE1,PEPTIDE1,2:R3-3:R3$$$V2.0",
            ),
        ] {
            assert_eq!(canon(one), canon(other), "{one} and {other}");
        }
        assert_eq!(
            canon("PEPTIDE1{A._.C.C}$PEPTIDE1,PEPTIDE1,3:R3-4:R3$$$V2.0"),
            "PEPTIDE1{A.C.C}$PEPTIDE1,PEPTIDE1,2:R3-3:R3$$$V2.0"
        );
        // A monomer that may be missing, and stated proportions, mean something.
        for (one, other) in [
            ("PEPTIDE1{M.(K,_)}$$$$V2.0", "PEPTIDE1{M.K}$$$$V2.0"),
            (
                "PEPTIDE1{(A:3+L:3+V:1)}$$$$V2.0",
                "PEPTIDE1{(A+L+V)}$$$$V2.0",
            ),
        ] {
            assert_ne!(canon(one), canon(other), "{one} and {other}");
        }
        assert!(refused("PEPTIDE1{A._.C}$PEPTIDE1,PEPTIDE1,2:R3-3:R3$$$V2.0").contains("missing"));
        // Nothing but a missing monomer is still a polymer.
        assert_eq!(canon("PEPTIDE1{_}$$$$V2.0"), "PEPTIDE1{_}$$$$V2.0");
    }

    #[test]
    fn a_ring_is_one_molecule_from_whichever_unit_its_written() {
        // A cyclic trinucleotide written from each of its three nucleotides, its last
        // phosphate's R2 joined to its first sugar's R1: one molecule (one InChIKey,
        // XNVATHYUPFFQQH-POYLIAOGSA-N, by RDKit).
        let rings = [
            "RNA1{R(A)P.R(C)P.R(G)P}$RNA1,RNA1,1:R1-9:R2$$$V2.0",
            "RNA1{R(C)P.R(G)P.R(A)P}$RNA1,RNA1,9:R2-1:R1$$$V2.0",
            "RNA1{R(G)P.R(A)P.R(C)P}$RNA1,RNA1,1:R1-9:R2$$$V2.0",
        ];
        for ring in rings {
            assert_eq!(
                canon(ring),
                "RNA1{R(A)P.R(C)P.R(G)P}$RNA1,RNA1,1:R1-9:R2$$$V2.0",
                "{ring}"
            );
        }
        // Read the other way, it's another molecule; and a strand that isn't a ring is
        // another from each of its ends.
        assert_ne!(
            canon("RNA1{R(A)P.R(G)P.R(C)P}$RNA1,RNA1,1:R1-9:R2$$$V2.0"),
            canon(rings[0])
        );
        assert_ne!(
            canon("RNA1{R(C)P.R(G)P.R(A)P}$$$$V2.0"),
            canon("RNA1{R(A)P.R(C)P.R(G)P}$$$$V2.0")
        );
        // A head-to-tail cyclic peptide, with a dye on its cysteine: its other bonds turn
        // with it.
        assert_eq!(
            canon(
                "PEPTIDE1{G.C.A}|CHEM1{[5FAM]}$PEPTIDE1,PEPTIDE1,3:R2-1:R1|PEPTIDE1,CHEM1,2:R3-1:R1$$$V2.0"
            ),
            "PEPTIDE1{A.G.C}|CHEM1{[5FAM]}$PEPTIDE1,PEPTIDE1,1:R1-3:R2|PEPTIDE1,CHEM1,3:R3-1:R1$$$V2.0"
        );
        // A ring that repeats reads least from more than one unit: its other bonds decide.
        assert_eq!(
            canon(
                "PEPTIDE1{A.C.A.C}|CHEM1{[5FAM]}$PEPTIDE1,PEPTIDE1,4:R2-1:R1|PEPTIDE1,CHEM1,4:R3-1:R1$$$V2.0"
            ),
            canon(
                "PEPTIDE1{A.C.A.C}|CHEM1{[5FAM]}$PEPTIDE1,PEPTIDE1,4:R2-1:R1|PEPTIDE1,CHEM1,2:R3-1:R1$$$V2.0"
            ),
        );
        // A disulfide isn't a ring of the backbone: it's left as written.
        assert_eq!(
            canon("PEPTIDE1{C.A.C}$PEPTIDE1,PEPTIDE1,1:R3-3:R3$$$V2.0"),
            "PEPTIDE1{C.A.C}$PEPTIDE1,PEPTIDE1,1:R3-3:R3$$$V2.0"
        );
    }

    #[test]
    fn says_how_long_each_polymer_may_be_and_its_groups() {
        let length = |text: &str| {
            let r = reading(text).unwrap();
            let l = r.polymers[0].length;
            (l.fewest, l.most)
        };
        // Pistoia's G'2-5' is 2 to 5 glycines; * any number; (K,_) a lysine or none.
        assert_eq!(length("PEPTIDE1{A.G'2-5'.C}$$$$V2.0"), (4, Some(7)));
        assert_eq!(length("PEPTIDE1{A.G'2'.C}$$$$V2.0"), (4, Some(4)));
        assert_eq!(length("PEPTIDE1{A.*.G}$$$$V2.0"), (2, None));
        assert_eq!(length("PEPTIDE1{A._.G}$$$$V2.0"), (2, Some(2)));
        assert_eq!(length("PEPTIDE1{H.E.L.M.(K,_)}$$$$V2.0"), (4, Some(5)));
        assert_eq!(length("PEPTIDE1{A.(G.K)'1-3'}$$$$V2.0"), (3, Some(7)));
        // A strand's nucleotides are its units with a base: a 5′ phosphate isn't one.
        let r = reading("RNA1{P.[dR](A)P.[dR](C)}$$$$V2.0").unwrap();
        assert_eq!((r.polymers[0].units, r.polymers[0].length.fewest), (3, 2));
        assert_eq!(length("RNA1{R(T)P.R(_,A)P.R(T)}$$$$V2.0"), (2, Some(3)));
        assert_eq!(length("RNA1{(R(A)P)'2-4'.R(C)}$$$$V2.0"), (3, Some(5)));
        // Polymer groups: one of them (,) or a mixture (+), with ratios.
        let groups = |text: &str| reading(text).unwrap().groups;
        let either = groups("PEPTIDE1{A.G}|PEPTIDE2{K.L.M}$$G1(PEPTIDE1,PEPTIDE2)$$V2.0");
        assert_eq!(
            either,
            [GroupSummary {
                id: "G1".into(),
                mixture: false,
                members: vec![
                    GroupMember {
                        id: "PEPTIDE1".into(),
                        ratio: None
                    },
                    GroupMember {
                        id: "PEPTIDE2".into(),
                        ratio: None
                    }
                ],
            }]
        );
        let both = groups("PEPTIDE1{A.G}|PEPTIDE2{K.L.M}$$G1(PEPTIDE1:2+PEPTIDE2:3)$$V2.0");
        assert!(both[0].mixture);
        assert_eq!(both[0].members[1].ratio.as_deref(), Some("3"));
    }

    #[test]
    fn the_json_request_reads_each() {
        let out = read_json(br#"{"helm": ["RNA1{[R](A)}$$$$", "nope"]}"#);
        let v: serde_json::Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(v["readings"][0]["canonical"], "RNA1{R(A)}$$$$V2.0");
        assert!(v["readings"][1]["error"].as_str().unwrap().contains("four"));
    }
}
