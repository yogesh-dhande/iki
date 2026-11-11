//! The `iki-worker` binary: leases jobs from an Iki server and runs them (ADR 0010).
//!
//! `iki-worker start` registers with `IKI_REGISTRATION_TOKEN` and runs jobs until it's
//! stopped. Everything else it needs comes from the environment too; see `config`.

mod api;
mod config;
mod files;
mod job;
mod logs;
mod say;
mod worker;

use std::io::{self, Write};
use std::process::ExitCode;

/// What the command line asked for.
#[derive(Debug, PartialEq, Eq)]
enum Command {
    Start { once: bool },
    Version,
    Help,
    Unknown(String),
}

fn parse(args: &[String]) -> Command {
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    match args.as_slice() {
        ["start"] => Command::Start { once: false },
        ["start", "--once"] => Command::Start { once: true },
        ["--version" | "-V"] => Command::Version,
        [] | ["--help" | "-h"] => Command::Help,
        [first, ..] => Command::Unknown((*first).to_owned()),
    }
}

fn version_line() -> String {
    format!(
        "iki-worker {} (protocol v{})",
        env!("CARGO_PKG_VERSION"),
        iki_protocol::PROTOCOL_VERSION
    )
}

const USAGE: &str = "\
Usage: iki-worker start [--once]   Lease jobs and run them until stopped (or one job)
       iki-worker --version

Set in the environment:
  IKI_URL                  The server, such as https://iki.example.com
  IKI_REGISTRATION_TOKEN   From Compute settings, to register with
  IKI_WORKER_STATE         A file to keep the worker's own token in across restarts
  IKI_PLUGINS_DIR          Each plugin's code, in a directory named for it
  IKI_WORK_DIR             Where jobs run (a temporary directory unless set)
  IKI_WORKER_NAME          How people tell it apart (the host name unless set)
  IKI_WORKER_CPU, IKI_WORKER_MEMORY, IKI_WORKER_GPUS, IKI_WORKER_GPU_MEMORY,
  IKI_WORKER_SCRATCH_DISK  What one job may use (CPU and memory are detected on Linux)
  HTTPS_PROXY, NO_PROXY    A proxy to reach the server through
  SSL_CERT_FILE            Extra CA certificates, if the system's don't cover the server";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match parse(&args) {
        Command::Start { once } => {
            let started = config::Config::from_env(&|name| std::env::var(name).ok())
                .and_then(|config| worker::start(&config, once));
            match started {
                Ok(()) => ExitCode::SUCCESS,
                Err(message) => {
                    say::error(&message, &[]);
                    ExitCode::FAILURE
                }
            }
        }
        Command::Version => write_or_fail(&mut io::stdout(), &version_line(), ExitCode::SUCCESS),
        Command::Help => write_or_fail(&mut io::stdout(), USAGE, ExitCode::SUCCESS),
        Command::Unknown(arg) => write_or_fail(
            &mut io::stderr(),
            &format!("iki-worker: unknown argument '{arg}'\n{USAGE}"),
            ExitCode::from(2),
        ),
    }
}

fn write_or_fail(out: &mut impl Write, text: &str, code: ExitCode) -> ExitCode {
    writeln!(out, "{text}").map_or(ExitCode::FAILURE, |()| code)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn parses_commands() {
        assert_eq!(parse(&args(&["start"])), Command::Start { once: false });
        assert_eq!(
            parse(&args(&["start", "--once"])),
            Command::Start { once: true }
        );
        assert_eq!(parse(&args(&["--version"])), Command::Version);
        assert_eq!(parse(&args(&["-V"])), Command::Version);
        assert_eq!(parse(&[]), Command::Help);
        assert_eq!(parse(&args(&["--nope"])), Command::Unknown("--nope".into()));
        assert_eq!(
            parse(&args(&["start", "--twice"])),
            Command::Unknown("start".into())
        );
    }

    #[test]
    fn version_line_includes_protocol_version() {
        assert_eq!(
            version_line(),
            format!("iki-worker {} (protocol v1)", env!("CARGO_PKG_VERSION"))
        );
    }
}
