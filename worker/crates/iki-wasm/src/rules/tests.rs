use std::collections::BTreeMap;
use std::sync::Arc;

use serde_json::{Value, json};

use super::*;

/// A sample record and a guard's variables, as a LIMS plugin declares them.
fn lims() -> Value {
    let change = json!({"type": "object", "label": "Change", "fields": {
        "from": {"type": "string"},
        "to": {"type": "string"},
        "by": {"type": "string"},
        "at": {"type": "timestamp"},
        "reason": {"type": "string"},
    }});
    json!({
        "record": {"type": "object", "label": "Sample", "fields": {
            "hrid": {"type": "string"},
            "purity": {"type": "double"},
            "count": {"type": "int"},
            "status": {"type": "string", "label": "Status", "values":
                ["registered", "received", "in_review", "approved", "rejected"]},
            "instrument": {"type": "object", "label": "Instrument", "fields": {
                "hrid": {"type": "string"},
                "service": {"type": "string", "label": "Service", "values":
                    ["in_service", "out_of_service", "retired"]},
            }},
            "received_at": {"type": "timestamp"},
            "starts_at": {"type": "timestamp"},
            "ends_at": {"type": "timestamp"},
            "tags": {"type": "list", "items": {"type": "string"}},
            "values": {"type": "map", "values": {"type": "dyn"}},
        }},
        "actor": {"type": "object", "label": "Actor", "fields": {
            "person": {"type": "string"},
            "agent": {"type": "object", "label": "Agent", "fields": {
                "name": {"type": "string"},
                "client_id": {"type": "string"},
            }},
            "roles": {"type": "list", "items": {"type": "string"}},
        }},
        "history": {"type": "object", "label": "History", "fields": {
            "status": {"type": "list", "items": change},
        }},
        "last": {"type": "object", "label": "Last changes", "fields": {
            "status": {"type": "object", "label": "Status changes", "fields": {
                "in_review": change,
            }},
        }},
        "now": {"type": "timestamp"},
    })
}

/// A sample as saved, and who is saving it.
fn sample() -> Value {
    json!({
        "record": {
            "hrid": "SAM-000001",
            "purity": 92.5,
            "count": 3,
            "status": "in_review",
            "instrument": {"hrid": "INS-000001", "service": "in_service"},
            "received_at": "2026-11-18T10:00:00Z",
            "starts_at": "2026-11-18T09:00:00Z",
            "ends_at": "2026-11-18T10:30:00Z",
            "tags": ["urgent", "batch-7"],
            "values": {"ph": 7.2},
        },
        "actor": {"person": "ben", "roles": ["reviewer"]},
        "history": {"status": [
            {"from": "received", "to": "in_review", "by": "ana", "at": "2026-11-18T10:05:00Z"},
        ]},
        "last": {"status": {"in_review": {"by": "ana", "at": "2026-11-18T10:05:00Z"}}},
        "now": "2026-11-18T11:00:00Z",
    })
}

fn variables(schemas: &Value) -> BTreeMap<String, Schema> {
    serde_json::from_value(schemas.clone()).unwrap()
}

fn checked_with(schemas: &Value, source: &str, kind: Kind) -> RuleChecked {
    check(&CheckRequest {
        variables: variables(schemas),
        expressions: vec![Expression {
            source: source.to_owned(),
            kind,
        }],
    })
    .remove(0)
}

fn condition(source: &str) -> RuleChecked {
    checked_with(&lims(), source, Kind::Condition)
}

fn reads(source: &str) -> BTreeMap<String, Vec<String>> {
    match condition(source) {
        RuleChecked::Valid { reads } => reads,
        invalid @ RuleChecked::Invalid { .. } => unreachable!("{source}: {invalid:?}"),
    }
}

/// Why a condition doesn't check: its error, hint, line and column.
fn refused_with(
    schemas: &Value,
    source: &str,
    kind: Kind,
) -> (String, Option<String>, Option<usize>, Option<usize>) {
    match checked_with(schemas, source, kind) {
        RuleChecked::Invalid {
            error,
            hint,
            line,
            column,
        } => (error, hint, line, column),
        valid @ RuleChecked::Valid { .. } => unreachable!("{source} checked: {valid:?}"),
    }
}

fn refused(source: &str) -> (String, Option<String>, Option<usize>, Option<usize>) {
    refused_with(&lims(), source, Kind::Condition)
}

fn evaluated_with(
    schemas: &Value,
    expressions: &[(&str, Kind)],
    contexts: &[Value],
) -> Vec<Vec<RuleEvaluated>> {
    evaluate(&EvaluateRequest {
        variables: variables(schemas),
        expressions: expressions
            .iter()
            .map(|(source, kind)| Expression {
                source: (*source).to_owned(),
                kind: *kind,
            })
            .collect(),
        contexts: contexts
            .iter()
            .map(|c| c.as_object().cloned().unwrap())
            .collect(),
    })
}

/// A condition's outcome with the sample, changed by `change`.
fn holds(source: &str, change: impl FnOnce(&mut Value)) -> RuleEvaluated {
    let mut context = sample();
    change(&mut context);
    evaluated_with(&lims(), &[(source, Kind::Condition)], &[context])
        .remove(0)
        .remove(0)
}

fn yes() -> RuleEvaluated {
    RuleEvaluated::Value { value: true }
}

