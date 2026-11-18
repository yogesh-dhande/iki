//! The type checker: what each part of a rule's expression is, from the declared
//! variables' schemas, so that a rule that would fail on every record is refused when
//! its plugin is loaded rather than when someone saves. It accepts what cel-rust runs
//! without a type error, and nothing else: cel has no checker of its own.

use cel::IdedExpr;
use cel::common::ast::{
    CallExpr, ComprehensionExpr, EntryExpr, Expr, LiteralValue, SelectExpr, SourceInfo, operators,
};
use std::collections::{BTreeMap, BTreeSet};

use super::types::{
    Choices, Object, Ty, capitalized, closest, comparable, join, listed, orderable, short,
};
use super::value::Probes;

/// Where a fault is in the expression it's in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum At {
    /// At this byte.
    Byte(usize),
    /// At this line and column (in characters), from 1, as the parser counts.
    LineColumn(usize, usize),
}

/// Why an expression doesn't check, and where.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct Fault {
    /// Why, for a person.
    pub reason: String,
    /// What to write instead, if there's something to say.
    pub hint: Option<String>,
    /// Where, if it's somewhere in particular.
    pub at: Option<At>,
    /// Whether `reason` is the parser's, which ends with where it is.
    pub parsed: bool,
}

impl Fault {
    /// A fault with no hint, anywhere.
    pub(super) fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
            hint: None,
            at: None,
            parsed: false,
        }
    }

    /// The fault, with a hint.
    #[must_use]
    pub(super) fn hint(mut self, hint: impl Into<String>) -> Self {
        self.hint = Some(hint.into());
        self
    }

    /// The fault, with a hint if there is one.
    #[must_use]
    fn maybe(mut self, hint: Option<String>) -> Self {
        self.hint = hint;
        self
    }
}

/// What part of an expression is, or why it doesn't check.
type Checked = Result<Ty, Fault>;

/// What an expression reads of one variable.
#[derive(Debug, Default)]
pub(super) struct Reads {
    /// Whether it reads it whole, not just some fields of it.
    pub whole: bool,
    /// The fields it selects of it.
    pub fields: BTreeSet<String>,
}

/// The global functions the checker knows, as cel's standard library has them.
const FUNCTIONS: [&str; 9] = [
    "size",
    "int",
    "uint",
    "double",
    "string",
    "bool",
    "timestamp",
    "duration",
    "dyn",
];

/// A timestamp's getters: each takes an optional time zone.
const TIMESTAMP_GETTERS: [&str; 10] = [
    "getFullYear",
    "getMonth",
    "getDayOfYear",
    "getDayOfMonth",
    "getDate",
    "getDayOfWeek",
    "getHours",
    "getMinutes",
    "getSeconds",
    "getMilliseconds",
];

/// A duration's getters.
const DURATION_GETTERS: [&str; 4] = ["getHours", "getMinutes", "getSeconds", "getMilliseconds"];

/// Text's methods.
const TEXT_METHODS: [&str; 4] = ["contains", "startsWith", "endsWith", "size"];

/// The macros, which are written as methods of a list or map.
const MACROS: [&str; 6] = ["all", "exists", "exists_one", "existsOne", "map", "filter"];

/// The arithmetic operators.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Op {
    Add,
    Sub,
    Mul,
    Div,
    Rem,
}

impl Op {
    fn of(name: &str) -> Option<Self> {
        match name {
            operators::ADD => Some(Self::Add),
            operators::SUBSTRACT => Some(Self::Sub),
            operators::MULTIPLY => Some(Self::Mul),
            operators::DIVIDE => Some(Self::Div),
            operators::MODULO => Some(Self::Rem),
            _ => None,
        }
    }

    /// Whether it works on this kind of value with some other.
    fn takes(self, t: &Ty) -> bool {
        match self {
            Self::Add => {
                t.numeric() || matches!(t, Ty::Str(_) | Ty::List(_) | Ty::Timestamp | Ty::Duration)
            }
            Self::Sub => t.numeric() || matches!(t, Ty::Timestamp | Ty::Duration),
            Self::Mul | Self::Div => t.numeric(),
            Self::Rem => matches!(t, Ty::Int | Ty::UInt),
        }
    }

    /// What it makes of `a` and `b`, as cel-rust's operators do: numbers of the same
    /// kind only (cel doesn't mix them in arithmetic), text and lists joined, and times
    /// and durations as the spec has them.
    fn result(self, a: &Ty, b: &Ty) -> Option<Ty> {
        match (self, a, b) {
            (Self::Rem, Ty::Double, Ty::Double) => None,
            (_, Ty::Int, Ty::Int) => Some(Ty::Int),
            (_, Ty::UInt, Ty::UInt) => Some(Ty::UInt),
            (_, Ty::Double, Ty::Double) => Some(Ty::Double),
            (Self::Add, Ty::Str(_), Ty::Str(_)) => Some(Ty::Str(None)),
            (Self::Add, Ty::List(x), Ty::List(y)) => {
                Some(Ty::List(Box::new(join(x, y).unwrap_or(Ty::Dyn))))
            }
            (Self::Add | Self::Sub, Ty::Timestamp, Ty::Duration)
            | (Self::Add, Ty::Duration, Ty::Timestamp) => Some(Ty::Timestamp),
            (Self::Add | Self::Sub, Ty::Duration, Ty::Duration)
            | (Self::Sub, Ty::Timestamp, Ty::Timestamp) => Some(Ty::Duration),
            (_, Ty::Dyn, Ty::Dyn) => Some(Ty::Dyn),
            (_, Ty::Dyn, t) | (_, t, Ty::Dyn) if self.takes(t) => Some(Ty::Dyn),
            _ => None,
        }
    }

