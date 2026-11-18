//! Text with expressions in it (ADR 0090): `{{ size(proteins.selection) }} picked`. Each
//! part between `{{` and `}}` is a CEL expression; the rest is text, shown as it is. The
//! browser renders templates here and the server checks them here, so both split them the
//! same way, CEL's strings and maps (`{{ {"a": {"b": 1}} }}`) and all.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

use crate::expressions::{self, Evaluated};

/// The longest template, in characters.
pub const MAX_TEMPLATE: usize = 10_000;
/// The most expressions one template may have.
pub const MAX_PARTS: usize = 50;

/// A piece of a template.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Part<'a> {
    /// Text, shown as it is.
    Text(&'a str),
    /// An expression, without its braces.
    Expression(&'a str),
}

/// Where the expression that starts at `start` (just after its `{{`) ends: the `}}` that
/// isn't in a string and closes no brace of its own.
fn closing(template: &str, start: usize) -> Option<usize> {
    let bytes = template.as_bytes();
    let mut depth = 0usize;
    let mut i = start;
    while i < bytes.len() {
        match bytes[i] {
            quote @ (b'"' | b'\'') => {
                // A raw string (r"…") has no escapes; a triple-quoted one may span lines.
                let raw = i > start && matches!(bytes[i - 1], b'r' | b'R');
                let triple = bytes[i..].starts_with(&[quote; 3]);
                let width = if triple { 3 } else { 1 };
                i += width;
                loop {
                    if i >= bytes.len() {
                        return None;
                    }
                    if !raw && bytes[i] == b'\\' {
                        i += 2;
                        continue;
                    }
                    if bytes[i..].starts_with(&[quote; 3][..width]) {
                        i += width;
                        break;
                    }
                    i += 1;
                }
                continue;
            }
            b'{' => depth += 1,
            b'}' if depth > 0 => depth -= 1,
            b'}' if bytes.get(i + 1) == Some(&b'}') => return Some(i),
            _ => {}
        }
        i += 1;
    }
    None
}

/// The template's parts, in order, or why it doesn't split.
///
/// # Errors
///
/// When it's too long, has too many expressions, or a `{{` isn't closed.
pub fn parts(template: &str) -> Result<Vec<Part<'_>>, String> {
    if template.chars().count() > MAX_TEMPLATE {
        return Err(format!(
            "It's over {MAX_TEMPLATE} characters, the most text may be."
        ));
    }
    let mut out = Vec::new();
    let mut rest = 0;
    while let Some(found) = template[rest..].find("{{") {
        let open = rest + found;
        if open > rest {
            out.push(Part::Text(&template[rest..open]));
        }
        let start = open + 2;
        let Some(end) = closing(template, start) else {
            let column = template[..open].chars().count() + 1;
            return Err(format!("The `{{{{` at character {column} isn't closed."));
        };
        out.push(Part::Expression(&template[start..end]));
        rest = end + 2;
    }
    if rest < template.len() {
        out.push(Part::Text(&template[rest..]));
    }
    let count = out
        .iter()
        .filter(|p| matches!(p, Part::Expression(_)))
        .count();
    if count > MAX_PARTS {
        return Err(format!(
            "It has {count} expressions; text may have at most {MAX_PARTS}."
        ));
    }
    Ok(out)
}

/// The expression a template is all of, `{{ proteins.selection }}` say, whose value is
/// then the template's: an entity, a list, a number. Text around it, even spaces, makes
/// it text.
fn whole<'a>(parts: &[Part<'a>]) -> Option<&'a str> {
    match parts {
        [Part::Expression(e)] => Some(e),
        _ => None,
    }
}

/// What [`check`] makes of one template.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum Checked {
    /// Each expression in it reads.
    Valid {
        /// The variables they read, sorted.
        names: Vec<String>,
        /// The fields they read of those variables, as `name.field`, sorted: what a
        /// page can say a panel doesn't offer.
        fields: Vec<String>,
        /// Whether it's one expression and nothing else, so its value is that one's.
        whole: bool,
    },
    /// It doesn't: why, for a person, saying which expression.
    Invalid {
        /// For a person.
        error: String,
    },
}