fn no() -> RuleEvaluated {
    RuleEvaluated::Value { value: false }
}

#[test]
fn four_eyes_needs_someone_other_than_the_reviewer() {
    let rule = "last.status.in_review.by != actor.person";
    assert_eq!(
        reads(rule),
        BTreeMap::from([
            ("actor".to_owned(), vec!["person".to_owned()]),
            ("last".to_owned(), vec!["status".to_owned()]),
        ])
    );
    assert_eq!(holds(rule, |_| {}), yes());
    assert_eq!(holds(rule, |c| c["actor"]["person"] = json!("ana")), no());
    // Never reviewed: the change it reads is empty, which a guard takes as failing.
    assert!(matches!(
        holds(rule, |c| c["last"]["status"]["in_review"] = Value::Null),
        RuleEvaluated::Error { empty: true, .. }
    ));
}

#[test]
fn states_and_services_are_checked_against_their_values() {
    assert_eq!(holds("record.status == 'in_review'", |_| {}), yes());
    assert_eq!(
        reads("record.instrument.service == 'in_service'"),
        BTreeMap::from([("record".to_owned(), vec!["instrument".to_owned()])])
    );
    assert_eq!(
        holds("record.instrument.service == 'in_service'", |_| {}),
        yes()
    );
    assert_eq!(
        refused("record.status == 'aproved'"),
        (
            "`aproved` isn't one of Status's values.".to_owned(),
            Some(
                "Did you mean `approved`? It's one of `registered`, `received`, `in_review`, `approved`, `rejected`."
                    .to_owned()
            ),
            Some(1),
            Some(18),
        )
    );
    // Either way round, and in a list.
    assert!(
        refused("'aproved' == record.status")
            .0
            .contains("`aproved`")
    );
    assert!(
        refused("record.status != 'aproved'")
            .0
            .contains("`aproved`")
    );
    let (error, hint, ..) = refused("record.status in ['approved', 'rejectd']");
    assert_eq!(error, "`rejectd` isn't one of Status's values.");
    assert!(hint.unwrap().starts_with("Did you mean `rejected`?"));
    let (error, hint, ..) = refused("record.instrument.service == 'broken'");
    assert_eq!(error, "`broken` isn't one of Service's values.");
    assert_eq!(
        hint.as_deref(),
        Some("It's one of `in_service`, `out_of_service`, `retired`.")
    );
    assert_eq!(
        holds("record.status in ['approved', 'rejected']", |_| {}),
        no()
    );
}

#[test]
fn times_and_durations_compare_and_subtract() {
    assert_eq!(holds("record.ends_at > record.starts_at", |_| {}), yes());
    assert_eq!(
        holds(
            "record.ends_at - record.starts_at <= duration('8h')",
            |_| {}
        ),
        yes()
    );
    assert_eq!(
        holds(
            "record.ends_at - record.starts_at <= duration('1h')",
            |_| {}
        ),
        no()
    );
    assert_eq!(
        holds("now - record.received_at < duration('2h')", |_| {}),
        yes()
    );
    assert_eq!(
        holds("record.received_at + duration('30m') < now", |_| {}),
        yes()
    );
    assert_eq!(holds("record.received_at.getHours() == 10", |_| {}), yes());
    assert_eq!(
        reads("now - record.received_at < duration('2h')"),
        BTreeMap::from([
            ("now".to_owned(), vec![]),
            ("record".to_owned(), vec!["received_at".to_owned()]),
        ])
    );
    let (error, hint, ..) = refused("record.received_at > '2026-11-18'");
    assert_eq!(
        error,
        "`>` can't compare a time (timestamp) with text (string)."
    );
    assert_eq!(
        hint.as_deref(),
        Some("Write the time as `timestamp('2026-11-18T10:00:00Z')`.")
    );
    assert_eq!(
        refused("record.received_at > timestamp('2026-13-01')").0,
        "`2026-13-01` isn't a time."
    );
    assert_eq!(
        refused("record.ends_at - record.starts_at < duration('8 hours')").0,
        "`8 hours` isn't a duration."
    );
    assert_eq!(
        refused("now.getHours('Europe/London') > 9"),
        (
            "`getHours()` takes no time zone: rules' times are in UTC.".to_owned(),
            Some("Write `getHours()`, which reads the time in UTC.".to_owned()),
            Some(1),
            Some(14),
        )
    );
    assert_eq!(
        refused("record.starts_at - record.ends_at.getHours() > 0").0,
        "`-` doesn't subtract a whole number (int) from a time (timestamp)."
    );
    let (error, hint, ..) = refused("record.starts_at + record.ends_at > now");
    assert_eq!(
        error,
        "`+` doesn't add a time (timestamp) and a time (timestamp)."
    );
    assert!(hint.unwrap().contains("duration('8h')"));
}

