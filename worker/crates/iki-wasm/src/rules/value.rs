//! A rule's values: JSON read as CEL by each variable's schema, values written as text
//! for messages, and whether a failure came from an empty value.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt::Write as _;
use std::sync::Arc;

use super::types::Ty;
use crate::expressions;

/// cel's own readings of times and durations, so that what a rule reads
/// from JSON, and the literals the checker accepts, are exactly what cel accepts.
pub(super) struct Probes {
    env: Arc<cel::Env>,
    timestamp: Option<cel::Program>,
    duration: Option<cel::Program>,
}

impl Probes {
    /// The probes, in `env`.
    pub(super) fn new(env: Arc<cel::Env>) -> Self {
        let compile = |source: &str| cel::Program::compile(source).ok();
        Self {
            env,
            timestamp: compile("timestamp(text)"),
            duration: compile("duration(text)"),
        }
    }

    /// The environment rules run in: CEL's standard library.
    pub(super) fn env(&self) -> &Arc<cel::Env> {
        &self.env
    }

    fn run(&self, program: Option<&cel::Program>, text: &str) -> Option<cel::Value> {
        let mut context = cel::Context::with_env(Arc::clone(&self.env));
        context.add_variable_from_value("text", text.to_owned());
        program?.execute(&context).ok()
    }

    /// The time `text` is, as `timestamp(text)` reads it (RFC 3339), if it is one.
    pub(super) fn timestamp(&self, text: &str) -> Option<cel::Value> {
        self.run(self.timestamp.as_ref(), text)
    }

    /// The duration `text` is, as `duration(text)` reads it (`1h30m`), if it is one.
    pub(super) fn duration(&self, text: &str) -> Option<cel::Value> {
        self.run(self.duration.as_ref(), text)
    }
}

/// A whole number in a double, as JSON may write one (`5.0`).
#[allow(clippy::cast_possible_truncation, clippy::cast_precision_loss)]
fn whole(number: f64) -> Option<i64> {
    // i64::MAX as f64 rounds up to 2^63, which is just out of range.
    let limit = i64::MAX as f64;
    (number.fract() == 0.0 && number >= -limit && number < limit).then_some(number as i64)
}

/// JSON as a CEL value of the kind `ty` declares: a number is a double when the schema
/// says double, even when it's written `95`, and a time is read from RFC 3339. What
/// doesn't fit its schema is null, as an empty value is.
pub(super) fn value(json: &serde_json::Value, ty: &Ty, probes: &Probes) -> cel::Value {
    use serde_json::Value as J;
    match (ty, json) {
        (Ty::Dyn, json) => expressions::value(json),
        (Ty::Bool, J::Bool(b)) => cel::Value::Bool(*b),
        (Ty::Int, J::Number(n)) => n
            .as_i64()
            .or_else(|| n.as_f64().and_then(whole))
            .map_or(cel::Value::Null, cel::Value::Int),
        (Ty::UInt, J::Number(n)) => n
            .as_u64()
            .or_else(|| {
                n.as_f64()
                    .and_then(whole)
                    .and_then(|i| u64::try_from(i).ok())
            })
            .map_or(cel::Value::Null, cel::Value::UInt),
        (Ty::Double, J::Number(n)) => n.as_f64().map_or(cel::Value::Null, cel::Value::Float),
        (Ty::Str(_), J::String(s)) => cel::Value::String(Arc::new(s.clone())),
        (Ty::Timestamp, J::String(s)) => probes.timestamp(s).unwrap_or(cel::Value::Null),
        (Ty::Duration, J::String(s)) => probes.duration(s).unwrap_or(cel::Value::Null),
        (Ty::List(item), J::Array(items)) => cel::Value::List(Arc::new(
            items.iter().map(|i| value(i, item, probes)).collect(),
        )),
        (Ty::Map(values), J::Object(entries)) => entries
            .iter()
            .map(|(k, v)| (k.clone(), value(v, values, probes)))
            .collect::<HashMap<String, cel::Value>>()
            .into(),
        (Ty::Object(object), J::Object(entries)) => object
            .fields
            .iter()
            .map(|(field, ty)| {
                let json = entries.get(field).unwrap_or(&J::Null);
                (field.clone(), value(json, ty, probes))
            })
            .collect::<HashMap<String, cel::Value>>()
            .into(),
        _ => cel::Value::Null,
    }
}

/// The field `key` of a map, if it has one.
fn get<'a>(map: &'a cel::objects::Map, key: &str) -> Option<&'a cel::Value> {
    map.map.get(&cel::objects::Key::from(key.to_owned()))
}

/// Whether any of `paths` (a variable, then fields of it) is null or missing among
/// `values`: an empty value a rule read, which is why it failed, if it did.
pub(super) fn empty(paths: &BTreeSet<Vec<String>>, values: &BTreeMap<String, cel::Value>) -> bool {
    paths.iter().any(|path| {
        let Some((root, fields)) = path.split_first() else {
            return false;
        };
        let mut at = values.get(root);
        for field in fields {
            at = match at {
                Some(cel::Value::Map(map)) => get(map, field),
                // A field of something that isn't a map fails for another reason.
                Some(cel::Value::Null) | None => None,
                Some(_) => return false,
            };
        }
        matches!(at, None | Some(cel::Value::Null))
    })
}

