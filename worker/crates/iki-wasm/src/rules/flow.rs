//! A diagram can know a transition's field, source and destination, but has no
//! record, actor or history. Walk the checked guards' CEL syntax trees with those
//! three facts. An unrecognised expression is unknown, never a reason to omit an edge.

use cel::IdedExpr;
use cel::common::ast::{Expr, LiteralValue, operators};
use serde::Deserialize;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    field: String,
    origins: Vec<String>,
    target: String,
    guards: Vec<Guard>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Guard {
    check: String,
    when: Option<String>,
}

/// The result of two conditions that must both hold, when one or both can be unknown.
fn both(a: Option<bool>, b: Option<bool>) -> Option<bool> {
    match (a, b) {
        (Some(false), _) | (_, Some(false)) => Some(false),
        (Some(true), Some(true)) => Some(true),
        _ => None,
    }
}

struct Change<'a> {
    field: &'a str,
    from: &'a str,
    to: &'a str,
}

impl Change<'_> {
    fn text<'a>(&'a self, expr: &'a IdedExpr) -> Option<&'a str> {
        match &expr.expr {
            Expr::Literal(LiteralValue::String(s)) => Some(s.inner()),
            Expr::Select(s) if !s.test => {
                let Expr::Ident(name) = &s.operand.expr else {
                    return None;
                };
                match (name.as_str(), s.field.as_str()) {
                    ("state", "from") => Some(self.from),
                    ("state", "to") => Some(self.to),
                    ("state", "field") => Some(self.field),
                    ("record", field) if field == self.field => Some(self.from),
                    _ => None,
                }
            }
            _ => None,
        }
    }

    fn truth(&self, expr: &IdedExpr) -> Option<bool> {
        match &expr.expr {
            Expr::Literal(LiteralValue::Boolean(b)) => Some(**b),
            Expr::Call(c) if c.target.is_none() => {
                match (c.func_name.as_str(), c.args.as_slice()) {
                    (operators::LOGICAL_AND, [a, b]) => both(self.truth(a), self.truth(b)),
                    (operators::LOGICAL_OR, [a, b]) => match (self.truth(a), self.truth(b)) {
                        (Some(true), _) | (_, Some(true)) => Some(true),
                        (Some(false), Some(false)) => Some(false),
                        _ => None,
                    },
                    (operators::LOGICAL_NOT, [a]) => Some(!self.truth(a)?),
                    (operators::EQUALS, [a, b]) => Some(self.text(a)? == self.text(b)?),
                    (operators::NOT_EQUALS, [a, b]) => Some(self.text(a)? != self.text(b)?),
                    (operators::IN, [a, b]) => {
                        let item = self.text(a)?;
                        let Expr::List(list) = &b.expr else {
                            return None;
                        };
                        let mut found = false;
                        for element in &list.elements {
                            found |= self.text(element)? == item;
                        }
                        Some(found)
                    }
                    _ => None,
                }
            }
            _ => None,
        }
    }
}

/// One outcome per origin: false rules it out; null means the record or other
/// information is still needed; true means the declared guards allow this origin.
pub(crate) fn read_json(bytes: &[u8]) -> Vec<u8> {
    let request = match serde_json::from_slice::<Request>(bytes) {
        Ok(request) => request,
        Err(error) => return crate::failed(&format!("The request isn't one: {error}.")),
    };
    let guards: Vec<_> = request
        .guards
        .iter()
        .map(|g| {
            (
                super::parse(&g.check).ok(),
                g.when.as_deref().map(super::parse),
            )
        })
        .collect();
    let origins: Vec<Option<bool>> = request
        .origins
        .iter()
        .map(|from| {
            let change = Change {
                field: &request.field,
                from,
                to: &request.target,
            };
            let mut all = Some(true);
            for (check, when) in &guards {
                let applies = match when {
                    None => Some(true),
                    Some(w) => w.as_ref().ok().and_then(|p| change.truth(p.expression())),
                };
                let holds = match applies {
                    Some(false) => Some(true),
                    Some(true) => check.as_ref().and_then(|p| change.truth(p.expression())),
                    None => None,
                };
                match holds {
                    Some(false) => return Some(false),
                    None => all = None,
                    Some(true) => (),
                }
            }
            all
        })
        .collect();
    serde_json::to_vec(&serde_json::json!({ "origins": origins }))
        .unwrap_or_else(|_| crate::failed("The transitions couldn't be written out."))
}

#[cfg(test)]
mod tests {
    use serde_json::{Value, json};

    fn project(check: &str, when: Option<&str>) -> Value {
        let request = json!({
            "field": "review", "target": "released", "origins": ["recorded", "submitted", "reviewed"],
            "guards": [{"check": check, "when": when}]
        });
        let reply = super::read_json(request.to_string().as_bytes());
        serde_json::from_slice::<Value>(&reply).unwrap_or_default()["origins"].clone()
    }

    #[test]
    fn state_comparisons_rule_out_only_impossible_origins() {
        assert_eq!(
            project(
                "(state.from == 'submitted' || state.from == 'reviewed') && record.fit != null",
                None,
            ),
            json!([false, null, null])
        );
        assert_eq!(
            project("!(record.review in ['recorded','submitted'])", None),
            json!([false, false, true])
        );
        assert_eq!(
            project("state.from != 'reviewed' || actor.waiver", None),
            json!([true, true, null])
        );
        assert_eq!(
            project("state.field == 'review' && 'released' == state.to", None),
            json!([true, true, true])
        );
    }

    #[test]
    fn when_and_unknown_expressions_keep_conditional_moves() {
        assert_eq!(
            project("false", Some("state.from != 'recorded'")),
            json!([true, false, false])
        );
        assert_eq!(
            project("state.from == 'reviewed'", Some("actor.waiver")),
            json!([null, null, null])
        );
        assert_eq!(
            project("history.all(h, h.by != actor.person)", None),
            json!([null, null, null])
        );
    }
}