#[test]
fn says_what_a_typo_was_likely_meant_to_be() {
    assert_eq!(
        refused("record.purty >= 95.0"),
        (
            "Sample has no field `purty`.".to_owned(),
            Some("Did you mean `purity`?".to_owned()),
            Some(1),
            Some(8),
        )
    );
    assert_eq!(
        refused("recod.purity >= 95.0"),
        (
            "There's nothing called `recod`.".to_owned(),
            Some("Did you mean `record`?".to_owned()),
            Some(1),
            Some(1),
        )
    );
    let (error, hint, ..) = refused("record.instrument.servce == 'retired'");
    assert_eq!(error, "Instrument has no field `servce`.");
    assert_eq!(hint.as_deref(), Some("Did you mean `service`?"));
    let (error, hint, ..) = refused("record.zzz == 1");
    assert_eq!(error, "Sample has no field `zzz`.");
    assert!(
        hint.unwrap()
            .starts_with("Its fields are `count`, `ends_at`")
    );
    assert_eq!(
        refused("sise(record.tags) > 0").1.as_deref(),
        Some("Did you mean `size()`?")
    );
    assert_eq!(
        refused("record.tags.exsts(t, t == 'urgent')"),
        (
            "A list has no method `exsts()`.".to_owned(),
            Some("Did you mean `exists()`?".to_owned()),
            Some(1),
            Some(18),
        )
    );
    assert_eq!(
        refused("record.hrid.startswith('SAM')").1.as_deref(),
        Some("Did you mean `startsWith()`?")
    );
    assert_eq!(
        refused("history.status.all(c, c.too != 'approved')").0,
        "Change has no field `too`."
    );
    let (error, hint, ..) = refused("nobody");
    assert_eq!(error, "There's nothing called `nobody`.");
    assert_eq!(
        hint.as_deref(),
        Some("It can read `actor`, `history`, `last`, `now`, `record`.")
    );
}

#[test]
fn doesnt_mix_whole_numbers_and_numbers_in_arithmetic() {
    assert_eq!(
        refused("record.purity + 1 > 2.0"),
        (
            "`+` doesn't add a number (double) and a whole number (int).".to_owned(),
            Some("Write `1.0` for a number, or convert with `double(…)`.".to_owned()),
            Some(1),
            Some(15),
        )
    );
    assert_eq!(
        refused("record.count * 2.5 > 1.0").1.as_deref(),
        Some("Write `1.0` for a number, or convert with `double(…)`.")
    );
    assert_eq!(
        refused("record.purity * 100 > 1.0").1.as_deref(),
        Some("Write `100.0` for a number, or convert with `double(…)`.")
    );
    assert_eq!(
        refused("record.count + 1u > 0").1.as_deref(),
        Some("Convert one with `int(…)` or `uint(…)`.")
    );
    assert_eq!(
        refused("1u + 2 > 0u").1.as_deref(),
        Some("Write `2u` for an unsigned whole number, or convert with `int(…)` or `uint(…)`.")
    );
    // Comparisons do mix them, as cel-rust compares an int with a double.
    assert_eq!(holds("record.purity >= 92", |_| {}), yes());
    assert_eq!(holds("record.count < 3.5", |_| {}), yes());
    assert_eq!(
        holds("double(record.count) * record.purity > 200.0", |_| {}),
        yes()
    );
    let (error, ..) = refused("record.purity % 2.0 > 0.0");
    assert_eq!(
        error,
        "`%` works on whole numbers only, and this has a number (double)."
    );
}

#[test]
fn compares_anything_with_null() {
    assert_eq!(holds("record.purity != null", |_| {}), yes());
    assert_eq!(holds("null == record.instrument", |_| {}), no());
    assert_eq!(
        holds("record.instrument == null", |c| c["record"]["instrument"] =
            Value::Null),
        yes()
    );
    assert_eq!(
        holds(
            "record.purity == null || record.purity >= 95.0",
            |c| c["record"]["purity"] = Value::Null
        ),
        yes()
    );
    assert_eq!(
        refused("record.purity == 'high'").0,
        "`==` compares a number (double) with text (string), which are never equal."
    );
    assert_eq!(
        refused("record.instrument == 'INS-000001'").1.as_deref(),
        Some("Compare its HRID: `record.instrument.hrid`.")
    );
    assert_eq!(
        refused("null < 1").1.as_deref(),
        Some("`null` is only equal or unequal to things: use `==` or `!=`.")
    );
}

#[test]
fn a_failure_says_whether_an_empty_value_caused_it() {
    let empty = |c: &mut Value| c["record"]["purity"] = Value::Null;
    let RuleEvaluated::Error { error, empty: true } = holds("record.purity >= 95.0", empty) else {
        unreachable!("it held")
    };
    assert_eq!(error, "`>=` doesn't work on an empty value and a number.");
    // A field left out is empty too.
    let left_out = |c: &mut Value| {
        c["record"].as_object_mut().unwrap().remove("purity");
    };
    assert!(matches!(
        holds("record.purity >= 95.0", left_out),
        RuleEvaluated::Error { empty: true, .. }
    ));
    // As is a variable, or what's under an empty field.
    assert!(matches!(
        holds("now > record.received_at", |c| c["now"] = Value::Null),
        RuleEvaluated::Error { empty: true, .. }
    ));
    assert!(matches!(
        holds("record.instrument.service == 'in_service'", |c| c["record"]
            ["instrument"] =
            Value::Null),
        RuleEvaluated::Error { empty: true, .. }
    ));
    // A value that doesn't fit its schema is empty.
    assert!(matches!(
        holds("record.purity >= 95.0", |c| c["record"]["purity"] =
            json!("high")),
        RuleEvaluated::Error { empty: true, .. }
    ));
    // A map's missing key is.
    assert!(matches!(
        holds("record.values.conductivity > 1", |_| {}),
        RuleEvaluated::Error { empty: true, .. }
    ));
    // Dividing by zero isn't.
    assert_eq!(
        holds("record.count / 0 == 1", |_| {}),
        RuleEvaluated::Error {
            error: "It divides by zero.".to_owned(),
            empty: false
        }
    );
    // An empty value that `||` doesn't need doesn't fail it.
    assert_eq!(holds("true || record.purity > 1.0", empty), yes());
    // A comprehension's own variable isn't what's empty.
    assert!(matches!(
        holds(
            "record.tags.all(t, t.size() > 0)",
            |c| c["record"]["tags"] = json!([null])
        ),
        RuleEvaluated::Error { empty: false, .. }
    ));
    // A non-bool outcome is an error.
    assert_eq!(
        holds("record.values.ph", |_| {}),
        RuleEvaluated::Error {
            error: "It's a number where yes or no is wanted.".to_owned(),
            empty: false
        }
    );
}

