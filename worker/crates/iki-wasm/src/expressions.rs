//! Expressions in apps (ADR 0090): Google's Common Expression Language, with the `cel`
//! crate. The browser evaluates them here, in a Web Worker; the server checks them with
//! this same code, run by wasmtime, so what it accepts is what browsers run.

use std::collections::BTreeSet;

use cel::common::ast::{EntryExpr, Expr};
use serde::{Deserialize, Serialize};

/// The longest expression, in characters.
pub const MAX_LENGTH: usize = 1000;
/// How deeply an expression may nest, which bounds how much work it is to read.
pub const MAX_DEPTH: u16 = 32;

/// What [`check`] makes of one expression.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum Checked {
    /// It reads, and these are the names it uses, sorted.
    Valid {
        /// The variables it reads, such as a panel's ID.
        names: Vec<String>,
    },
    /// It doesn't.
    Invalid {
        /// Why, for a person.
        error: String,
    },
}

fn compile(source: &str) -> Result<cel::Program, String> {
    if source.chars().count() > MAX_LENGTH {
        return Err(format!(
            "It's over {MAX_LENGTH} characters, the most an expression may be."
        ));
    }
    if source.trim().is_empty() {
        return Err("It's empty.".to_owned());
    }
    let unreadable = |errors: &cel::ParseErrors| {
        let first = errors.errors.first();
        first.map_or_else(
            || "It isn't an expression.".to_owned(),
            |e| {
                if e.msg.contains("Recursion limit") {
                    return format!("It nests more than {MAX_DEPTH} deep.");
                }
                let (line, column) = e.pos;
                format!("{}, at line {line}, column {column}.", plainly(&e.msg))
            },
        )
    };
    cel::parser::Parser::new()
        .max_recursion_depth(MAX_DEPTH)
        .parse(source)
        .map_err(|errors| unreadable(&errors))?;
    cel::Program::compile(source).map_err(|errors| unreadable(&errors))
}

/// What an expression reads that it doesn't bind itself, as the variables of `map`,
/// `filter`, `all` and `exists` are: names, and the fields it selects of them.
#[derive(Default)]
struct Uses {
    names: BTreeSet<String>,
    /// As `name.field`.
    fields: BTreeSet<String>,
}

fn free(node: &cel::IdedExpr, bound: &mut Vec<String>, uses: &mut Uses) {
    match &node.expr {
        Expr::Ident(name) => {
            if !bound.contains(name) {
                uses.names.insert(name.clone());
            }
        }
        Expr::Select(select) => {
            if let Expr::Ident(name) = &select.operand.expr
                && !bound.contains(name)
            {
                uses.fields.insert(format!("{name}.{}", select.field));
            }
            free(&select.operand, bound, uses);
        }
        Expr::Call(call) => {
            if let Some(target) = &call.target {
                free(target, bound, uses);
            }
            for arg in &call.args {
                free(arg, bound, uses);
            }
        }
        Expr::List(list) => {
            for element in &list.elements {
                free(element, bound, uses);
            }
        }
        Expr::Map(map) => entries(&map.entries, bound, uses),
        Expr::Struct(structure) => entries(&structure.entries, bound, uses),
        Expr::Comprehension(c) => {
            // The range and the start are read outside it; the rest with its variables.
            free(&c.iter_range, bound, uses);
            free(&c.accu_init, bound, uses);
            let before = bound.len();
            bound.push(c.iter_var.clone());
            if let Some(second) = &c.iter_var2 {
                bound.push(second.clone());
            }
            bound.push(c.accu_var.clone());
            free(&c.loop_cond, bound, uses);
            free(&c.loop_step, bound, uses);
            free(&c.result, bound, uses);
            bound.truncate(before);
        }
        Expr::Literal(_) | Expr::Unspecified => {}
    }
}

fn entries(entries: &[cel::common::ast::IdedEntryExpr], bound: &mut Vec<String>, uses: &mut Uses) {
    for entry in entries {
        match &entry.expr {
            EntryExpr::MapEntry(e) => {
                free(&e.key, bound, uses);
                free(&e.value, bound, uses);
            }
            EntryExpr::StructField(f) => free(&f.value, bound, uses),
        }
    }
}

/// The names an expression reads, and the fields it selects of them (as `name.field`),
/// sorted; or why it doesn't read.
///
/// # Errors
///
/// When it doesn't read, saying why for a person.
pub fn uses(source: &str) -> Result<(Vec<String>, Vec<String>), String> {
    let program = compile(source)?;
    let mut found = Uses::default();
    free(program.expression(), &mut Vec::new(), &mut found);
    Ok((
        found.names.into_iter().collect(),
        found.fields.into_iter().collect(),
    ))
}