    /// Why it doesn't work on `a` and `b`.
    fn refusal(self, a: &Ty, b: &Ty) -> String {
        let (a, b) = (a.describe(), b.describe());
        match self {
            Self::Add => format!("`+` doesn't add {a} and {b}."),
            Self::Sub => format!("`-` doesn't subtract {b} from {a}."),
            Self::Mul => format!("`*` doesn't multiply {a} by {b}."),
            Self::Div => format!("`/` doesn't divide {a} by {b}."),
            Self::Rem => format!("`%` doesn't divide {a} by {b}."),
        }
    }
}

/// A whole number as written, if the expression is one: what a hint can rewrite.
fn whole_literal(expr: &IdedExpr) -> Option<String> {
    match &expr.expr {
        Expr::Literal(LiteralValue::Int(i)) => Some(i.inner().to_string()),
        Expr::Literal(LiteralValue::UInt(u)) => Some(u.inner().to_string()),
        _ => None,
    }
}

/// Text as written, if the expression is a string literal.
fn text_literal(expr: &IdedExpr) -> Option<&str> {
    match &expr.expr {
        Expr::Literal(LiteralValue::String(s)) => Some(s.inner()),
        _ => None,
    }
}

/// What to write instead when numbers of different kinds meet in arithmetic.
fn numbers_hint(a: &Ty, b: &Ty, left: &IdedExpr, right: &IdedExpr) -> String {
    if matches!(a, Ty::Double) || matches!(b, Ty::Double) {
        let whole = if matches!(a, Ty::Double) { right } else { left };
        let one = whole_literal(whole).unwrap_or_else(|| "1".to_owned());
        return format!("Write `{one}.0` for a number, or convert with `double(…)`.");
    }
    // An int and a uint: the int may have been meant unsigned.
    let signed = if matches!(a, Ty::Int) { left } else { right };
    match whole_literal(signed) {
        Some(n) => format!(
            "Write `{n}u` for an unsigned whole number, or convert with `int(…)` or `uint(…)`."
        ),
        None => "Convert one with `int(…)` or `uint(…)`.".to_owned(),
    }
}

/// What to write instead when arithmetic doesn't work on two kinds of value.
fn arithmetic_hint(op: Op, a: &Ty, b: &Ty) -> Option<String> {
    let time_or_length = |t: &Ty| matches!(t, Ty::Timestamp | Ty::Duration);
    match (op, a, b) {
        (Op::Add, Ty::Str(_), t) | (Op::Add, t, Ty::Str(_))
            if t.numeric() || matches!(t, Ty::Bool) || time_or_length(t) =>
        {
            Some("Convert it to text with `string(…)` first.".to_owned())
        }
        (Op::Add, Ty::Timestamp, Ty::Timestamp) => {
            Some("Add a duration to a time, as in `now + duration('8h')`.".to_owned())
        }
        (Op::Sub, Ty::Duration, Ty::Timestamp) => {
            Some("Subtract the earlier time from the later one: `later - earlier`.".to_owned())
        }
        (_, Ty::Str(_), t) | (_, t, Ty::Str(_)) if time_or_length(t) => Some(
            "Write a time as `timestamp('2026-11-18T10:00:00Z')` and a duration as `duration('8h')`."
                .to_owned(),
        ),
        (_, x, y) if (time_or_length(x) && y.numeric()) || (x.numeric() && time_or_length(y)) => {
            Some("Write a duration as `duration('8h')`.".to_owned())
        }
        _ => None,
    }
}

/// What to write instead when `==` compares kinds that are never equal.
fn equality_hint(a: &Ty, b: &Ty, left: &IdedExpr) -> Option<String> {
    match (a, b) {
        (Ty::Object(o), Ty::Str(_)) if o.fields.contains_key("hrid") => {
            Some(spelled(left).map_or_else(
                || "Compare its `hrid`.".to_owned(),
                |s| format!("Compare its HRID: `{s}.hrid`."),
            ))
        }
        (Ty::Timestamp, Ty::Str(_)) | (Ty::Str(_), Ty::Timestamp) => {
            Some("Write the time as `timestamp('2026-11-18T10:00:00Z')`.".to_owned())
        }
        (Ty::Duration, Ty::Str(_)) | (Ty::Str(_), Ty::Duration) => {
            Some("Write the duration as `duration('8h')`.".to_owned())
        }
        (Ty::Bool, Ty::Str(_)) | (Ty::Str(_), Ty::Bool) => {
            Some("Write `true` or `false`, without quotes.".to_owned())
        }
        (t, Ty::Str(_)) | (Ty::Str(_), t) if t.numeric() => Some(
            "Convert the text with `double(…)` or `int(…)`, or the number with `string(…)`."
                .to_owned(),
        ),
        _ => None,
    }
}

/// What to write instead when `<` and the like don't order two kinds of value.
fn ordering_hint(a: &Ty, b: &Ty) -> Option<String> {
    match (a, b) {
        (Ty::Timestamp, Ty::Str(_)) | (Ty::Str(_), Ty::Timestamp) => {
            Some("Write the time as `timestamp('2026-11-18T10:00:00Z')`.".to_owned())
        }
        (Ty::Duration, Ty::Str(_)) | (Ty::Str(_), Ty::Duration) => {
            Some("Write the duration as `duration('8h')`.".to_owned())
        }
        (Ty::Timestamp, Ty::Duration) | (Ty::Duration, Ty::Timestamp) => Some(
            "Add the duration to a time first, as in `record.starts_at + duration('8h')`."
                .to_owned(),
        ),
        (Ty::Null, _) | (_, Ty::Null) => {
            Some("`null` is only equal or unequal to things: use `==` or `!=`.".to_owned())
        }
        (t, Ty::Str(_)) | (Ty::Str(_), t) if t.numeric() => {
            Some("Convert the text with `double(…)` or `int(…)` first.".to_owned())
        }
        _ => None,
    }
}

/// The expression as written, when it's a name and fields of it: `record.instrument`.
fn spelled(expr: &IdedExpr) -> Option<String> {
    match &expr.expr {
        Expr::Ident(name) => Some(name.clone()),
        Expr::Select(select) if !select.test => {
            Some(format!("{}.{}", spelled(&select.operand)?, select.field))
        }
        _ => None,
    }
}