#[test]
fn errors_never_show_values() {
    let context = json!({"record": {"values": {"secret": "confidential sequence"}}});
    let out = evaluated_with(
        &lims(),
        &[
            ("record.values.secret > 1", Kind::Condition),
            ("record.values.secret * 2 > 1", Kind::Condition),
            ("{{ record.values.secret + 1 }}", Kind::Message),
        ],
        &[context],
    );
    for outcome in &out[0] {
        let RuleEvaluated::Error { error, .. } = outcome else {
            unreachable!("{outcome:?}")
        };
        assert!(!error.contains("confidential"), "{error}");
    }
}

#[test]
fn messages_write_values_as_text() {
    let message = "Purity {{ record.purity }} is below 95 on {{ record.instrument }} at \
        {{ record.received_at }} ({{ record.tags }}) after {{ record.ends_at - record.starts_at }}: \
        {{ record.status == 'approved' }}, {{ record.count }}{{ record.purity == null }}.";
    let out = evaluated_with(&lims(), &[(message, Kind::Message)], &[sample()]);
    assert_eq!(
        out[0][0],
        RuleEvaluated::Text {
            text:
                "Purity 92.5 is below 95 on INS-000001 at 2026-11-18T10:00:00Z (urgent, batch-7) \
                after 1h30m: No, 3No."
                    .to_owned()
        }
    );
    // A whole number in a double has no `.0`; an empty value is nothing.
    let mut context = sample();
    context["record"]["purity"] = json!(95);
    context["record"]["hrid"] = Value::Null;
    let out = evaluated_with(
        &lims(),
        &[
            ("{{ record.purity }}%, [{{ record.hrid }}]", Kind::Message),
            ("No expressions.", Kind::Message),
            (
                "{{ record.ends_at - record.starts_at + duration('10.5s') }}",
                Kind::Message,
            ),
            (
                "{{ timestamp('2026-11-18T10:00:00.250+01:00') }}",
                Kind::Message,
            ),
            (
                "{{ record.starts_at - record.ends_at }} {{ duration('0s') }}",
                Kind::Message,
            ),
            ("{{ record.values }}", Kind::Message),
        ],
        &[context],
    );
    let texts: Vec<String> = out[0]
        .iter()
        .map(|o| match o {
            RuleEvaluated::Text { text } => text.clone(),
            other => unreachable!("{other:?}"),
        })
        .collect();
    assert_eq!(
        texts,
        [
            "95%, []",
            "No expressions.",
            "1h30m10.5s",
            "2026-11-18T09:00:00.25Z",
            "-1h30m 0s",
            r#"{"ph":7.2}"#,
        ]
    );
    let out = evaluated_with(
        &lims(),
        &[(
            "Purity {{ record.purity * 2.0 }} of {{ record.values.missing }}",
            Kind::Message,
        )],
        &[sample()],
    );
    assert_eq!(
        out[0][0],
        RuleEvaluated::Error {
            error: "In `{{ record.values.missing }}`: There's no `missing`.".to_owned(),
            empty: true
        }
    );
}

#[test]
fn getters_take_no_time_zone() {
    let (schemas, _) = kinds();
    for source in [
        "t.getFullYear('UTC')",
        "t.getMonth('Europe/London')",
        "t.getDayOfYear('+05:30')",
        "t.getDayOfMonth(s)",
        "t.getDate('UTC')",
        "t.getDayOfWeek('UTC')",
        "t.getHours('UTC')",
        "t.getMinutes('UTC')",
        "t.getSeconds('UTC')",
        "t.getMilliseconds('UTC')",
        "xn.getHours('UTC')",
    ] {
        let (error, ..) = refused_with(&schemas, &format!("{{{{ {source} }}}}"), Kind::Message);
        assert!(error.contains("takes no time zone"), "{source}: {error}");
    }
}

