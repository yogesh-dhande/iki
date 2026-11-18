//! What a rule's values are: the declared variables' schemas as the checker reads them,
//! and how a kind of value is named for a person.

use std::collections::BTreeMap;
use std::rc::Rc;

use super::Schema;

/// The text a string can be, as a field declares it: an enum's values, or a state
/// field's states.
#[derive(Debug, PartialEq, Eq)]
pub(super) struct Choices {
    /// What a person calls the field, as `Status`.
    pub label: String,
    /// The only text it can be.
    pub values: Vec<String>,
}

/// A value with known fields, as a record is.
#[derive(Debug, PartialEq)]
pub(super) struct Object {
    /// What a person calls it, as `Sample`.
    pub label: String,
    /// Its fields, by name.
    pub fields: BTreeMap<String, Ty>,
}

/// What an expression's value is, as far as the checker can tell. Any of them may be
/// null at runtime: a field may be empty.
#[derive(Debug, Clone, PartialEq)]
pub(super) enum Ty {
    /// Yes or no.
    Bool,
    /// A signed whole number.
    Int,
    /// An unsigned whole number.
    UInt,
    /// A floating-point number.
    Double,
    /// Text, and the only text it can be if it's declared.
    Str(Option<Rc<Choices>>),
    /// A point in time.
    Timestamp,
    /// A length of time.
    Duration,
    /// Anything: the checker can't tell.
    Dyn,
    /// `null`, as written.
    Null,
    /// A list of values of one kind.
    List(Box<Ty>),
    /// A map from text to values of one kind.
    Map(Box<Ty>),
    /// A value with known fields.
    Object(Rc<Object>),
}

impl Ty {
    /// What a variable or field declared with `schema` is. `name` labels a string with
    /// values that has no label of its own.
    pub(super) fn of(schema: &Schema, name: &str) -> Self {
        match schema {
            Schema::Bool => Self::Bool,
            Schema::Int => Self::Int,
            Schema::Uint => Self::UInt,
            Schema::Double => Self::Double,
            Schema::String { label, values } => Self::Str(
                values
                    .as_ref()
                    .filter(|values| !values.is_empty())
                    .map(|values| {
                        Rc::new(Choices {
                            label: label.clone().unwrap_or_else(|| name.to_owned()),
                            values: values.clone(),
                        })
                    }),
            ),
            Schema::Timestamp => Self::Timestamp,
            Schema::Duration => Self::Duration,
            Schema::Dyn => Self::Dyn,
            Schema::List { items } => Self::List(Box::new(Self::of(items, name))),
            Schema::Map { values } => Self::Map(Box::new(Self::of(values, name))),
            Schema::Object { label, fields } => Self::Object(Rc::new(Object {
                label: label.clone(),
                fields: fields
                    .iter()
                    .map(|(field, schema)| (field.clone(), Self::of(schema, field)))
                    .collect(),
            })),
        }
    }

    /// Whether it's a number of any kind.
    pub(super) fn numeric(&self) -> bool {
        matches!(self, Self::Int | Self::UInt | Self::Double)
    }

    /// Whether it's yes or no, or may be.
    pub(super) fn boolish(&self) -> bool {
        matches!(self, Self::Bool | Self::Dyn)
    }

    /// Whether it's text, or may be.
    pub(super) fn textual(&self) -> bool {
        matches!(self, Self::Str(_) | Self::Dyn)
    }

    /// Whether it's a map of either kind, whose keys are text.
    pub(super) fn keyed(&self) -> bool {
        matches!(self, Self::Map(_) | Self::Object(_))
    }

    /// What a person calls this kind of value, as "a whole number (int)".
    pub(super) fn describe(&self) -> String {
        match self {
            Self::Bool => "yes or no (bool)".to_owned(),
            Self::Int => "a whole number (int)".to_owned(),
            Self::UInt => "a whole number (uint)".to_owned(),
            Self::Double => "a number (double)".to_owned(),
            Self::Str(_) => "text (string)".to_owned(),
            Self::Timestamp => "a time (timestamp)".to_owned(),
            Self::Duration => "a duration".to_owned(),
            Self::Dyn => "any value".to_owned(),
            Self::Null => "null".to_owned(),
            Self::List(_) => "a list".to_owned(),
            Self::Map(_) => "a map".to_owned(),
            Self::Object(object) => object.label.clone(),
        }
    }
}

/// Whether values of these kinds can ever be equal, as cel's `==` sees it: numbers of
/// any kind with each other, null and anything else with anything (every value may be
/// null), and otherwise only the same kind.
pub(super) fn comparable(a: &Ty, b: &Ty) -> bool {
    match (a, b) {
        (Ty::Dyn | Ty::Null, _) | (_, Ty::Dyn | Ty::Null) => true,
        (Ty::List(x), Ty::List(y)) => comparable(x, y),
        (x, y) if x.numeric() && y.numeric() => true,
        (x, y) if x.keyed() && y.keyed() => true,
        (Ty::Bool, Ty::Bool)
        | (Ty::Str(_), Ty::Str(_))
        | (Ty::Timestamp, Ty::Timestamp)
        | (Ty::Duration, Ty::Duration) => true,
        _ => false,
    }
}

