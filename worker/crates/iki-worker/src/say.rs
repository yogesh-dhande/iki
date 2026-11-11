//! What the worker says about itself, on stderr, one `logfmt` line per event.
//!
//! `level=info msg="Leased a job." job=… run=RUN-000123`: easy to read in a terminal and
//! parsed as-is by Loki, Datadog and the like. The command's own output goes to the
//! server, not here.

use std::fmt::Display;
use std::io::Write;

/// How much an event matters.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    Info,
    Warn,
    Error,
}

impl Level {
    const fn name(self) -> &'static str {
        match self {
            Self::Info => "info",
            Self::Warn => "warn",
            Self::Error => "error",
        }
    }
}

/// One `key=value` pair, quoted when it needs to be.
fn pair(out: &mut String, key: &str, value: &str) {
    out.push(' ');
    out.push_str(key);
    out.push('=');
    let bare = !value.is_empty()
        && value
            .chars()
            .all(|c| !c.is_whitespace() && !c.is_control() && c != '"' && c != '=');
    if bare {
        out.push_str(value);
        return;
    }
    out.push('"');
    for c in value.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c.is_control() => out.push(char::REPLACEMENT_CHARACTER),
            c => out.push(c),
        }
    }
    out.push('"');
}

/// The line for an event, without its newline.
pub fn line(level: Level, message: &str, fields: &[(&str, &dyn Display)]) -> String {
    let mut out = String::from("level=");
    out.push_str(level.name());
    pair(&mut out, "msg", message);
    for (key, value) in fields {
        pair(&mut out, key, &value.to_string());
    }
    out
}

/// Writes an event to stderr. A closed stderr isn't worth stopping a job for.
pub fn event(level: Level, message: &str, fields: &[(&str, &dyn Display)]) {
    let mut text = line(level, message, fields);
    text.push('\n');
    let _ = std::io::stderr().lock().write_all(text.as_bytes());
}

pub fn info(message: &str, fields: &[(&str, &dyn Display)]) {
    event(Level::Info, message, fields);
}

pub fn warn(message: &str, fields: &[(&str, &dyn Display)]) {
    event(Level::Warn, message, fields);
}

pub fn error(message: &str, fields: &[(&str, &dyn Display)]) {
    event(Level::Error, message, fields);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quotes_only_what_needs_it() {
        assert_eq!(
            line(
                Level::Info,
                "Leased a job.",
                &[("job", &"j1"), ("attempt", &2)]
            ),
            r#"level=info msg="Leased a job." job=j1 attempt=2"#
        );
    }

    #[test]
    fn escapes_quotes_and_newlines() {
        assert_eq!(
            line(Level::Warn, "x", &[("detail", &"say \"hi\"\nthen=go")]),
            r#"level=warn msg=x detail="say \"hi\"\nthen=go""#
        );
        assert_eq!(line(Level::Error, "", &[]), r#"level=error msg="""#);
    }
}