#[test]
fn a_time_at_midnight_utc_is_written_as_a_date() {
    let schemas = json!({"expires_on": {"type": "timestamp"}});
    let out = evaluated_with(
        &schemas,
        &[
            ("Expires {{ expires_on }}", Kind::Message),
            ("{{ expires_on + duration('1s') }}", Kind::Message),
            ("{{ expires_on + duration('1ms') }}", Kind::Message),
            (
                "{{ timestamp('2026-11-01T01:00:00+01:00') }}",
                Kind::Message,
            ),
        ],
        &[json!({"expires_on": "2026-11-01T00:00:00Z"})],
    );
    let texts: Vec<&str> = out[0]
        .iter()
        .map(|o| match o {
            RuleEvaluated::Text { text } => text.as_str(),
            other => unreachable!("{other:?}"),
        })
        .collect();
    assert_eq!(
        texts,
        [
            "Expires 2026-11-01",
            "2026-11-01T00:00:01Z",
            "2026-11-01T00:00:00.001Z",
            // Midnight UTC, written with an offset, is the date too.
            "2026-11-01",
        ]
    );
}

#[test]
fn messages_check_each_expression_where_it_is() {
    assert_eq!(
        refused_with(&lims(), "Bad: {{ record.purty }}", Kind::Message),
        (
            "In `{{ record.purty }}`: Sample has no field `purty`.".to_owned(),
            Some("Did you mean `purity`?".to_owned()),
            Some(1),
            Some(16),
        )
    );
    assert_eq!(
        refused_with(&lims(), "a\n{{ 1 + }}", Kind::Message),
        (
            "In `{{ 1 + }}`: It ends too soon, at line 2, column 8.".to_owned(),
            None,
            Some(2),
            Some(8),
        )
    );
    assert_eq!(
        refused_with(&lims(), "ab {{ x", Kind::Message).0,
        "The `{{` at character 4 isn't closed."
    );
    let RuleChecked::Valid { reads } = checked_with(
        &lims(),
        "{{ record.purity }} by {{ actor.person }}: {{ record }}",
        Kind::Message,
    ) else {
        unreachable!()
    };
    // The whole record is read, so no field in particular.
    assert_eq!(
        reads,
        BTreeMap::from([
            ("actor".to_owned(), vec!["person".to_owned()]),
            ("record".to_owned(), vec![]),
        ])
    );
}

#[test]
fn says_why_it_doesnt_read_as_the_parser_does() {
    assert_eq!(
        refused("record.purity >="),
        (
            "It ends too soon, at line 1, column 17.".to_owned(),
            None,
            Some(1),
            Some(17)
        )
    );
    assert_eq!(
        refused("record.purity\n  >= )"),
        (
            "It doesn't expect ')' here, at line 2, column 6.".to_owned(),
            None,
            Some(2),
            Some(6)
        )
    );
    assert_eq!(refused("  ").0, "It's empty.");
    assert_eq!(
        refused(&"a".repeat(MAX_LENGTH + 1)).0,
        "It's over 1000 characters, the most an expression may be."
    );
    let deep = std::thread::Builder::new()
        .stack_size(64 << 20)
        .spawn(|| refused(&format!("{}true{}", "(".repeat(40), ")".repeat(40))).0)
        .map(std::thread::JoinHandle::join);
    let Ok(Ok(deep)) = deep else {
        unreachable!("the check stopped")
    };
    assert_eq!(deep, "It nests more than 32 deep.");
    let (error, hint, line, column) = refused("record.?purity > 1.0");
    assert_eq!(
        error,
        "Optional values (`.?` and `[?`) aren't supported in rules."
    );
    assert!(hint.is_some());
    assert_eq!((line, column), (Some(1), Some(7)));
    assert!(
        refused("record.values[?'ph'] > 1")
            .0
            .starts_with("Optional values")
    );
    assert_eq!(
        refused("record.tags.all(t.x, true)").0,
        "A macro's variable must be a simple name, as `x` in `items.all(x, x > 0)`, at line 1, column 18."
    );
}

#[test]
fn says_what_isnt_available() {
    let (error, hint, ..) = refused("record.hrid.matches('^SAM-')");
    assert_eq!(
        error,
        "`matches()` isn't available: rules have no regular expressions."
    );
    assert_eq!(
        hint.as_deref(),
        Some("A string field's `pattern` checks its format.")
    );
    assert!(
        refused("matches(record.hrid, '^SAM-')")
            .0
            .starts_with("`matches()`")
    );
    assert_eq!(
        refused("has(record.purity)").1.as_deref(),
        Some("Write `record.purity != null` to check it has a value.")
    );
    assert_eq!(holds("has(record.values.ph)", |_| {}), yes());
    assert_eq!(holds("has(record.values.conductivity)", |_| {}), no());
    assert_eq!(
        refused("record.purity").0,
        "It works out to a number (double), but a condition must be yes or no."
    );
    assert_eq!(
        refused("b'abc' == b'abc'").0,
        "Bytes (`b'…'`) aren't supported in rules."
    );
    assert_eq!(
        refused("max(record.tags) == 'x'").0,
        "There's no function `max()`."
    );
    assert!(
        refused("all(record.tags)")
            .0
            .starts_with("`all` is written after")
    );
    assert!(
        refused("record.tags.all(t)")
            .0
            .starts_with("`all` takes a variable")
    );
}