/// The parser's message, for a person: ANTLR's "mismatched input ')' expecting {…}"
/// becomes "It doesn't expect ')' here".
pub(crate) fn plainly(message: &str) -> String {
    let message = message.strip_prefix("Syntax error: ").unwrap_or(message);
    if message.contains("'<EOF>'") {
        return "It ends too soon".to_owned();
    }
    for opening in [
        "mismatched input '",
        "extraneous input '",
        "no viable alternative at input '",
        "token recognition error at: '",
    ] {
        if let Some(rest) = message.strip_prefix(opening) {
            // The token ends at the quote before " expecting …", or at the last quote.
            let end = rest.find("' ").or_else(|| rest.rfind('\''));
            if let Some(end) = end {
                let token: String = rest[..end].chars().take(20).collect();
                return format!("It doesn't expect '{token}' here");
            }
        }
    }
    message.trim_end_matches('.').to_owned()
}

/// Whether each expression reads, and what names it uses.
#[must_use]
pub fn check(sources: &[String]) -> Vec<Checked> {
    sources
        .iter()
        .map(|source| match uses(source) {
            Ok((names, _)) => Checked::Valid { names },
            Err(error) => Checked::Invalid { error },
        })
        .collect()
}

/// What to evaluate: expressions, with the variables they may read and, for a grid's
/// columns, rows, each read as `row`.
#[derive(Debug, Clone, Deserialize)]
pub struct Evaluation {
    /// The expressions.
    pub expressions: Vec<String>,
    /// The names they may read, and their values.
    #[serde(default)]
    pub variables: serde_json::Map<String, serde_json::Value>,
    /// Rows: when given, each expression is evaluated for each row, as `row`.
    #[serde(default)]
    pub rows: Option<Vec<serde_json::Value>>,
}

/// One expression's value, or why it has none.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(untagged)]
pub enum Evaluated {
    /// Its value.
    Value {
        /// As JSON.
        value: serde_json::Value,
    },
    /// Why it has none.
    Error {
        /// For a person.
        error: String,
    },
}

/// JSON as CEL reads it. A whole number is an `int`, as JSON readers take it, so
/// `row.length * 2` works (CEL won't multiply a `uint` by an `int`); one too large for
/// that is a `uint`, and any other number a `double`.
pub(crate) fn value(json: &serde_json::Value) -> cel::Value {
    match json {
        serde_json::Value::Null => cel::Value::Null,
        serde_json::Value::Bool(b) => cel::Value::Bool(*b),
        serde_json::Value::Number(n) => n.as_i64().map_or_else(
            || {
                n.as_u64().map_or_else(
                    || cel::Value::Float(n.as_f64().unwrap_or(f64::NAN)),
                    cel::Value::UInt,
                )
            },
            cel::Value::Int,
        ),
        serde_json::Value::String(text) => cel::Value::String(text.clone().into()),
        serde_json::Value::Array(items) => {
            cel::Value::List(items.iter().map(value).collect::<Vec<_>>().into())
        }
        serde_json::Value::Object(fields) => fields
            .iter()
            .map(|(k, v)| (k.clone(), value(v)))
            .collect::<std::collections::HashMap<String, cel::Value>>()
            .into(),
    }
}

fn context(
    variables: &serde_json::Map<String, serde_json::Value>,
) -> cel::Context<'static, 'static> {
    let mut context = cel::Context::default();
    for (name, json) in variables {
        context.add_variable_from_value(name.clone(), value(json));
    }
    context
}

fn run(program: &cel::Program, context: &cel::Context<'_, '_>) -> Evaluated {
    match program.execute(context) {
        Ok(value) => match value.json() {
            Ok(value) => Evaluated::Value { value },
            Err(_) => Evaluated::Error {
                error: "Its value can't be shown.".to_owned(),
            },
        },
        Err(error) => Evaluated::Error {
            error: format!("{}.", failure(&error)),
        },
    }
}

/// What kind of value it is, for a person, without what's in it.
pub(crate) fn kind(value: &cel::Value) -> &'static str {
    match value {
        cel::Value::List(_) => "a list",
        cel::Value::Map(_) => "a map",
        cel::Value::Int(_) | cel::Value::UInt(_) => "a whole number",
        cel::Value::Float(_) => "a number",
        cel::Value::String(_) => "text",
        cel::Value::Bytes(_) => "bytes",
        cel::Value::Bool(_) => "yes or no",
        cel::Value::Null => "nothing",
        _ => "a value",
    }
}