/// Whether `<`, `<=`, `>` and `>=` order these, as cel's comparisons do: numbers of any
/// kind with each other (cel-rust compares an int with a double), and text, times,
/// durations and yes or no each with their own kind.
pub(super) fn orderable(a: &Ty, b: &Ty) -> bool {
    let ordered = |t: &Ty| {
        t.numeric()
            || matches!(
                t,
                Ty::Str(_) | Ty::Bool | Ty::Timestamp | Ty::Duration | Ty::Dyn
            )
    };
    match (a, b) {
        (Ty::Dyn, x) | (x, Ty::Dyn) => ordered(x),
        (x, y) if x.numeric() && y.numeric() => true,
        (Ty::Str(_), Ty::Str(_))
        | (Ty::Bool, Ty::Bool)
        | (Ty::Timestamp, Ty::Timestamp)
        | (Ty::Duration, Ty::Duration) => true,
        _ => false,
    }
}

/// What a value that's one of `a` or `b` is, as the branches of `? :` or a list's
/// items: the kind they share, or `None` when they don't share one.
pub(super) fn join(a: &Ty, b: &Ty) -> Option<Ty> {
    match (a, b) {
        (Ty::Null, other) | (other, Ty::Null) => Some(other.clone()),
        (Ty::Dyn, _) | (_, Ty::Dyn) => Some(Ty::Dyn),
        (Ty::Str(first), Ty::Str(second)) => Some(Ty::Str(match (first, second) {
            (Some(first), Some(second)) if first == second => Some(Rc::clone(first)),
            _ => None,
        })),
        (Ty::List(first), Ty::List(second)) => Some(Ty::List(Box::new(join(first, second)?))),
        (Ty::Map(first), Ty::Map(second)) => Some(Ty::Map(Box::new(join(first, second)?))),
        (Ty::Object(first), Ty::Object(second)) if first == second => Some(a.clone()),
        (Ty::Bool, Ty::Bool)
        | (Ty::Int, Ty::Int)
        | (Ty::UInt, Ty::UInt)
        | (Ty::Double, Ty::Double)
        | (Ty::Timestamp, Ty::Timestamp)
        | (Ty::Duration, Ty::Duration) => Some(a.clone()),
        _ => None,
    }
}

/// How many edits (insertions, deletions, substitutions) turn `a` into `b`.
fn distance(a: &str, b: &str) -> usize {
    let b: Vec<char> = b.chars().collect();
    let mut previous: Vec<usize> = (0..=b.len()).collect();
    let mut current = vec![0; b.len() + 1];
    for (i, ca) in a.chars().enumerate() {
        current[0] = i + 1;
        for (j, cb) in b.iter().enumerate() {
            let substitution = previous[j] + usize::from(ca != *cb);
            current[j + 1] = substitution.min(previous[j + 1] + 1).min(current[j] + 1);
        }
        std::mem::swap(&mut previous, &mut current);
    }
    previous[b.len()]
}

/// The candidate `word` was most likely meant to be, if one is close: a word of up to
/// three characters may be one edit off, up to seven two, and longer ones three.
/// Case doesn't count.
pub(super) fn closest<'a>(
    word: &str,
    candidates: impl IntoIterator<Item = &'a str>,
) -> Option<&'a str> {
    let limit = match word.chars().count() {
        0..=3 => 1,
        4..=7 => 2,
        _ => 3,
    };
    let word = word.to_lowercase();
    candidates
        .into_iter()
        .map(|candidate| (distance(&word, &candidate.to_lowercase()), candidate))
        .filter(|&(d, _)| d <= limit)
        .min_by_key(|&(d, _)| d)
        .map(|(_, candidate)| candidate)
}

/// The most a list in a message names.
const LISTED: usize = 8;

/// Names, quoted and separated by commas, the first few of a long list: "`a`, `b`, …".
pub(super) fn listed<'a>(names: impl IntoIterator<Item = &'a str>) -> String {
    let mut names = names.into_iter();
    let mut out: Vec<String> = names
        .by_ref()
        .take(LISTED)
        .map(|n| format!("`{}`", short(n)))
        .collect();
    if names.next().is_some() {
        out.push("…".to_owned());
    }
    out.join(", ")
}

/// A name or a literal as a message quotes it: its first 40 characters.
pub(super) fn short(text: &str) -> String {
    let mut chars = text.chars();
    let start: String = chars.by_ref().take(40).collect();
    if chars.next().is_some() {
        format!("{start}…")
    } else {
        start
    }
}

/// `text` with a capital first letter, to start a sentence with.
pub(super) fn capitalized(text: &str) -> String {
    let mut chars = text.chars();
    chars.next().map_or_else(String::new, |first| {
        first.to_uppercase().chain(chars).collect()
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_the_closest_name() {
        assert_eq!(distance("purty", "purity"), 1);
        assert_eq!(distance("", "abc"), 3);
        assert_eq!(distance("kitten", "sitting"), 3);
        assert_eq!(closest("purty", ["purity", "status"]), Some("purity"));
        assert_eq!(closest("Purity", ["purity"]), Some("purity"));
        assert_eq!(closest("zzz", ["purity", "status"]), None);
        // The first of equally close ones.
        assert_eq!(closest("ab", ["ac", "ad"]), Some("ac"));
    }

    #[test]
    fn lists_the_first_few_names() {
        assert_eq!(listed(["a", "b"]), "`a`, `b`");
        let many: Vec<String> = (0..10).map(|i| i.to_string()).collect();
        assert_eq!(
            listed(many.iter().map(String::as_str)),
            "`0`, `1`, `2`, `3`, `4`, `5`, `6`, `7`, …"
        );
        assert_eq!(capitalized("text (string)"), "Text (string)");
        assert_eq!(short(&"x".repeat(41)).chars().count(), 41);
    }
}