/// The most of an expression a message quotes: enough to find it, never a page of it.
const QUOTED: usize = 60;

/// Where a failure is, for a person: "In `{{ size(rows) }}`", its start if it's long.
pub(crate) fn quoted(source: &str) -> String {
    if source.chars().count() <= QUOTED {
        return format!("In `{{{{{source}}}}}`");
    }
    let start: String = source.chars().take(QUOTED).collect();
    format!("In `{{{{{start}…}}}}`")
}

/// Whether each template reads, and the names and fields its expressions use.
#[must_use]
pub fn check(templates: &[String]) -> Vec<Checked> {
    templates
        .iter()
        .map(|template| {
            let parts = match parts(template) {
                Ok(parts) => parts,
                Err(error) => return Checked::Invalid { error },
            };
            let mut names = BTreeSet::new();
            let mut fields = BTreeSet::new();
            for part in &parts {
                let Part::Expression(source) = part else {
                    continue;
                };
                match expressions::uses(source) {
                    Ok((n, f)) => {
                        names.extend(n);
                        fields.extend(f);
                    }
                    Err(error) => {
                        return Checked::Invalid {
                            error: format!("{}: {error}", quoted(source)),
                        };
                    }
                }
            }
            Checked::Valid {
                names: names.into_iter().collect(),
                fields: fields.into_iter().collect(),
                whole: whole(&parts).is_some(),
            }
        })
        .collect()
}

/// What to render: templates, the variables their expressions may read and, for a grid's
/// columns, rows, each read as `row`.
#[derive(Debug, Clone, Deserialize)]
pub struct Rendering {
    /// The templates.
    pub templates: Vec<String>,
    /// The names their expressions may read, and their values.
    #[serde(default)]
    pub variables: serde_json::Map<String, serde_json::Value>,
    /// Rows: when given, each template is rendered for each row, as `row`.
    #[serde(default)]
    pub rows: Option<Vec<serde_json::Value>>,
}

/// A value as text: text as it is, numbers as they are written, yes or no, lists'
/// items with commas between them, and an entity (anything with a `title`) by its title.
#[must_use]
pub fn text(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::Null => String::new(),
        serde_json::Value::Bool(true) => "Yes".to_owned(),
        serde_json::Value::Bool(false) => "No".to_owned(),
        serde_json::Value::Number(n) => n.to_string(),
        serde_json::Value::String(s) => s.clone(),
        serde_json::Value::Array(items) => items.iter().map(text).collect::<Vec<_>>().join(", "),
        serde_json::Value::Object(fields) => match fields.get("title").or(fields.get("hrid")) {
            Some(serde_json::Value::String(title)) => title.clone(),
            _ => value.to_string(),
        },
    }
}

/// Each template's value, for each row or just once: a whole expression's value as it
/// is, or text with each expression's value written in. An expression that fails makes
/// the template fail, saying which one.
#[must_use]
pub fn render(rendering: &Rendering) -> Vec<Vec<Evaluated>> {
    let count = rendering.rows.as_ref().map_or(1, Vec::len);
    let split: Vec<Result<Vec<Part<'_>>, String>> =
        rendering.templates.iter().map(|t| parts(t)).collect();
    // Every template's expressions at once, so the variables and rows are read once.
    let sources: Vec<String> = split
        .iter()
        .flatten()
        .flatten()
        .filter_map(|p| match p {
            Part::Expression(e) => Some((*e).to_owned()),
            Part::Text(_) => None,
        })
        .collect();
    let values = expressions::evaluate(&expressions::Evaluation {
        expressions: sources,
        variables: rendering.variables.clone(),
        rows: rendering.rows.clone(),
    });
    let mut next = 0;
    split
        .iter()
        .map(|split| {
            let parts = match split {
                Ok(parts) => parts,
                Err(error) => {
                    return vec![
                        Evaluated::Error {
                            error: error.clone()
                        };
                        count
                    ];
                }
            };
            let first = next;
            next += parts
                .iter()
                .filter(|p| matches!(p, Part::Expression(_)))
                .count();
            (0..count)
                .map(|row| assemble(parts, &values[first..next], row))
                .collect()
        })
        .collect()
}

