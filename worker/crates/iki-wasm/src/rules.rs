//! Rules and guards (step 58): conditions a record must meet when it's saved, and that a
//! state change must meet before it's made, with messages saying why one failed. Each is
//! CEL over variables a plugin declares the shape of (`record`, `actor`, `now`), as
//! `record.purity >= 95.0 || record.status == 'rejected'`.
//!
//! cel-rust has no type checker, so this module has one: a rule that could only ever
//! fail (a typo in a field's name, a status that isn't one, text compared with a
//! number) is refused when its plugin is loaded, saying where and what was likely
//! meant, rather than failing for someone saving a record. It then evaluates rules with
//! each variable's JSON read by its schema, so `95` is a double where one is declared,
//! and says when a failure came from an empty value, which a save rule takes as not
//! applying, as SQL's CHECK does with NULL.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use serde::{Deserialize, Serialize};

use crate::expressions::{self, MAX_DEPTH, MAX_LENGTH};
use crate::templates::{self, Part};

mod check;
pub(crate) mod flow;
#[cfg(test)]
mod tests;
mod types;
mod value;

use check::{At, Checker, Fault, Reads};
use types::Ty;
use value::Probes;

/// What a variable's values are, as a plugin declares them. Any of them may be null.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(tag = "type", rename_all = "lowercase", deny_unknown_fields)]
pub enum Schema {
    /// Yes or no.
    Bool,
    /// A signed whole number.
    Int,
    /// An unsigned whole number.
    Uint,
    /// A floating-point number.
    Double,
    /// Text.
    String {
        /// What a person calls it, as `Status`, for messages about its values.
        #[serde(default)]
        label: Option<String>,
        /// The only text it can be: an enum's values, or a state field's states.
        #[serde(default)]
        values: Option<Vec<String>>,
    },
    /// A point in time, written in RFC 3339.
    Timestamp,
    /// A length of time, written as CEL writes one: `1h30m`.
    Duration,
    /// Anything, read as JSON is.
    Dyn,
    /// A list of values of one kind.
    List {
        /// What its items are.
        items: Box<Schema>,
    },
    /// A map from text to values of one kind.
    Map {
        /// What its values are.
        values: Box<Schema>,
    },
    /// A value with known fields, as a record is.
    Object {
        /// What a person calls it, as `Sample`.
        label: String,
        /// Its fields.
        fields: BTreeMap<String, Schema>,
    },
}

/// What an expression is for.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    /// A CEL expression that's yes or no.
    Condition,
    /// Text with CEL expressions in it, between `{{` and `}}`.
    Message,
}

/// One rule's expression.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Expression {
    /// As written.
    pub source: String,
    /// What it is.
    pub kind: Kind,
}

/// What [`check`] reads: expressions, and the variables they may read.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CheckRequest {
    /// The variables, by name, and what their values are.
    #[serde(default)]
    pub variables: BTreeMap<String, Schema>,
    /// The expressions.
    pub expressions: Vec<Expression>,
}

/// What [`check`] makes of one expression.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum RuleChecked {
    /// It checks.
    Valid {
        /// Each variable it reads, with the fields it selects of it when it's an object,
        /// sorted; none when it reads the variable whole or the variable isn't one.
        reads: BTreeMap<String, Vec<String>>,
    },
    /// It doesn't.
    Invalid {
        /// Why, for a person.
        error: String,
        /// What to write instead, if there's something to say.
        hint: Option<String>,
        /// The line it's on, from 1, if it's somewhere in particular.
        line: Option<usize>,
        /// The column, in characters from 1, if it's somewhere in particular.
        column: Option<usize>,
    },
}

/// What [`evaluate`] reads: expressions, the variables they may read, and contexts,
/// each the variables' values once.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EvaluateRequest {
    /// The variables, by name, and what their values are.
    #[serde(default)]
    pub variables: BTreeMap<String, Schema>,
    /// The expressions.
    pub expressions: Vec<Expression>,
    /// The variables' values, as JSON, once per context. A variable a context leaves
    /// out is null.
    #[serde(default)]
    pub contexts: Vec<serde_json::Map<String, serde_json::Value>>,
}

/// One expression's outcome in one context.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum RuleEvaluated {
    /// A condition's value.
    Value {
        /// Whether it holds.
        value: bool,
    },
    /// A message, with its expressions' values written in.
    Text {
        /// The message.
        text: String,
    },
    /// Why it has no outcome.
    Error {
        /// For a person, without what any value is.
        error: String,
        /// Whether a variable or field it reads is empty here, which is likely why.
        empty: bool,
    },
}

/// Why an expression doesn't check, placed in its text.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Rejection {
    error: String,
    hint: Option<String>,
    line: Option<usize>,
    column: Option<usize>,
}