/// "Did you mean `x`?" when one of `candidates` is close to `word`, or else `fallback`
/// followed by the candidates, if there are any.
fn suggestion(word: &str, candidates: &[&str], fallback: &str) -> Option<String> {
    if let Some(close) = closest(word, candidates.iter().copied()) {
        return Some(format!("Did you mean `{close}`?"));
    }
    (!candidates.is_empty()).then(|| format!("{fallback} {}.", listed(candidates.iter().copied())))
}

/// Why a string literal isn't one of a field's values, with what it might have meant.
fn not_a_choice(text: &str, choices: &Choices) -> Fault {
    let values: Vec<&str> = choices.values.iter().map(String::as_str).collect();
    let all = listed(values.iter().copied());
    let hint = match closest(text, values.iter().copied()) {
        Some(close) => format!("Did you mean `{close}`? It's one of {all}."),
        None => format!("It's one of {all}."),
    };
    Fault::new(format!(
        "`{}` isn't one of {}'s values.",
        short(text),
        choices.label
    ))
    .hint(hint)
}

/// Why an object has no field of that name.
fn no_field(object: &Object, field: &str) -> Fault {
    let fields: Vec<&str> = object.fields.keys().map(String::as_str).collect();
    Fault::new(format!("{} has no field `{}`.", object.label, short(field))).maybe(suggestion(
        field,
        &fields,
        "Its fields are",
    ))
}

/// The macro a comprehension was expanded from, and the parts of it a person wrote.
enum Shape<'a> {
    /// `all(x, condition)`.
    All(&'a IdedExpr),
    /// `exists(x, condition)`.
    Exists(&'a IdedExpr),
    /// `exists_one(x, condition)`.
    ExistsOne(&'a IdedExpr),
    /// `map(x, value)`.
    Map(&'a IdedExpr),
    /// `map(x, condition, value)`, or `filter(x, condition)` when the value is `x`.
    MapIf(&'a IdedExpr, &'a IdedExpr),
}

impl<'a> Shape<'a> {
    /// The shape of `c`, if it's one of the standard macros' expansions.
    fn of(c: &'a ComprehensionExpr) -> Option<Self> {
        let Expr::Call(step) = &c.loop_step.expr else {
            return None;
        };
        let accumulator = |e: &IdedExpr| matches!(&e.expr, Expr::Ident(n) if *n == c.accu_var);
        // `@result + [value]`: the value a map or filter appends.
        let appended = |e: &'a IdedExpr| match &e.expr {
            Expr::Call(add) if add.func_name == operators::ADD => match add.args.as_slice() {
                [accu, list] if accumulator(accu) => match &list.expr {
                    Expr::List(list) if list.elements.len() == 1 => list.elements.first(),
                    _ => None,
                },
                _ => None,
            },
            _ => None,
        };
        match (
            &c.accu_init.expr,
            step.func_name.as_str(),
            step.args.as_slice(),
        ) {
            (Expr::Literal(LiteralValue::Boolean(_)), operators::LOGICAL_AND, [accu, p])
                if accumulator(accu) =>
            {
                Some(Self::All(p))
            }
            (Expr::Literal(LiteralValue::Boolean(_)), operators::LOGICAL_OR, [accu, p])
                if accumulator(accu) =>
            {
                Some(Self::Exists(p))
            }
            (Expr::Literal(LiteralValue::Int(_)), operators::CONDITIONAL, [p, _, _]) => {
                Some(Self::ExistsOne(p))
            }
            (Expr::List(_), operators::ADD, _) => appended(&c.loop_step).map(Self::Map),
            (Expr::List(_), operators::CONDITIONAL, [condition, add, accu])
                if accumulator(accu) =>
            {
                appended(add).map(|value| Self::MapIf(condition, value))
            }
            _ => None,
        }
    }

    /// The macro's name, as written.
    fn name(&self, variable: &str) -> &'static str {
        match self {
            Self::All(_) => "all",
            Self::Exists(_) => "exists",
            Self::ExistsOne(_) => "exists_one",
            Self::Map(_) => "map",
            Self::MapIf(_, value) => match &value.expr {
                Expr::Ident(name) if name == variable => "filter",
                _ => "map",
            },
        }
    }
}

/// Checks one expression against the declared variables.
pub(super) struct Checker<'a> {
    variables: &'a BTreeMap<String, Ty>,
    info: &'a SourceInfo,
    source: &'a str,
    probes: &'a Probes,
    /// The variables comprehensions bind, innermost last.
    bound: Vec<(String, Ty)>,
    /// What it reads of each declared variable.
    pub reads: BTreeMap<String, Reads>,
    /// Each variable it reads, and each chain of fields it selects of one: `[record]`,
    /// `[record, instrument]`, `[record, instrument, service]`.
    pub paths: BTreeSet<Vec<String>>,
}

impl<'a> Checker<'a> {
    /// A checker for an expression `source`, parsed with `info`.
    pub(super) fn new(
        variables: &'a BTreeMap<String, Ty>,
        info: &'a SourceInfo,
        source: &'a str,
        probes: &'a Probes,
    ) -> Self {
        Self {
            variables,
            info,
            source,
            probes,
            bound: Vec::new(),
            reads: BTreeMap::new(),
            paths: BTreeSet::new(),
        }
    }

    /// The fault, placed at node `id`.
    fn at(&self, id: u64, mut fault: Fault) -> Fault {
        if fault.at.is_none() {
            fault.at = self
                .info
                .offset_for(id)
                .and_then(|(start, _)| usize::try_from(start).ok())
                .map(At::Byte);
        }
        fault
    }