#[test]
fn comprehensions_type_their_variable() {
    let rule = "history.status.exists(c, c.to == 'in_review' && c.by != actor.person)";
    assert_eq!(
        reads(rule),
        BTreeMap::from([
            ("actor".to_owned(), vec!["person".to_owned()]),
            ("history".to_owned(), vec!["status".to_owned()]),
        ])
    );
    assert_eq!(holds(rule, |_| {}), yes());
    assert_eq!(
        holds("record.tags.map(t, size(t)).all(n, n > 2)", |_| {}),
        yes()
    );
    assert_eq!(
        holds(
            "record.tags.filter(t, t.startsWith('b')) == ['batch-7']",
            |_| {}
        ),
        yes()
    );
    assert_eq!(
        holds("record.tags.map(t, t != 'urgent', size(t)) == [7]", |_| {}),
        yes()
    );
    assert_eq!(
        holds("record.tags.exists_one(t, t.endsWith('7'))", |_| {}),
        yes()
    );
    assert_eq!(holds("record.values.all(k, k == 'ph')", |_| {}), yes());
    assert_eq!(
        holds("history.status.all(c, now - c.at < duration('1h'))", |_| {}),
        yes()
    );
    assert_eq!(
        refused("record.tags.all(t, t + 1)").0,
        "`+` doesn't add text (string) and a whole number (int)."
    );
    assert_eq!(
        refused("record.tags.all(t, size(t))").0,
        "The condition in `all` must be yes or no, and this is a whole number (int)."
    );
    assert_eq!(
        refused("record.purity.exists(p, p > 1.0)").0,
        "`exists` goes through a list or a map, and this is a number (double)."
    );
    // Outside it, the variable is gone.
    assert_eq!(
        refused("record.tags.all(t, t != '') && t == 'x'").0,
        "There's nothing called `t`."
    );
}

#[test]
fn checks_the_rest_of_the_operators() {
    assert_eq!(
        holds("(record.purity > 90.0 ? 'high' : 'low') == 'high'", |_| {}),
        yes()
    );
    assert_eq!(
        refused("(record.purity > 90.0 ? 1 : 'low') == 1").0,
        "The two sides of `? :` differ: a whole number (int) and text (string)."
    );
    assert_eq!(holds("'urgent' in record.tags", |_| {}), yes());
    assert_eq!(holds("'ph' in record.values", |_| {}), yes());
    assert_eq!(
        refused("1 in record.tags").0,
        "`in` looks for a whole number (int) in a list of text (string), which never holds it."
    );
    assert_eq!(holds("record.tags[0] == 'urgent'", |_| {}), yes());
    assert_eq!(
        refused("record.tags['a'] == 'x'").0,
        "A list's items are numbered from 0: `[…]` takes a whole number, not text (string)."
    );
    assert_eq!(
        refused("record.hrid[0] == 'S'").0,
        "Text (string) can't be indexed with `[…]`."
    );
    assert_eq!(holds("record['purity'] > 90.0", |_| {}), yes());
    assert_eq!(
        refused("record['purty'] > 90.0").0,
        "Sample has no field `purty`."
    );
    assert_eq!(
        refused("record.purity.value > 1").0,
        "`.value` can't be read of a number (double)."
    );
    assert_eq!(
        refused("!record.purity").0,
        "`!` turns yes into no, and this is a number (double)."
    );
    assert_eq!(
        refused("record.purity && true").0,
        "`&&` joins yes-or-no values, and this is a number (double)."
    );
    assert_eq!(holds("-record.purity < 0.0", |_| {}), yes());
    assert_eq!(
        holds("{'a': record.count}.a == 3 && [1, 'x'][1] == 'x'", |_| {}),
        yes()
    );
    assert_eq!(
        refused("{1: 'a'}[1] == 'a'").0,
        "A map's keys must be text here, not a whole number (int)."
    );
    assert_eq!(
        holds("'SAM-' + string(record.count) == 'SAM-3'", |_| {}),
        yes()
    );
    assert_eq!(
        refused("'SAM-' + record.count == 'SAM-3'").1.as_deref(),
        Some("Convert it to text with `string(…)` first.")
    );
    assert_eq!(
        holds(
            "actor.roles.exists(r, r == 'reviewer') && actor.agent == null",
            |_| {}
        ),
        yes()
    );
}

/// Variables of every kind, for checking the checker against the runtime.
fn kinds() -> (Value, Value) {
    let schemas = json!({
        "b": {"type": "bool"},
        "i": {"type": "int"},
        "u": {"type": "uint"},
        "d": {"type": "double"},
        "s": {"type": "string"},
        "t": {"type": "timestamp"},
        "du": {"type": "duration"},
        "li": {"type": "list", "items": {"type": "int"}},
        "ls": {"type": "list", "items": {"type": "string"}},
        "m": {"type": "map", "values": {"type": "int"}},
        "o": {"type": "object", "label": "Thing", "fields": {
            "n": {"type": "int"}, "s": {"type": "string"},
        }},
        "xn": {"type": "dyn"},
        "xm": {"type": "dyn"},
    });
    let values = json!({
        "b": true, "i": 7, "u": 8, "d": 2.5, "s": "abc",
        "t": "2026-11-18T10:00:00Z", "du": "90m",
        "li": [1, 2, 3], "ls": ["a", "b"], "m": {"k": 1},
        "o": {"n": 1, "s": "x"},
        "xn": 5, "xm": {"y": 1, "l": [1]},
    });
    (schemas, values)
}