impl From<Rejection> for RuleChecked {
    fn from(r: Rejection) -> Self {
        Self::Invalid {
            error: r.error,
            hint: r.hint,
            line: r.line,
            column: r.column,
        }
    }
}

/// The byte line `line`, column `column` (both from 1, the column in characters) is at
/// in `source`.
fn byte_of(source: &str, line: usize, column: usize) -> usize {
    let mut offset = 0;
    for (i, text) in source.split_inclusive('\n').enumerate() {
        if i + 1 == line {
            let within = text
                .char_indices()
                .nth(column.saturating_sub(1))
                .map_or(text.len(), |(b, _)| b);
            return offset + within;
        }
        offset += text.len();
    }
    source.len()
}

/// The line and column (in characters), both from 1, of byte `byte` of `text`.
fn line_column(text: &str, byte: usize) -> (usize, usize) {
    let mut byte = byte.min(text.len());
    while !text.is_char_boundary(byte) {
        byte -= 1;
    }
    let before = &text[..byte];
    let line = before.matches('\n').count() + 1;
    let column = before.rsplit('\n').next().map_or(0, |l| l.chars().count()) + 1;
    (line, column)
}

/// Where the expression is in the text it's part of.
struct Placement<'a> {
    /// The whole text: a condition, or a message.
    text: &'a str,
    /// The expression.
    expression: &'a str,
    /// The byte of `text` it starts at.
    start: usize,
    /// Whether it's one of a message's, which a rejection quotes.
    quoted: bool,
}

impl Placement<'_> {
    /// The fault, with its line and column in the whole text.
    fn reject(&self, fault: Fault) -> Rejection {
        let byte = fault.at.map(|at| {
            self.start
                + match at {
                    At::Byte(b) => b,
                    At::LineColumn(l, c) => byte_of(self.expression, l, c),
                }
        });
        let (line, column) = byte.map(|b| line_column(self.text, b)).unzip();
        let mut error = fault.reason;
        if fault.parsed {
            match (line, column) {
                (Some(l), Some(c)) => error = format!("{error}, at line {l}, column {c}."),
                _ => error.push('.'),
            }
        }
        if self.quoted {
            error = format!("{}: {error}", templates::quoted(self.expression));
        }
        Rejection {
            error,
            hint: fault.hint,
            line,
            column,
        }
    }
}

/// Why the parser didn't read an expression, as [`expressions::check`] says it.
fn unreadable(errors: &cel::ParseErrors) -> Fault {
    let Some(e) = errors.errors.first() else {
        return Fault::new("It isn't an expression.");
    };
    if e.msg.contains("Recursion limit") {
        return Fault::new(format!("It nests more than {MAX_DEPTH} deep."));
    }
    let (line, column) = e.pos;
    let at = match (usize::try_from(line), usize::try_from(column)) {
        (Ok(l), Ok(c)) if l >= 1 && c >= 1 => Some(At::LineColumn(l, c)),
        _ => None,
    };
    if e.msg.contains("'.?'") || e.msg.contains("'[?'") {
        return Fault {
            at,
            ..check::optional()
        };
    }
    let reason = if e.msg.contains("must be a simple name") {
        "A macro's variable must be a simple name, as `x` in `items.all(x, x > 0)`".to_owned()
    } else if e.msg.contains("invalid argument to has() macro") {
        "`has()` takes a field, as in `has(m.key)`".to_owned()
    } else {
        expressions::plainly(&e.msg)
    };
    Fault {
        reason,
        hint: None,
        at,
        parsed: true,
    }
}

/// Parses an expression, within the limits apps' expressions have.
fn parse(source: &str) -> Result<cel::Program, Fault> {
    if source.chars().count() > MAX_LENGTH {
        return Err(Fault::new(format!(
            "It's over {MAX_LENGTH} characters, the most an expression may be."
        )));
    }
    if source.trim().is_empty() {
        return Err(Fault::new("It's empty."));
    }
    cel::parser::Parser::new()
        .max_recursion_depth(MAX_DEPTH)
        .parse(source)
        .map_err(|errors| unreadable(&errors))?;
    cel::Program::compile(source).map_err(|errors| unreadable(&errors))
}

/// What kind of value a cel type is, by its name, for a person.
fn kind_named(name: &str) -> &'static str {
    match name {
        "null_type" => "an empty value",
        "int" | "uint" => "a whole number",
        "double" => "a number",
        "string" => "text",
        "bool" => "yes or no",
        "bytes" => "bytes",
        "list" => "a list",
        "map" => "a map",
        "iterable" => "a list or a map",
        n if n.ends_with("Timestamp") || n == "timestamp" => "a time",
        n if n.ends_with("Duration") || n == "duration" => "a duration",
        _ => "a value",
    }
}