/// A time as RFC 3339, in UTC, as `string(t)` writes it: `2026-11-18T10:00:00Z`, with
/// a fraction of a second only when it has one. `base` is it to the second. Midnight
/// exactly is a date alone, `2026-11-01`: a date field reaches CEL as midnight UTC.
fn time(base: &str, nanos: u32) -> String {
    let nanos = nanos % 1_000_000_000;
    if nanos == 0
        && let Some(date) = base.strip_suffix("T00:00:00")
    {
        date.to_owned()
    } else if nanos == 0 {
        format!("{base}Z")
    } else {
        let fraction = format!("{nanos:09}");
        format!("{base}.{}Z", fraction.trim_end_matches('0'))
    }
}

/// A duration of `seconds` and `nanos` (both negative for a negative one) as a person
/// writes one, and as `duration(…)` reads it: `1h30m`, `45s`, `1.5s`, `0s`.
fn length(seconds: i64, nanos: i32) -> String {
    let total = i128::from(seconds) * 1_000_000_000 + i128::from(nanos);
    if total == 0 {
        return "0s".to_owned();
    }
    let magnitude = total.unsigned_abs();
    let (seconds, nanos) = (magnitude / 1_000_000_000, magnitude % 1_000_000_000);
    let (hours, minutes, seconds) = (seconds / 3600, seconds % 3600 / 60, seconds % 60);
    let mut out = String::new();
    if total < 0 {
        out.push('-');
    }
    if hours > 0 {
        let _ = write!(out, "{hours}h");
    }
    if minutes > 0 {
        let _ = write!(out, "{minutes}m");
    }
    if nanos > 0 {
        let fraction = format!("{nanos:09}");
        let _ = write!(out, "{seconds}.{}s", fraction.trim_end_matches('0'));
    } else if seconds > 0 {
        let _ = write!(out, "{seconds}s");
    }
    out
}

/// A value as a message writes it: text as it is, a number as it's written (`95`, not
/// `95.0`), yes or no, a time in RFC 3339 (a date alone at midnight UTC, as a date field
/// is) and a duration as `1h30m`, a list's items with
/// commas between them, and a record by its HRID.
pub(super) fn text(value: &cel::Value) -> String {
    match value {
        cel::Value::Bool(true) => "Yes".to_owned(),
        cel::Value::Bool(false) => "No".to_owned(),
        cel::Value::Int(i) => i.to_string(),
        cel::Value::UInt(u) => u.to_string(),
        cel::Value::Float(f) => f.to_string(),
        cel::Value::String(s) => s.as_str().to_owned(),
        cel::Value::Timestamp(t) => time(
            &t.naive_utc().format("%Y-%m-%dT%H:%M:%S").to_string(),
            t.timestamp_subsec_nanos(),
        ),
        cel::Value::Duration(d) => length(d.num_seconds(), d.subsec_nanos()),
        cel::Value::List(items) => items.iter().map(text).collect::<Vec<_>>().join(", "),
        cel::Value::Map(map) => match get(map, "hrid") {
            Some(cel::Value::String(hrid)) => hrid.as_str().to_owned(),
            _ => value.json().map(|j| j.to_string()).unwrap_or_default(),
        },
        _ => String::new(),
    }
}

/// A computed field's value as JSON its field reads, from what its expression gave: a
/// number where it holds numbers (a whole one where it holds whole numbers), text, yes
/// or no, or a time in RFC 3339, in UTC; null where it's empty. Or why it isn't one: a
/// number that isn't finite, as a division by zero gives, or text that isn't one of an
/// enum's values (ADR 0297).
pub(super) fn computed(value: &cel::Value, wanted: &Ty) -> Result<serde_json::Value, String> {
    use cel::Value as V;
    use serde_json::Value as J;
    let number = |n: f64| {
        serde_json::Number::from_f64(n)
            .map(J::Number)
            .ok_or_else(|| "It isn't a finite number: a division by zero gives none.".to_owned())
    };
    match (wanted, value) {
        (_, V::Null) => Ok(J::Null),
        (Ty::Double, V::Float(f)) => number(*f),
        #[allow(clippy::cast_precision_loss)]
        (Ty::Double, V::Int(i)) => number(*i as f64),
        #[allow(clippy::cast_precision_loss)]
        (Ty::Double, V::UInt(u)) => number(*u as f64),
        (Ty::Int, V::Int(i)) => Ok(J::from(*i)),
        (Ty::Int, V::UInt(u)) => i64::try_from(*u)
            .map(J::from)
            .map_err(|_| "It's too large a whole number.".to_owned()),
        (Ty::Int, V::Float(f)) => whole(*f).map(J::from).ok_or_else(|| {
            "It's a number with a fraction, where a whole number is wanted.".to_owned()
        }),
        (Ty::Str(choices), V::String(s)) => match choices {
            Some(choices) if !choices.values.iter().any(|v| v == s.as_str()) => Err(format!(
                "`{}` isn't one of {}'s values.",
                super::types::short(s),
                choices.label
            )),
            _ => Ok(J::String(s.as_str().to_owned())),
        },
        (Ty::Bool, V::Bool(b)) => Ok(J::Bool(*b)),
        (Ty::Timestamp, V::Timestamp(t)) => Ok(J::String(
            t.naive_utc().format("%Y-%m-%dT%H:%M:%S%.fZ").to_string(),
        )),
        (wanted, other) => Err(format!(
            "It's {} where {} is wanted.",
            expressions::kind(other),
            wanted.describe()
        )),
    }
}