    /// The fault, placed at a select's field name, which follows its `.` (node `id`).
    fn at_field(&self, id: u64, field: &str, fault: Fault) -> Fault {
        let dot = self
            .info
            .offset_for(id)
            .and_then(|(start, _)| usize::try_from(start).ok());
        let byte = dot.and_then(|dot| {
            self.source
                .get(dot..)
                .and_then(|rest| rest.find(field))
                .map(|i| dot + i)
        });
        match byte {
            Some(byte) => Fault {
                at: Some(At::Byte(byte)),
                ..fault
            },
            None => self.at(id, fault),
        }
    }

    fn is_bound(&self, name: &str) -> bool {
        self.bound.iter().any(|(n, _)| n == name)
    }

    /// Whether `name` is a declared variable a comprehension doesn't hide.
    fn is_free(&self, name: &str) -> bool {
        !self.is_bound(name) && self.variables.contains_key(name)
    }

    /// The variable or field chain `expr` is, rooted at a declared variable: what can be
    /// empty in a context.
    fn path(&self, expr: &IdedExpr) -> Option<Vec<String>> {
        match &expr.expr {
            Expr::Ident(name) if self.is_free(name) => Some(vec![name.clone()]),
            Expr::Select(select) if !select.test => {
                let mut path = self.path(&select.operand)?;
                path.push(select.field.clone());
                Some(path)
            }
            Expr::Call(call) if call.func_name == operators::INDEX && call.target.is_none() => {
                match call.args.as_slice() {
                    [of, key] => {
                        let key = text_literal(key)?;
                        let mut path = self.path(of)?;
                        path.push(key.to_owned());
                        Some(path)
                    }
                    _ => None,
                }
            }
            _ => None,
        }
    }

    /// Notes that the expression reads field `field` of `operand`, if that's a variable.
    fn read_field(&mut self, operand: &IdedExpr, field: &str) {
        if let Expr::Ident(name) = &operand.expr
            && self.is_free(name)
        {
            self.reads
                .entry(name.clone())
                .or_default()
                .fields
                .insert(field.to_owned());
        }
    }

    /// What expression `expr` is, which must be yes or no: the whole of a condition.
    pub(super) fn condition(&mut self, expr: &IdedExpr) -> Result<(), Fault> {
        let ty = self.check(expr)?;
        if ty.boolish() {
            return Ok(());
        }
        Err(Fault::new(format!(
            "It works out to {}, but a condition must be yes or no.",
            ty.describe()
        ))
        .hint("Compare it, as in `record.purity >= 95.0`, or test it, as in `record.tags.exists(t, t == 'urgent')`."))
    }

    /// What `expr` is.
    pub(super) fn check(&mut self, expr: &IdedExpr) -> Checked {
        match &expr.expr {
            Expr::Literal(literal) => self.literal(expr.id, literal),
            Expr::Ident(name) => self.ident(expr.id, name, true),
            Expr::Select(select) => self.select(expr, select),
            Expr::Call(call) => self.call(expr, call),
            Expr::List(list) => {
                if !list.optional_indices.is_empty() {
                    return Err(self.at(expr.id, optional()));
                }
                let mut item: Option<Ty> = None;
                for element in &list.elements {
                    let ty = self.check(element)?;
                    item = Some(match item {
                        None => ty,
                        Some(before) => join(&before, &ty).unwrap_or(Ty::Dyn),
                    });
                }
                Ok(Ty::List(Box::new(item.unwrap_or(Ty::Dyn))))
            }
            Expr::Map(map) => self.map(expr.id, &map.entries),
            Expr::Comprehension(c) => self.comprehension(expr.id, c),
            Expr::Struct(_) => Err(self.at(
                expr.id,
                Fault::new("Typed objects (`Name{…}`) aren't supported in rules.")
                    .hint("Write a map, as `{'key': value}`."),
            )),
            Expr::Unspecified => Err(self.at(expr.id, Fault::new("It isn't an expression."))),
        }
    }

    fn literal(&self, id: u64, literal: &LiteralValue) -> Checked {
        Ok(match literal {
            LiteralValue::Boolean(_) => Ty::Bool,
            LiteralValue::Int(_) => Ty::Int,
            LiteralValue::UInt(_) => Ty::UInt,
            LiteralValue::Double(_) => Ty::Double,
            LiteralValue::String(_) => Ty::Str(None),
            LiteralValue::Null => Ty::Null,
            LiteralValue::Bytes(_) => {
                return Err(self.at(id, Fault::new("Bytes (`b'…'`) aren't supported in rules.")));
            }
        })
    }

    /// A name: a variable a comprehension binds, or a declared one, read whole unless
    /// `whole` is false (it's what a select selects a field of).
    fn ident(&mut self, id: u64, name: &str, whole: bool) -> Checked {
        if let Some((_, ty)) = self.bound.iter().rev().find(|(n, _)| n == name) {
            return Ok(ty.clone());
        }
        if let Some(ty) = self.variables.get(name) {
            self.reads.entry(name.to_owned()).or_default().whole |= whole;
            self.paths.insert(vec![name.to_owned()]);
            return Ok(ty.clone());
        }
        let mut names: Vec<&str> = self.variables.keys().map(String::as_str).collect();
        names.extend(self.bound.iter().map(|(n, _)| n.as_str()));
        let fault = Fault::new(format!("There's nothing called `{}`.", short(name)))
            .maybe(suggestion(name, &names, "It can read"));
        Err(self.at(id, fault))
    }

    /// What `operand` is, as something a field is selected or an item indexed of: a
    /// variable isn't read whole.
    fn operand(&mut self, operand: &IdedExpr, whole: bool) -> Checked {
        match &operand.expr {
            Expr::Ident(name) => self.ident(operand.id, name, whole),
            _ => self.check(operand),
        }
    }