/// What kind of value it is, for a person, without what's in it.
fn kind_of(value: &cel::Value) -> &'static str {
    match value {
        cel::Value::Null => "an empty value",
        cel::Value::Timestamp(_) => "a time",
        cel::Value::Duration(_) => "a duration",
        other => expressions::kind(other),
    }
}

/// Why a rule failed, for a person, as [`expressions`] says it but with operators and
/// kinds of value named as a person knows them: "`>=` doesn't work on an empty value and
/// a number", not "`_>=_` doesn't take `null_type` and double".
fn failure(error: &cel::ExecutionError) -> String {
    use cel::ExecutionError as E;
    match error {
        E::NoSuchOverload(overload) => {
            let kinds: Vec<&str> = overload
                .argument_types()
                .iter()
                .map(|t| kind_named(t))
                .collect();
            let first = kinds.first().copied().unwrap_or("a value");
            match overload.function() {
                "_._" => format!("A field can't be read of {first}"),
                "_[_]" => format!("{} can't be indexed", types::capitalized(first)),
                f if f.chars().all(|c| c.is_ascii_alphanumeric()) => {
                    format!("`{f}()` doesn't work on {}", kinds.join(" and "))
                }
                f => {
                    let op = f.trim_matches(|c| c == '_' || c == '@');
                    format!("`{op}` doesn't work on {}", kinds.join(" and "))
                }
            }
        }
        E::UnexpectedType { got, want } => {
            format!(
                "It's {} where {} is wanted",
                kind_named(got),
                kind_named(want)
            )
        }
        E::UnsupportedBinaryOperator(op, a, b) => {
            let op = match *op {
                "add" => "+",
                "sub" => "-",
                "mul" => "*",
                "div" => "/",
                "rem" => "%",
                other => other,
            };
            format!("`{op}` doesn't work on {} and {}", kind_of(a), kind_of(b))
        }
        E::ValuesNotComparable(a, b) => {
            let a = types::capitalized(kind_of(a));
            format!("{a} can't be compared with {}", kind_of(b))
        }
        other => expressions::failure(other),
    }
}

/// A piece of a rule: text, or an expression ready to run.
enum Piece {
    /// A message's text, as it is.
    Text(String),
    /// An expression.
    Expression {
        /// As written.
        source: String,
        /// Parsed.
        program: cel::Program,
        /// What it reads that can be empty: see [`Checker::paths`].
        paths: BTreeSet<Vec<String>>,
    },
}

/// One rule's expression, parsed and checked.
struct Analysed {
    kind: Kind,
    pieces: Vec<Piece>,
    reads: BTreeMap<String, Reads>,
}

impl Analysed {
    /// What it reads of each variable, as [`RuleChecked::Valid`] says.
    fn reads(&self, variables: &BTreeMap<String, Ty>) -> BTreeMap<String, Vec<String>> {
        self.reads
            .iter()
            .map(|(name, reads)| {
                let object = matches!(variables.get(name), Some(Ty::Object(_)));
                let fields = if reads.whole || !object {
                    Vec::new()
                } else {
                    reads.fields.iter().cloned().collect()
                };
                (name.clone(), fields)
            })
            .collect()
    }

    /// Notes what an expression reads, among the rest.
    fn add_reads(&mut self, reads: BTreeMap<String, Reads>) {
        for (name, more) in reads {
            let all = self.reads.entry(name).or_default();
            all.whole |= more.whole;
            all.fields.extend(more.fields);
        }
    }

    /// Its outcome with these variables' values.
    fn run(
        &self,
        scope: &cel::Context<'_, '_>,
        values: &BTreeMap<String, cel::Value>,
    ) -> RuleEvaluated {
        let failed = |error: String, paths: &BTreeSet<Vec<String>>| RuleEvaluated::Error {
            error,
            empty: value::empty(paths, values),
        };
        let mut text = String::new();
        for piece in &self.pieces {
            let (source, program, paths) = match piece {
                Piece::Text(t) => {
                    text.push_str(t);
                    continue;
                }
                Piece::Expression {
                    source,
                    program,
                    paths,
                } => (source, program, paths),
            };
            let result = program.execute(scope);
            match (self.kind, result) {
                (Kind::Condition, Ok(cel::Value::Bool(value))) => {
                    return RuleEvaluated::Value { value };
                }
                (Kind::Condition, Ok(other)) => {
                    let kind = kind_of(&other);
                    return failed(format!("It's {kind} where yes or no is wanted."), paths);
                }
                (Kind::Condition, Err(error)) => {
                    return failed(format!("{}.", failure(&error)), paths);
                }
                (Kind::Message, Ok(v)) => text.push_str(&value::text(&v)),
                (Kind::Message, Err(error)) => {
                    let error = format!("{}: {}.", templates::quoted(source), failure(&error));
                    return failed(error, paths);
                }
            }
        }
        match self.kind {
            Kind::Message => RuleEvaluated::Text { text },
            Kind::Condition => RuleEvaluated::Error {
                error: "It isn't an expression.".to_owned(),
                empty: false,
            },
        }
    }
}