/// Why an expression failed, for a person. cel's own messages print values as Rust
/// sees them (`Map({...})`), which would also put what a row holds in an error.
pub(crate) fn failure(error: &cel::ExecutionError) -> String {
    use cel::ExecutionError as E;
    let name = |n: &str| n.chars().take(40).collect::<String>();
    let message = match error {
        E::InvalidArgumentCount { expected, actual } => {
            format!("It takes {expected} arguments, not {actual}")
        }
        E::UnsupportedTargetType { target } => format!("It can't be used on {}", kind(target)),
        E::NotSupportedAsMethod { method, target } => {
            format!("{} has no `{}()`", kind(target), name(method))
        }
        E::UnsupportedKeyType(key) => format!("{} can't be a map's key", kind(key)),
        E::UnexpectedType { got, want } => format!("It's {got} where {want} is wanted"),
        E::NoSuchKey(key) => format!("There's no `{}`", name(key)),
        E::NoSuchOverload(overload) => format!(
            "`{}` doesn't take {}",
            name(overload.function()),
            overload.argument_types().join(" and ")
        ),
        E::UndeclaredReference(reference) => {
            format!("There's nothing called `{}`", name(reference))
        }
        E::MissingArgumentOrTarget => "It's missing an argument".to_owned(),
        E::ValuesNotComparable(a, b) => {
            format!("{} can't be compared with {}", kind(a), kind(b))
        }
        E::UnsupportedBinaryOperator(op, a, b) => {
            format!("`{op}` doesn't work on {} and {}", kind(a), kind(b))
        }
        E::UnsupportedIndex(index, of) => format!("{} can't index {}", kind(index), kind(of)),
        E::FunctionError { function, .. } => format!("`{}()` failed", name(function)),
        E::DivisionByZero(_) | E::RemainderByZero(_) => "It divides by zero".to_owned(),
        E::Overflow(op, ..) => format!("`{op}` makes a number too large"),
        E::IndexOutOfBounds(cel::Value::Int(index)) => format!("There's no item {index}"),
        E::IndexOutOfBounds(_) => "There's no such item".to_owned(),
        E::DuplicateKey(_) => "A map has the same key twice".to_owned(),
        _ => "It can't be worked out".to_owned(),
    };
    let mut chars = message.chars();
    let short: String = chars.by_ref().take(200).collect();
    if chars.next().is_some() {
        format!("{short}…")
    } else {
        short
    }
}

