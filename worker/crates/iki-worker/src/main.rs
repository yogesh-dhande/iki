//! The `iki-worker` binary.
//!
//! Job leasing and execution land with the job queue. For now the binary only
//! reports its version and the protocol version it speaks.

use std::io::{self, Write};
use std::process::ExitCode;

/// What the command line asked for.
#[derive(Debug, PartialEq, Eq)]
enum Command {
    Version,
    Help,
    Unknown(String),
}

fn parse(args: &[String]) -> Command {
    match args.first().map(String::as_str) {
        Some("--version" | "-V") => Command::Version,
        None | Some("--help" | "-h") => Command::Help,
        Some(other) => Command::Unknown(other.to_owned()),
    }
}

fn version_line() -> String {
    format!(
        "iki-worker {} (protocol v{})",
        env!("CARGO_PKG_VERSION"),
        iki_protocol::PROTOCOL_VERSION
    )
}

const USAGE: &str = "Usage: iki-worker [--version | --help]";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (mut out, mut err) = (io::stdout().lock(), io::stderr().lock());
    let result = match parse(&args) {
        Command::Version => writeln!(out, "{}", version_line()).map(|()| ExitCode::SUCCESS),
        Command::Help => writeln!(out, "{USAGE}").map(|()| ExitCode::SUCCESS),
        Command::Unknown(arg) => writeln!(err, "iki-worker: unknown argument '{arg}'\n{USAGE}")
            .map(|()| ExitCode::from(2)),
    };
    result.unwrap_or(ExitCode::FAILURE)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn parses_version_flags() {
        assert_eq!(parse(&args(&["--version"])), Command::Version);
        assert_eq!(parse(&args(&["-V"])), Command::Version);
    }

    #[test]
    fn no_arguments_shows_help() {
        assert_eq!(parse(&[]), Command::Help);
    }

    #[test]
    fn rejects_unknown_arguments() {
        assert_eq!(parse(&args(&["--nope"])), Command::Unknown("--nope".into()));
    }

    #[test]
    fn version_line_includes_protocol_version() {
        assert_eq!(
            version_line(),
            format!("iki-worker {} (protocol v1)", env!("CARGO_PKG_VERSION"))
        );
    }
}