/// Every form the checker accepts, each of which cel-rust runs without an error.
const ACCEPTED: &[&str] = &[
    // Arithmetic, of each kind with its own.
    "i + i",
    "u + u",
    "d + d",
    "s + s",
    "li + li",
    "li + []",
    "t + du",
    "du + t",
    "du + du",
    "i - i",
    "u - u",
    "d - d",
    "t - t",
    "t - du",
    "du - du",
    "i * i",
    "u * u",
    "d * d",
    "i / i",
    "u / u",
    "d / d",
    "i % i",
    "u % u",
    "-i",
    "-d",
    "!b",
    "b && b",
    "b || b",
    "b ? i : i",
    "b ? null : i",
    "b ? li : []",
    // Equality, across numbers and with null.
    "i == u",
    "i == d",
    "u == d",
    "i != d",
    "s == s",
    "b == b",
    "t == t",
    "du == du",
    "li == li",
    "li == []",
    "m == m",
    "o == m",
    "o == o",
    "null == i",
    "s != null",
    "xn == 'a'",
    "xn == 5",
    "ls == ['a', 'b']",
    // Ordering, across numbers.
    "i < u",
    "i < d",
    "u < d",
    "d < i",
    "d <= u",
    "u > i",
    "u >= d",
    "s < s",
    "t < t",
    "du < du",
    "b < b",
    "xn < 6",
    "xn > d",
    // Membership and indexing.
    "i in li",
    "d in li",
    "s in ls",
    "'k' in m",
    "'n' in o",
    "xn in li",
    "'y' in xm",
    "li[0]",
    "li[1u]",
    "li[xn - 5]",
    "m['k']",
    "m[s + 'k'] == null || true",
    "m.k",
    "o.n",
    "o['n']",
    "o[s] == null || true",
    "xm.y",
    "xm['y']",
    "xm.l[0]",
    // Functions.
    "size(s)",
    "size(li)",
    "size(m)",
    "size(o)",
    "size(xm)",
    "s.size()",
    "li.size()",
    "m.size()",
    "o.size()",
    "xm.size()",
    "s.contains('a')",
    "s.startsWith('a')",
    "s.endsWith('c')",
    "s.contains(s)",
    "int(i)",
    "int(u)",
    "int(d)",
    "int('5')",
    "int(t)",
    "int(xn)",
    "uint(u)",
    "uint(i)",
    "uint(d)",
    "uint('5')",
    "double(d)",
    "double(i)",
    "double(u)",
    "double('2.5')",
    "string(s)",
    "string(b)",
    "string(i)",
    "string(u)",
    "string(d)",
    "string(t)",
    "string(du)",
    "bool(b)",
    "bool('true')",
    "timestamp('2026-11-18T10:00:00Z')",
    "timestamp(t)",
    "timestamp(i)",
    "duration('1h30m')",
    "duration(du)",
    "dyn(i)",
    "dyn(li)",
    // Getters, in UTC.
    "t.getFullYear()",
    "t.getMonth()",
    "t.getDayOfYear()",
    "t.getDayOfMonth()",
    "t.getDate()",
    "t.getDayOfWeek()",
    "t.getHours()",
    "t.getMinutes()",
    "t.getSeconds()",
    "t.getMilliseconds()",
    "du.getHours()",
    "du.getMinutes()",
    "du.getSeconds()",
    "du.getMilliseconds()",
    // Macros.
    "li.all(n, n > 0)",
    "li.exists(n, n > 2)",
    "li.exists_one(n, n == 2)",
    "li.existsOne(n, n == 2)",
    "li.map(n, n * 2)",
    "li.map(n, n > 1, n * 2)",
    "li.filter(n, n > 1)",
    "m.all(k, k != '')",
    "o.exists(k, k == 'n')",
    "xm.l.all(n, n > 0)",
    "ls.map(x, x + s)",
    "li.map(n, [n]).all(l, size(l) == 1)",
    "has(m.k)",
    "has(xm.y)",
    // Literals.
    "[1, 'a']",
    "{'a': 1, 'b': 'c'}",
    "1u",
    "1.5",
    "'text'",
    "true",
    "null",
    "-1",
];

/// Forms the checker refuses, each of which cel-rust fails on.
const FAILING: &[&str] = &[
    "i + d",
    "i + u",
    "u * d",
    "d % d",
    "s + i",
    "-u",
    "-du",
    "t + t",
    "du - t",
    "du * i",
    "s < i",
    "t < s",
    "li < li",
    "b < i",
    "null < 1",
    "null + 1",
    "!i",
    "i && b",
    "li['a']",
    "m[1]",
    "s[0]",
    "i.x",
    "o.missing",
    "size(i)",
    "i.size()",
    "s.size(1)",
    "s.contains(1)",
    "t.getHours(1)",
    "du.getMonth()",
    "du.getHours('UTC')",
    "int(b)",
    "bool(i)",
    "duration(i)",
    "timestamp(d)",
    "string(li)",
    "i in s",
    "li.all(n, n)",
    "i.all(n, true)",
    "s.matches('a')",
    "max(li)",
    "unknown",
    "timestamp('nope')",
    "duration('8 hours')",
    "t.getHours('Nowhere/City')",
    "'a' in i",
    "li[0] + 's'",
];