/// Each expression's value: a list of one, or of one for each row.
#[must_use]
pub fn evaluate(evaluation: &Evaluation) -> Vec<Vec<Evaluated>> {
    let shared = context(&evaluation.variables);
    evaluation
        .expressions
        .iter()
        .map(|source| {
            let program = match compile(source) {
                Ok(program) => program,
                Err(error) => {
                    let count = evaluation.rows.as_ref().map_or(1, Vec::len);
                    return vec![Evaluated::Error { error }; count];
                }
            };
            match &evaluation.rows {
                None => vec![run(&program, &shared)],
                Some(rows) => rows
                    .iter()
                    .map(|row| {
                        let mut scope = shared.new_inner_scope();
                        scope.add_variable_from_value("row", value(row));
                        run(&program, &scope)
                    })
                    .collect(),
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn evaluation(value: serde_json::Value) -> Evaluation {
        match serde_json::from_value(value) {
            Ok(e) => e,
            Err(e) => unreachable!("{e}"),
        }
    }

    #[test]
    fn says_which_names_an_expression_reads() {
        let checked = check(&[
            "size(proteins.selection) > 0 && inputs.entity.hrid != ''".to_owned(),
            "proteins.rows.map(r, r.values.length).exists(l, l > limit)".to_owned(),
            "[1, 2].all(x, [3].exists(y, x < y)) && x == 1".to_owned(),
        ]);
        assert_eq!(
            checked,
            vec![
                Checked::Valid {
                    names: vec!["inputs".to_owned(), "proteins".to_owned()]
                },
                // The variables `map` and `exists` bind aren't names it reads.
                Checked::Valid {
                    names: vec!["limit".to_owned(), "proteins".to_owned()]
                },
                // Outside `all`, x is a name again.
                Checked::Valid {
                    names: vec!["x".to_owned()]
                },
            ]
        );
    }

    #[test]
    fn says_why_an_expression_doesnt_read() {
        // The parser recurses for each level, which a debug build's test thread hasn't the
        // stack for; a release build, as browsers and the server run, has.
        let checked = std::thread::Builder::new()
            .stack_size(64 << 20)
            .spawn(|| {
                let deep = format!("{}1{}", "(".repeat(40), ")".repeat(40));
                let long = "a".repeat(MAX_LENGTH + 1);
                check(&["1 +".to_owned(), String::new(), deep, long])
            })
            .map(std::thread::JoinHandle::join);
        let Ok(Ok(checked)) = checked else {
            unreachable!("the check stopped")
        };
        let errors: Vec<String> = checked
            .into_iter()
            .map(|c| match c {
                Checked::Invalid { error } => error,
                Checked::Valid { .. } => unreachable!("it read"),
            })
            .collect();
        assert_eq!(errors[0], "It ends too soon, at line 1, column 4.");
        assert_eq!(errors[1], "It's empty.");
        assert_eq!(errors[2], "It nests more than 32 deep.");
        assert_eq!(
            errors[3],
            "It's over 1000 characters, the most an expression may be."
        );
    }

    #[test]
    fn says_plainly_what_the_parser_doesnt_expect() {
        let checked = check(&["a + )".to_owned(), "a $ b".to_owned()]);
        let errors: Vec<String> = checked
            .into_iter()
            .map(|c| match c {
                Checked::Invalid { error } => error,
                Checked::Valid { .. } => unreachable!("it read"),
            })
            .collect();
        assert_eq!(
            errors[0],
            "It doesn't expect ')' here, at line 1, column 5."
        );
        assert!(
            errors[1].starts_with("It doesn't expect '$"),
            "{}",
            errors[1]
        );
    }

    #[test]
    fn evaluates_with_the_variables_given() {
        let out = evaluate(&evaluation(json!({
            "expressions": [
                "size(proteins.selection)",
                "proteins.selection[0].hrid",
                "string(size(proteins.selection)) + ' picked'",
                "proteins.selection[0].values.weight / 1000.0",
                "missing + 1",
            ],
            "variables": {
                "proteins": {"selection": [
                    {"hrid": "PRT-000001", "values": {"weight": 15000.5}}
                ]}
            }
        })));
        assert_eq!(out[0], vec![Evaluated::Value { value: json!(1) }]);
        assert_eq!(
            out[1],
            vec![Evaluated::Value {
                value: json!("PRT-000001")
            }]
        );
        assert_eq!(
            out[2],
            vec![Evaluated::Value {
                value: json!("1 picked")
            }]
        );
        assert_eq!(
            out[3],
            vec![Evaluated::Value {
                value: json!(15.0005)
            }]
        );
        assert!(
            matches!(&out[4][0], Evaluated::Error { error } if error.contains("missing")),
            "{:?}",
            out[4]
        );
    }

    #[test]
    fn evaluates_each_row_as_row() {
        let out = evaluate(&evaluation(json!({
            "expressions": ["row.length * 2", "row.name + suffix"],
            "variables": {"suffix": "!"},
            "rows": [{"length": 3, "name": "a"}, {"length": 5, "name": "b"}]
        })));
        assert_eq!(
            out,
            vec![
                vec![
                    Evaluated::Value { value: json!(6) },
                    Evaluated::Value { value: json!(10) }
                ],
                vec![
                    Evaluated::Value { value: json!("a!") },
                    Evaluated::Value { value: json!("b!") }
                ],
            ]
        );
    }

    #[test]
    fn an_unreadable_expression_is_an_error_for_every_row() {
        let out = evaluate(&evaluation(json!({
            "expressions": ["row."],
            "rows": [{}, {}]
        })));
        assert_eq!(out[0].len(), 2);
        assert!(out[0].iter().all(|e| matches!(e, Evaluated::Error { .. })));
    }

    #[test]
    fn says_why_it_failed_without_showing_values() {
        let out = evaluate(&evaluation(json!({
            "expressions": [
                "row.secret",
                "row + 1",
                "row < 1",
                "[1, 2][5]",
                "row.size(1)",
                "nothing.here",
            ],
            "variables": {"row": {"name": "confidential sequence"}},
        })));
        let errors: Vec<String> = out
            .iter()
            .map(|values| match &values[0] {
                Evaluated::Error { error } => error.clone(),
                Evaluated::Value { value } => format!("a value: {value}"),
            })
            .collect();
        assert_eq!(errors[0], "There's no `secret`.");
        assert_eq!(errors[3], "There's no item 5.");
        assert_eq!(errors[5], "There's nothing called `nothing`.");
        for error in &errors {
            assert!(!error.contains("confidential"), "{error}");
            assert!(!error.contains("Map("), "{error}");
        }
    }
}