/// One template's value for one row, from its expressions' values.
fn assemble(parts: &[Part<'_>], values: &[Vec<Evaluated>], row: usize) -> Evaluated {
    if whole(parts).is_some() {
        return values[0][row].clone();
    }
    let mut out = String::new();
    let mut next = 0;
    for part in parts {
        match part {
            Part::Text(t) => out.push_str(t),
            Part::Expression(source) => {
                match &values[next][row] {
                    Evaluated::Value { value } => out.push_str(&text(value)),
                    Evaluated::Error { error } => {
                        return Evaluated::Error {
                            error: format!("{}: {error}", quoted(source)),
                        };
                    }
                }
                next += 1;
            }
        }
    }
    Evaluated::Value {
        value: serde_json::Value::String(out),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn rendered(templates: &[&str], variables: &serde_json::Value) -> Vec<Vec<Evaluated>> {
        render(&Rendering {
            templates: templates.iter().map(|t| (*t).to_owned()).collect(),
            variables: variables.as_object().cloned().unwrap_or_default(),
            rows: None,
        })
    }

    fn value(v: serde_json::Value) -> Evaluated {
        Evaluated::Value { value: v }
    }

    #[test]
    fn a_failure_quotes_a_long_expression_by_its_start() {
        // Over the most an expression may be: 600 terms.
        let body = format!(" {} ", ["1"; 600].join(" + "));
        let checked = check(&[format!("{{{{{body}}}}}")]);
        let start: String = body.chars().take(60).collect();
        let quoted = format!("In `{{{{{start}…}}}}`: ");
        assert!(
            matches!(&checked[..], [Checked::Invalid { error }] if error.starts_with(&quoted) && error.len() < 200),
            "{checked:?}"
        );
        let short = check(&["{{ 1 + }}".to_owned()]);
        assert!(
            matches!(&short[0], Checked::Invalid { error } if error.starts_with("In `{{ 1 + }}`: "))
        );
    }

    #[test]
    fn splits_text_from_expressions() {
        assert_eq!(
            parts("{{ a }} of {{ b }}!").unwrap(),
            vec![
                Part::Expression(" a "),
                Part::Text(" of "),
                Part::Expression(" b "),
                Part::Text("!"),
            ]
        );
        assert_eq!(parts("no braces").unwrap(), vec![Part::Text("no braces")]);
        assert_eq!(parts("").unwrap(), vec![]);
        // A lone closing pair is text.
        assert_eq!(parts("a }} b").unwrap(), vec![Part::Text("a }} b")]);
    }

    #[test]
    fn an_expression_ends_at_the_braces_that_close_it() {
        // A map's braces, and braces in strings, aren't the end.
        assert_eq!(
            parts(r#"{{ {"a": {"b": 1}} }}"#).unwrap(),
            vec![Part::Expression(r#" {"a": {"b": 1}} "#)]
        );
        assert_eq!(
            parts(r#"{{ "}}" + '}}' }} x"#).unwrap(),
            vec![Part::Expression(r#" "}}" + '}}' "#), Part::Text(" x")]
        );
        assert_eq!(
            parts(r#"{{ "a\"}}" }}"#).unwrap(),
            vec![Part::Expression(r#" "a\"}}" "#)]
        );
        assert_eq!(
            parts(r#"{{ """}}""" }}"#).unwrap(),
            vec![Part::Expression(r#" """}}""" "#)]
        );
        assert_eq!(
            parts(r#"{{ r"\" }}"#).unwrap(),
            vec![Part::Expression(r#" r"\" "#)]
        );
    }

    #[test]
    fn says_where_an_expression_isnt_closed() {
        assert_eq!(
            parts("ab {{ x").unwrap_err(),
            "The `{{` at character 4 isn't closed."
        );
        assert_eq!(
            parts(r#"{{ "}} "#).unwrap_err(),
            "The `{{` at character 1 isn't closed."
        );
        let many = "{{ 1 }}".repeat(MAX_PARTS + 1);
        assert!(parts(&many).unwrap_err().contains("at most 50"));
        assert!(
            parts(&"a".repeat(MAX_TEMPLATE + 1))
                .unwrap_err()
                .contains("over 10000")
        );
    }

    #[test]
    fn checks_each_expression_and_what_it_reads() {
        let checked = check(&[
            "{{ size(proteins.selection) }} picked of {{ proteins.rows.size() }}".to_owned(),
            "{{ viewer.entity.title }}".to_owned(),
            "{{ items.map(i, i.x) }}".to_owned(),
            "Just text".to_owned(),
            "{{ 1 + }} and more".to_owned(),
        ]);
        assert_eq!(
            checked[0],
            Checked::Valid {
                names: vec!["proteins".to_owned()],
                fields: vec!["proteins.rows".to_owned(), "proteins.selection".to_owned()],
                whole: false,
            }
        );
        // Only the field of the name itself, not deeper ones.
        assert_eq!(
            checked[1],
            Checked::Valid {
                names: vec!["viewer".to_owned()],
                fields: vec!["viewer.entity".to_owned()],
                whole: true,
            }
        );
        // A comprehension's variable is its own, and so are its fields.
        assert_eq!(
            checked[2],
            Checked::Valid {
                names: vec!["items".to_owned()],
                fields: vec![],
                whole: true,
            }
        );
        assert_eq!(
            checked[3],
            Checked::Valid {
                names: vec![],
                fields: vec![],
                whole: false,
            }
        );
        assert_eq!(
            checked[4],
            Checked::Invalid {
                error: "In `{{ 1 + }}`: It ends too soon, at line 1, column 6.".to_owned()
            }
        );
    }

    #[test]
    fn a_whole_expression_keeps_its_value() {
        let variables = json!({"grid": {"selection": [{"hrid": "PROT-1", "title": "Kinase"}]}});
        assert_eq!(
            rendered(
                &["{{ grid.selection }}", "{{ size(grid.selection) * 2 }}"],
                &variables
            ),
            vec![
                vec![value(json!([{"hrid": "PROT-1", "title": "Kinase"}]))],
                vec![value(json!(2))],
            ]
        );
    }

    #[test]
    fn text_has_values_written_in() {
        let variables = json!({
            "grid": {"selection": [{"hrid": "PROT-1", "title": "Kinase"}, {"hrid": "PROT-2"}]},
            "flag": true,
            "ratio": 0.5,
            "none": null,
        });
        assert_eq!(
            rendered(
                &[
                    "{{ size(grid.selection) }} picked: {{ grid.selection }}.",
                    " {{ flag }}, {{ !flag }}, {{ ratio }}, [{{ none }}]",
                    "{{ {\"k\": 1} }} ",
                ],
                &variables
            ),
            vec![
                vec![value(json!("2 picked: Kinase, PROT-2."))],
                vec![value(json!(" Yes, No, 0.5, []"))],
                vec![value(json!("{\"k\":1} "))],
            ]
        );
    }

    #[test]
    fn a_failing_expression_says_which() {
        assert_eq!(
            rendered(&["a {{ 1 }} b {{ missing.x }}", "{{ 1 / 0 }}"], &json!({})),
            vec![
                vec![Evaluated::Error {
                    error: "In `{{ missing.x }}`: There's nothing called `missing`.".to_owned()
                }],
                vec![Evaluated::Error {
                    error: "It divides by zero.".to_owned()
                }],
            ]
        );
    }

    #[test]
    fn renders_each_row() {
        let out = render(&Rendering {
            templates: vec![
                "{{ row.values.length }} aa".to_owned(),
                "{{ unclosed".to_owned(),
            ],
            variables: serde_json::Map::new(),
            rows: Some(vec![
                json!({"values": {"length": 120}}),
                json!({"values": {"length": 85}}),
            ]),
        });
        assert_eq!(out[0], vec![value(json!("120 aa")), value(json!("85 aa"))]);
        assert_eq!(out[1].len(), 2);
    }
}