    fn select(&mut self, expr: &IdedExpr, select: &SelectExpr) -> Checked {
        let base = self.operand(&select.operand, false)?;
        let field = select.field.as_str();
        if select.test {
            return self.has(expr.id, select, &base);
        }
        let ty = match &base {
            Ty::Object(object) => match object.fields.get(field) {
                Some(ty) => ty.clone(),
                None => return Err(self.at_field(expr.id, field, no_field(object, field))),
            },
            Ty::Map(values) => (**values).clone(),
            Ty::Dyn => Ty::Dyn,
            other => {
                let fault = Fault::new(format!(
                    "`.{}` can't be read of {}.",
                    short(field),
                    other.describe()
                ));
                return Err(self.at_field(expr.id, field, fault));
            }
        };
        self.read_field(&select.operand, field);
        if let Some(path) = self.path(expr) {
            self.paths.insert(path);
        }
        Ok(ty)
    }

    /// `has(a.b)`: whether a map has the key.
    fn has(&mut self, id: u64, select: &SelectExpr, base: &Ty) -> Checked {
        let field = select.field.as_str();
        match base {
            Ty::Object(object) if object.fields.contains_key(field) => {
                let written = spelled(&select.operand)
                    .map_or_else(|| field.to_owned(), |s| format!("{s}.{field}"));
                Err(self.at_field(
                    id,
                    field,
                    Fault::new(format!(
                        "`has()` is always true of {}'s fields: they're there even when they're empty.",
                        object.label
                    ))
                    .hint(format!("Write `{written} != null` to check it has a value.")),
                ))
            }
            Ty::Object(object) => Err(self.at_field(id, field, no_field(object, field))),
            Ty::Map(_) | Ty::Dyn => {
                self.read_field(&select.operand, field);
                Ok(Ty::Bool)
            }
            other => Err(self.at_field(
                id,
                field,
                Fault::new(format!(
                    "`has()` checks a map's keys, and {} isn't a map.",
                    other.describe()
                )),
            )),
        }
    }

    fn map(&mut self, id: u64, entries: &[cel::common::ast::IdedEntryExpr]) -> Checked {
        let mut values: Option<Ty> = None;
        for entry in entries {
            let EntryExpr::MapEntry(entry) = &entry.expr else {
                return Err(self.at(id, Fault::new("It isn't a map.")));
            };
            if entry.optional {
                return Err(self.at(id, optional()));
            }
            let key = self.check(&entry.key)?;
            if !key.textual() {
                let fault = Fault::new(format!(
                    "A map's keys must be text here, not {}.",
                    key.describe()
                ));
                return Err(self.at(entry.key.id, fault));
            }
            let ty = self.check(&entry.value)?;
            values = Some(match values {
                None => ty,
                Some(before) => join(&before, &ty).unwrap_or(Ty::Dyn),
            });
        }
        Ok(Ty::Map(Box::new(values.unwrap_or(Ty::Dyn))))
    }

    fn call(&mut self, expr: &IdedExpr, call: &CallExpr) -> Checked {
        if call.target.is_some() {
            return self.method(expr, call);
        }
        let name = call.func_name.as_str();
        let args = call.args.as_slice();
        match (name, args) {
            (operators::LOGICAL_AND | operators::LOGICAL_OR, [a, b]) => {
                let symbol = if name == operators::LOGICAL_AND {
                    "&&"
                } else {
                    "||"
                };
                for side in [a, b] {
                    let ty = self.check(side)?;
                    if !ty.boolish() {
                        let fault = Fault::new(format!(
                            "`{symbol}` joins yes-or-no values, and this is {}.",
                            ty.describe()
                        ));
                        return Err(self.at(side.id, fault));
                    }
                }
                Ok(Ty::Bool)
            }
            (operators::LOGICAL_NOT, [a]) => {
                let ty = self.check(a)?;
                if ty.boolish() {
                    return Ok(Ty::Bool);
                }
                let fault = Fault::new(format!(
                    "`!` turns yes into no, and this is {}.",
                    ty.describe()
                ));
                Err(self.at(expr.id, fault))
            }
            (operators::NEGATE, [a]) => {
                let ty = self.check(a)?;
                match ty {
                    Ty::Int | Ty::Double | Ty::Dyn => Ok(ty),
                    other => Err(self.at(
                        expr.id,
                        Fault::new(format!("`-` can't negate {}.", other.describe())),
                    )),
                }
            }
            (operators::CONDITIONAL, [condition, yes, no]) => self.conditional(condition, yes, no),
            (operators::EQUALS | operators::NOT_EQUALS, [a, b]) => {
                let symbol = if name == operators::EQUALS {
                    "=="
                } else {
                    "!="
                };
                self.equality(expr.id, symbol, a, b)
            }
            (
                operators::LESS
                | operators::LESS_EQUALS
                | operators::GREATER
                | operators::GREATER_EQUALS,
                [a, b],
            ) => {
                let (x, y) = (self.check(a)?, self.check(b)?);
                if orderable(&x, &y) {
                    return Ok(Ty::Bool);
                }
                let symbol = name.trim_matches('_');
                let fault = Fault::new(format!(
                    "`{symbol}` can't compare {} with {}.",
                    x.describe(),
                    y.describe()
                ))
                .maybe(ordering_hint(&x, &y));
                Err(self.at(expr.id, fault))
            }
            (operators::IN, [a, b]) => self.membership(expr.id, a, b),
            (operators::INDEX, [of, key]) => self.index(expr, of, key),
            (operators::OPT_INDEX | operators::OPT_SELECT, _) => Err(self.at(expr.id, optional())),
            (operators::NOT_STRICTLY_FALSE, [a]) => {
                self.check(a)?;
                Ok(Ty::Bool)
            }
            _ => match Op::of(name) {
                Some(op) if args.len() == 2 => self.arithmetic(expr.id, op, &args[0], &args[1]),
                _ => self.function(expr, call),
            },
        }
    }