/// Forms the checker refuses because they could only ever be false (or true): cel-rust
/// runs them, but a rule written so is a mistake.
const ALWAYS: &[(&str, bool)] = &[
    ("i == s", false),
    ("s == b", false),
    ("t == s", false),
    ("du == i", false),
    ("li == m", false),
    ("ls == [1]", false),
    ("1 in m", false),
    ("i in ls", false),
    ("has(o.n)", true),
];

/// Runs `source` with cel alone, without the checker.
fn unchecked(source: &str, schemas: &Value, values: &Value) -> Result<cel::Value, String> {
    let env = Arc::new(cel::Env::stdlib());
    let probes = Probes::new(Arc::clone(&env));
    let variables = declared(&variables(schemas));
    let mut scope = cel::Context::with_env(env);
    for (name, ty) in &variables {
        scope.add_variable_from_value(name.clone(), value::value(&values[name], ty, &probes));
    }
    let program = cel::Program::compile(source).map_err(|e| e.to_string())?;
    program.execute(&scope).map_err(|e| e.to_string())
}

#[test]
fn what_it_accepts_runs_and_what_it_refuses_doesnt() {
    let (schemas, values) = kinds();
    let any = |source: &str| checked_with(&schemas, &format!("{{{{ {source} }}}}"), Kind::Message);
    for source in ACCEPTED {
        let checked = any(source);
        assert!(
            matches!(checked, RuleChecked::Valid { .. }),
            "{source}: {checked:?}"
        );
        let ran = unchecked(source, &schemas, &values);
        assert!(ran.is_ok(), "{source} failed: {ran:?}");
    }
    for source in FAILING {
        let checked = any(source);
        assert!(
            matches!(checked, RuleChecked::Invalid { .. }),
            "{source} checked"
        );
        let ran = unchecked(source, &schemas, &values);
        assert!(ran.is_err(), "{source} ran: {ran:?}");
    }
    for (source, always) in ALWAYS {
        let checked = any(source);
        assert!(
            matches!(checked, RuleChecked::Invalid { .. }),
            "{source} checked"
        );
        assert_eq!(
            unchecked(source, &schemas, &values),
            Ok(cel::Value::Bool(*always)),
            "{source}"
        );
    }
}

#[test]
fn every_accepted_form_evaluates_through_the_module() {
    let (schemas, values) = kinds();
    let expressions: Vec<(String, Kind)> = ACCEPTED
        .iter()
        .map(|s| (format!("{{{{ {s} }}}}"), Kind::Message))
        .collect();
    let borrowed: Vec<(&str, Kind)> = expressions.iter().map(|(s, k)| (s.as_str(), *k)).collect();
    let out = evaluated_with(&schemas, &borrowed, &[values]);
    for (source, outcome) in ACCEPTED.iter().zip(&out[0]) {
        assert!(
            matches!(outcome, RuleEvaluated::Text { .. }),
            "{source}: {outcome:?}"
        );
    }
}

#[test]
fn json_is_read_by_its_schema() {
    let (schemas, _) = kinds();
    let out = evaluated_with(
        &schemas,
        &[
            ("d == 95.0 && type(d) == double", Kind::Condition),
            ("{{ d }} {{ i }} {{ u }}", Kind::Message),
            ("i == 5", Kind::Condition),
            ("u == null", Kind::Condition),
            ("t == null && du == null", Kind::Condition),
            ("o.s == null && o.n == 1", Kind::Condition),
            ("li == [1, null, 3]", Kind::Condition),
        ],
        &[json!({
            "d": 95, "i": 5.0, "u": -1, "t": "yesterday", "du": "long",
            "o": {"n": 1, "other": true}, "li": [1, "two", 3],
        })],
    );
    // `type` isn't one of a rule's functions.
    assert!(matches!(
        out[0][0],
        RuleEvaluated::Error { empty: false, .. }
    ));
    assert_eq!(
        out[0][1],
        RuleEvaluated::Text {
            text: "95 5 ".to_owned()
        }
    );
    assert_eq!(&out[0][2..], &[yes(), yes(), yes(), yes(), yes()]);
    let out = evaluated_with(
        &schemas,
        &[("d == 95.0", Kind::Condition)],
        &[json!({"d": 95}), json!({"d": 95.5}), json!({})],
    );
    assert_eq!(out[0], vec![yes()]);
    assert_eq!(out[1], vec![no()]);
    assert!(matches!(out[2][0], RuleEvaluated::Value { value: false }));
}

#[test]
fn an_expression_that_doesnt_check_fails_in_every_context() {
    let out = evaluated_with(
        &lims(),
        &[
            ("record.purty > 1.0", Kind::Condition),
            ("true", Kind::Condition),
        ],
        &[sample(), sample()],
    );
    for context in &out {
        assert_eq!(
            context[0],
            RuleEvaluated::Error {
                error: "Sample has no field `purty`.".to_owned(),
                empty: false
            }
        );
        assert_eq!(context[1], yes());
    }
}

#[test]
fn a_schema_must_be_one() {
    for schema in [
        json!({"x": {"type": "float"}}),
        json!({"x": {"type": "string", "vales": ["a"]}}),
        json!({"x": {"type": "object", "fields": {}}}),
        json!({"x": {"type": "list"}}),
    ] {
        assert!(
            serde_json::from_value::<BTreeMap<String, Schema>>(schema.clone()).is_err(),
            "{schema}"
        );
    }
}