/// Parses and checks one expression, placed in its rule's text: the whole of a
/// condition, which must be yes or no, or one of a message's, which may be anything.
fn piece(
    placement: &Placement<'_>,
    condition: bool,
    variables: &BTreeMap<String, Ty>,
    probes: &Probes,
) -> Result<(Piece, BTreeMap<String, Reads>), Rejection> {
    let expression = placement.expression;
    let program = parse(expression).map_err(|f| placement.reject(f))?;
    let mut checker = Checker::new(variables, program.source_info(), expression, probes);
    let outcome = if condition {
        checker.condition(program.expression())
    } else {
        checker.check(program.expression()).map(|_| ())
    };
    outcome.map_err(|f| placement.reject(f))?;
    let paths = std::mem::take(&mut checker.paths);
    let reads = std::mem::take(&mut checker.reads);
    let piece = Piece::Expression {
        source: expression.to_owned(),
        program,
        paths,
    };
    Ok((piece, reads))
}

/// Parses and checks one rule's expression against the declared variables.
fn analyse(
    expression: &Expression,
    variables: &BTreeMap<String, Ty>,
    probes: &Probes,
) -> Result<Analysed, Rejection> {
    let source = expression.source.as_str();
    let mut analysed = Analysed {
        kind: expression.kind,
        pieces: Vec::new(),
        reads: BTreeMap::new(),
    };
    if expression.kind == Kind::Condition {
        let placement = Placement {
            text: source,
            expression: source,
            start: 0,
            quoted: false,
        };
        let (piece, reads) = piece(&placement, true, variables, probes)?;
        analysed.pieces.push(piece);
        analysed.add_reads(reads);
        return Ok(analysed);
    }
    let parts = templates::parts(source).map_err(|error| Rejection {
        error,
        hint: None,
        line: None,
        column: None,
    })?;
    let mut offset = 0;
    for part in parts {
        match part {
            Part::Text(t) => {
                offset += t.len();
                analysed.pieces.push(Piece::Text(t.to_owned()));
            }
            Part::Expression(e) => {
                // Past its `{{`, and then its `}}`.
                let start = offset + 2;
                let placement = Placement {
                    text: source,
                    expression: e,
                    start,
                    quoted: true,
                };
                let (piece, reads) = piece(&placement, false, variables, probes)?;
                analysed.pieces.push(piece);
                analysed.add_reads(reads);
                offset = start + e.len() + 2;
            }
        }
    }
    Ok(analysed)
}

/// The declared variables, as the checker reads them.
fn declared(variables: &BTreeMap<String, Schema>) -> BTreeMap<String, Ty> {
    variables
        .iter()
        .map(|(name, schema)| (name.clone(), Ty::of(schema, name)))
        .collect()
}

/// Whether each expression checks against the variables, and what it reads.
#[must_use]
pub fn check(request: &CheckRequest) -> Vec<RuleChecked> {
    let probes = Probes::new(Arc::new(cel::Env::stdlib()));
    let variables = declared(&request.variables);
    request
        .expressions
        .iter()
        .map(
            |expression| match analyse(expression, &variables, &probes) {
                Ok(analysed) => RuleChecked::Valid {
                    reads: analysed.reads(&variables),
                },
                Err(rejection) => rejection.into(),
            },
        )
        .collect()
}

/// Each expression's outcome in each context: a list per context, of one per
/// expression. An expression that doesn't check fails in every context, saying why.
#[must_use]
pub fn evaluate(request: &EvaluateRequest) -> Vec<Vec<RuleEvaluated>> {
    let probes = Probes::new(Arc::new(cel::Env::stdlib()));
    let variables = declared(&request.variables);
    let analysed: Vec<Result<Analysed, Rejection>> = request
        .expressions
        .iter()
        .map(|expression| analyse(expression, &variables, &probes))
        .collect();
    request
        .contexts
        .iter()
        .map(|context| {
            let values: BTreeMap<String, cel::Value> = variables
                .iter()
                .map(|(name, ty)| {
                    let json = context.get(name).unwrap_or(&serde_json::Value::Null);
                    (name.clone(), value::value(json, ty, &probes))
                })
                .collect();
            let mut scope = cel::Context::with_env(Arc::clone(probes.env()));
            for (name, v) in &values {
                scope.add_variable_from_value(name.clone(), v.clone());
            }
            analysed
                .iter()
                .map(|a| match a {
                    Ok(a) => a.run(&scope, &values),
                    Err(rejection) => RuleEvaluated::Error {
                        error: rejection.error.clone(),
                        empty: false,
                    },
                })
                .collect()
        })
        .collect()
}