    fn conditional(&mut self, condition: &IdedExpr, yes: &IdedExpr, no: &IdedExpr) -> Checked {
        let ty = self.check(condition)?;
        if !ty.boolish() {
            let fault = Fault::new(format!(
                "What comes before `?` must be yes or no, and this is {}.",
                ty.describe()
            ));
            return Err(self.at(condition.id, fault));
        }
        let (a, b) = (self.check(yes)?, self.check(no)?);
        join(&a, &b).ok_or_else(|| {
            let fault = Fault::new(format!(
                "The two sides of `? :` differ: {} and {}.",
                a.describe(),
                b.describe()
            ));
            let fault = if a.numeric() && b.numeric() {
                fault.hint("Write both as numbers, as `1.0`, or convert with `double(…)`.")
            } else {
                fault.hint("Make both the same kind of value, or wrap one in `dyn(…)`.")
            };
            self.at(no.id, fault)
        })
    }

    /// Whether a string literal on one side of `==` is a value the other side can be.
    fn choice(&self, ty: &Ty, other: &IdedExpr) -> Result<(), Fault> {
        if let (Ty::Str(Some(choices)), Some(text)) = (ty, text_literal(other))
            && !choices.values.iter().any(|v| v == text)
        {
            return Err(self.at(other.id, not_a_choice(text, choices)));
        }
        Ok(())
    }

    fn equality(&mut self, id: u64, symbol: &str, a: &IdedExpr, b: &IdedExpr) -> Checked {
        let (x, y) = (self.check(a)?, self.check(b)?);
        if !comparable(&x, &y) {
            let fault = Fault::new(format!(
                "`{symbol}` compares {} with {}, which are never equal.",
                x.describe(),
                y.describe()
            ))
            .maybe(equality_hint(&x, &y, a).or_else(|| equality_hint(&y, &x, b)));
            return Err(self.at(id, fault));
        }
        self.choice(&x, b)?;
        self.choice(&y, a)?;
        Ok(Ty::Bool)
    }

    fn membership(&mut self, id: u64, item: &IdedExpr, container: &IdedExpr) -> Checked {
        let x = self.check(item)?;
        let c = self.check(container)?;
        match &c {
            Ty::List(of) => {
                if !comparable(&x, of) {
                    let fault = Fault::new(format!(
                        "`in` looks for {} in a list of {}, which never holds it.",
                        x.describe(),
                        plural(of)
                    ));
                    return Err(self.at(id, fault));
                }
                self.choice(of, item)?;
                if let Expr::List(list) = &container.expr {
                    for element in &list.elements {
                        self.choice(&x, element)?;
                    }
                }
            }
            Ty::Map(_) | Ty::Object(_) => {
                if !x.textual() {
                    let fault = Fault::new(format!(
                        "A map's keys are text, so `in` can't look for {} in it.",
                        x.describe()
                    ));
                    return Err(self.at(id, fault));
                }
                if let (Ty::Object(object), Some(field)) = (&c, text_literal(item))
                    && !object.fields.contains_key(field)
                {
                    return Err(self.at(item.id, no_field(object, field)));
                }
            }
            Ty::Dyn => {}
            other => {
                let fault = Fault::new(format!(
                    "`in` looks in a list or a map, and this is {}.",
                    other.describe()
                ));
                return Err(self.at(container.id, fault));
            }
        }
        Ok(Ty::Bool)
    }

    fn index(&mut self, expr: &IdedExpr, of: &IdedExpr, key: &IdedExpr) -> Checked {
        let field = text_literal(key);
        let c = self.operand(of, field.is_none())?;
        let k = self.check(key)?;
        let ty = match &c {
            Ty::List(item) => match k {
                Ty::Int | Ty::UInt | Ty::Dyn => (**item).clone(),
                other => {
                    let fault = Fault::new(format!(
                        "A list's items are numbered from 0: `[…]` takes a whole number, not {}.",
                        other.describe()
                    ));
                    let fault = if matches!(other, Ty::Double) {
                        fault.hint("Write `0`, not `0.0`, or convert with `int(…)`.")
                    } else {
                        fault
                    };
                    return Err(self.at(key.id, fault));
                }
            },
            Ty::Map(values) if k.textual() => (**values).clone(),
            Ty::Object(object) if k.textual() => match field {
                Some(field) => match object.fields.get(field) {
                    Some(ty) => {
                        let ty = ty.clone();
                        self.read_field(of, field);
                        ty
                    }
                    None => return Err(self.at(key.id, no_field(object, field))),
                },
                None => Ty::Dyn,
            },
            Ty::Map(_) | Ty::Object(_) => {
                let fault = Fault::new(format!(
                    "A map's keys are text: `[…]` takes text, not {}.",
                    k.describe()
                ));
                return Err(self.at(key.id, fault));
            }
            Ty::Dyn => Ty::Dyn,
            other => {
                let fault = Fault::new(format!(
                    "{} can't be indexed with `[…]`.",
                    capitalized(&other.describe())
                ));
                let fault = if matches!(other, Ty::Str(_)) {
                    fault.hint("Check text with `startsWith(…)`, `endsWith(…)` or `contains(…)`.")
                } else {
                    fault
                };
                return Err(self.at(expr.id, fault));
            }
        };
        if let Some(path) = self.path(expr) {
            self.paths.insert(path);
        }
        Ok(ty)
    }

    fn arithmetic(&mut self, id: u64, op: Op, left: &IdedExpr, right: &IdedExpr) -> Checked {
        let (a, b) = (self.check(left)?, self.check(right)?);
        if let Some(ty) = op.result(&a, &b) {
            return Ok(ty);
        }
        let fault = if op == Op::Rem && (matches!(a, Ty::Double) || matches!(b, Ty::Double)) {
            Fault::new("`%` works on whole numbers only, and this has a number (double).")
                .hint("Convert it with `int(…)` first.")
        } else if a.numeric() && b.numeric() {
            Fault::new(op.refusal(&a, &b)).hint(numbers_hint(&a, &b, left, right))
        } else {
            Fault::new(op.refusal(&a, &b)).maybe(arithmetic_hint(op, &a, &b))
        };
        Err(self.at(id, fault))
    }

    /// The types of a call's arguments.
    fn arguments(&mut self, args: &[IdedExpr]) -> Result<Vec<Ty>, Fault> {
        args.iter().map(|arg| self.check(arg)).collect()
    }

    fn function(&mut self, expr: &IdedExpr, call: &CallExpr) -> Checked {
        let name = call.func_name.as_str();
        match name {
            "matches" => return Err(self.at(expr.id, no_regex())),
            "has" => {
                return Err(self.at(
                    expr.id,
                    Fault::new("`has()` takes one field, as in `has(m.key)`."),
                ));
            }
            name if MACROS.contains(&name) => {
                return Err(self.at(
                    expr.id,
                    Fault::new(format!(
                        "`{name}` is written after a list or a map, as in `items.{name}(x, …)`."
                    )),
                ));
            }
            name if !FUNCTIONS.contains(&name) => {
                let fault = Fault::new(format!("There's no function `{}()`.", short(name)))
                    .maybe(closest(name, FUNCTIONS).map(|f| format!("Did you mean `{f}()`?")));
                return Err(self.at(expr.id, fault));
            }
            _ => {}
        }
        let args = self.arguments(&call.args)?;
        let [arg] = args.as_slice() else {
            let fault = Fault::new(format!("`{name}()` takes one value, not {}.", args.len()));
            return Err(self.at(expr.id, fault));
        };
        let (accepts, result, what): (fn(&Ty) -> bool, Ty, &str) = match name {
            "size" => (sized, Ty::Int, "counts text, lists and maps"),
            "int" => (
                |t| t.numeric() || matches!(t, Ty::Str(_) | Ty::Timestamp),
                Ty::Int,
                "converts numbers, text and times",
            ),
            "uint" | "double" => (
                |t| t.numeric() || matches!(t, Ty::Str(_)),
                if name == "uint" { Ty::UInt } else { Ty::Double },
                "converts numbers and text",
            ),
            "string" => (
                |t| {
                    t.numeric() || matches!(t, Ty::Str(_) | Ty::Bool | Ty::Timestamp | Ty::Duration)
                },
                Ty::Str(None),
                "converts numbers, yes or no, times and durations",
            ),
            "bool" => (
                |t| matches!(t, Ty::Bool | Ty::Str(_)),
                Ty::Bool,
                "converts text, as `bool('true')`",
            ),
            "timestamp" => (
                |t| matches!(t, Ty::Str(_) | Ty::Timestamp | Ty::Int),
                Ty::Timestamp,
                "reads text, as `timestamp('2026-11-18T10:00:00Z')`",
            ),
            "duration" => (
                |t| matches!(t, Ty::Str(_) | Ty::Duration),
                Ty::Duration,
                "reads text, as `duration('8h')`",
            ),
            _ => (|_| true, Ty::Dyn, "takes anything"),
        };
        if !(accepts(arg) || matches!(arg, Ty::Dyn)) {
            let fault = Fault::new(format!("`{name}()` {what}, not {}.", arg.describe()));
            return Err(self.at(expr.id, fault));
        }
        if let Some(text) = call.args.first().and_then(text_literal) {
            self.literal_argument(name, text, &call.args[0])?;
        }
        Ok(result)
    }

    /// Checks the text a literal `timestamp('…')` or `duration('…')` reads, as cel will.
    fn literal_argument(&self, name: &str, text: &str, arg: &IdedExpr) -> Result<(), Fault> {
        let fault = match name {
            "timestamp" if self.probes.timestamp(text).is_none() => {
                Fault::new(format!("`{}` isn't a time.", short(text)))
                    .hint("Write one in RFC 3339, as `2026-11-18T10:00:00Z`.")
            }
            "duration" if self.probes.duration(text).is_none() => {
                Fault::new(format!("`{}` isn't a duration.", short(text)))
                    .hint("Write one as `8h`, `30m`, `1h30m` or `90s`.")
            }
            _ => return Ok(()),
        };
        Err(self.at(arg.id, fault))
    }

    fn method(&mut self, expr: &IdedExpr, call: &CallExpr) -> Checked {
        let name = call.func_name.as_str();
        let target = match &call.target {
            Some(target) => self.check(target)?,
            None => return Err(self.at(expr.id, Fault::new("It isn't a method call."))),
        };
        if name == "matches" {
            return Err(self.at(expr.id, no_regex()));
        }
        if MACROS.contains(&name) {
            let fault = if name == "map" {
                Fault::new(
                    "`map` takes a variable and a value, as in `items.map(x, x.name)`, and may take a condition between them.",
                )
            } else {
                Fault::new(format!(
                    "`{name}` takes a variable and a condition, as in `items.{name}(x, x > 0)`."
                ))
            };
            return Err(self.at(expr.id, fault));
        }
        // Before its arguments: `items.exsts(x, …)` is about `exsts`, not `x`.
        let known = match name {
            "size" => sized(&target) || matches!(target, Ty::Dyn),
            "contains" | "startsWith" | "endsWith" => target.textual(),
            getter if TIMESTAMP_GETTERS.contains(&getter) => {
                matches!(target, Ty::Timestamp | Ty::Dyn)
                    || (matches!(target, Ty::Duration) && DURATION_GETTERS.contains(&getter))
            }
            _ => false,
        };
        if !known {
            return Err(self.at(expr.id, no_method(name, &target)));
        }
        let args = self.arguments(&call.args)?;
        let arity = |count: usize| {
            let wanted = match count {
                0 => "no values".to_owned(),
                1 => "one value".to_owned(),
                n => format!("{n} values"),
            };
            Fault::new(format!("`{name}()` takes {wanted}, not {}.", args.len()))
        };
        match (name, &target) {
            ("size", t) if sized(t) || matches!(t, Ty::Dyn) => {
                if args.is_empty() {
                    Ok(Ty::Int)
                } else {
                    Err(self.at(expr.id, arity(0)))
                }
            }
            ("contains" | "startsWith" | "endsWith", t) if t.textual() => match args.as_slice() {
                [arg] if arg.textual() => Ok(Ty::Bool),
                [arg] => Err(self.at(
                    expr.id,
                    Fault::new(format!("`{name}()` takes text, not {}.", arg.describe())),
                )),
                _ => Err(self.at(expr.id, arity(1))),
            },
            (getter, Ty::Timestamp | Ty::Dyn) if TIMESTAMP_GETTERS.contains(&getter) => {
                self.getter(expr, call, &args)
            }
            (getter, Ty::Duration) if DURATION_GETTERS.contains(&getter) => {
                if args.is_empty() {
                    Ok(Ty::Int)
                } else {
                    Err(self.at(expr.id, arity(0)))
                }
            }
            _ => Err(self.at(expr.id, no_method(name, &target))),
        }
    }

    /// A timestamp's getter. Rules' times are UTC, as a date field's midnight is, so
    /// none takes the time zone cel would take.
    fn getter(&self, expr: &IdedExpr, call: &CallExpr, args: &[Ty]) -> Checked {
        let name = call.func_name.as_str();
        if args.is_empty() {
            return Ok(Ty::Int);
        }
        let at = call.args.first().map_or(expr.id, |arg| arg.id);
        Err(self.at(
            at,
            Fault::new(format!(
                "`{name}()` takes no time zone: rules' times are in UTC."
            ))
            .hint(format!("Write `{name}()`, which reads the time in UTC.")),
        ))
    }

    fn comprehension(&mut self, id: u64, c: &ComprehensionExpr) -> Checked {
        let Some(shape) = Shape::of(c) else {
            return Err(self.at(id, Fault::new("Rules don't support this kind of loop.")));
        };
        let name = shape.name(&c.iter_var);
        let range = self.check(&c.iter_range)?;
        let item = match &range {
            Ty::List(item) => (**item).clone(),
            Ty::Map(_) | Ty::Object(_) => Ty::Str(None),
            Ty::Dyn => Ty::Dyn,
            other => {
                let fault = Fault::new(format!(
                    "`{name}` goes through a list or a map, and this is {}.",
                    other.describe()
                ));
                return Err(self.at(id, fault));
            }
        };
        self.bound.push((c.iter_var.clone(), item));
        let result = self.body(name, &shape);
        self.bound.pop();
        result
    }

    /// What a macro makes, from the parts of it a person wrote.
    fn body(&mut self, name: &str, shape: &Shape<'_>) -> Checked {
        let condition = |checker: &mut Self, p: &IdedExpr| -> Result<(), Fault> {
            let ty = checker.check(p)?;
            if ty.boolish() {
                return Ok(());
            }
            let fault = Fault::new(format!(
                "The condition in `{name}` must be yes or no, and this is {}.",
                ty.describe()
            ));
            Err(checker.at(p.id, fault))
        };
        match shape {
            Shape::All(p) | Shape::Exists(p) | Shape::ExistsOne(p) => {
                condition(self, p)?;
                Ok(Ty::Bool)
            }
            Shape::Map(value) => Ok(Ty::List(Box::new(self.check(value)?))),
            Shape::MapIf(p, value) => {
                condition(self, p)?;
                Ok(Ty::List(Box::new(self.check(value)?)))
            }
        }
    }
}

/// Whether `size()` counts it.
fn sized(t: &Ty) -> bool {
    matches!(t, Ty::Str(_) | Ty::List(_) | Ty::Map(_) | Ty::Object(_))
}

/// A kind of value, as a list of them is said to hold: "a list of whole numbers (int)".
fn plural(t: &Ty) -> String {
    match t {
        Ty::Bool => "yes-or-no values (bool)".to_owned(),
        Ty::Int => "whole numbers (int)".to_owned(),
        Ty::UInt => "whole numbers (uint)".to_owned(),
        Ty::Double => "numbers (double)".to_owned(),
        Ty::Str(_) => "text (string)".to_owned(),
        Ty::Timestamp => "times (timestamp)".to_owned(),
        Ty::Duration => "durations".to_owned(),
        Ty::Dyn | Ty::Null => "values".to_owned(),
        Ty::List(_) => "lists".to_owned(),
        Ty::Map(_) => "maps".to_owned(),
        Ty::Object(object) => object.label.clone(),
    }
}

/// Why a method doesn't exist on a kind of value, with what it might have been.
fn no_method(name: &str, target: &Ty) -> Fault {
    let methods: Vec<&str> = match target {
        Ty::Str(_) => TEXT_METHODS.to_vec(),
        Ty::List(_) | Ty::Map(_) | Ty::Object(_) => {
            let mut m = vec!["size"];
            m.extend(MACROS);
            m
        }
        Ty::Timestamp => TIMESTAMP_GETTERS.to_vec(),
        Ty::Duration => DURATION_GETTERS.to_vec(),
        Ty::Dyn => {
            let mut m = TEXT_METHODS.to_vec();
            m.extend(TIMESTAMP_GETTERS);
            m.extend(MACROS);
            m
        }
        _ => Vec::new(),
    };
    let reason = if matches!(target, Ty::Dyn) {
        format!("There's no method `{}()`.", short(name))
    } else {
        format!(
            "{} has no method `{}()`.",
            capitalized(&target.describe()),
            short(name)
        )
    };
    let hint = closest(name, methods.iter().copied())
        .map(|m| format!("Did you mean `{m}()`?"))
        .or_else(|| match target {
            Ty::Duration => Some(format!(
                "A duration has {}.",
                listed(DURATION_GETTERS.iter().map(|g| &g[..]))
            )),
            _ => None,
        });
    Fault::new(reason).maybe(hint)
}

/// Why `matches()` isn't there.
fn no_regex() -> Fault {
    Fault::new("`matches()` isn't available: rules have no regular expressions.")
        .hint("A string field's `pattern` checks its format.")
}

/// Why `.?` and `[?` aren't there.
pub(super) fn optional() -> Fault {
    Fault::new("Optional values (`.?` and `[?`) aren't supported in rules.")
        .hint("Check a field has a value with `!= null` instead.")
}
