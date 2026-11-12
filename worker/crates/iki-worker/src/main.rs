//! The `iki-worker` binary: leases jobs from an Iki server and runs them (ADR 0010).
//!
//! `iki-worker start` registers with `IKI_REGISTRATION_TOKEN` and runs jobs until it's
//! stopped. Everything else it needs comes from the environment too; see `config`.
//! On someone's own computer, `iki-worker login <server>` signs it in as them instead
//! (ADR 0058), and `iki-worker logout` signs it out.
//! `iki-worker run --local input.json` runs one action's command offline, the same way.

mod api;
mod config;
mod files;
mod job;
mod local;
mod login;
mod logs;
mod plugins;
mod say;
mod worker;

use std::io::{self, Write};
use std::path::PathBuf;
use std::process::ExitCode;

/// What the command line asked for.
#[derive(Debug, PartialEq, Eq)]
enum Command {
    Start { once: bool },
    Login { url: String, open_browser: bool },
    Logout,
    RunLocal(local::Options),
    Version,
    Help,
    Unknown(String),
}

fn parse(args: &[String]) -> Command {
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    match args.as_slice() {
        ["start"] => Command::Start { once: false },
        ["start", "--once"] => Command::Start { once: true },
        ["run", rest @ ..] => parse_run(rest),
        ["login", url] | ["login", url, "--no-browser"] | ["login", "--no-browser", url]
            if !url.starts_with('-') =>
        {
            Command::Login {
                url: (*url).to_owned(),
                open_browser: !args.contains(&"--no-browser"),
            }
        }
        ["login", ..] => Command::Unknown(
            "login needs your Iki's address, such as https://iki.example.com".into(),
        ),
        ["logout"] => Command::Logout,
        ["--version" | "-V"] => Command::Version,
        [] | ["--help" | "-h"] => Command::Help,
        [first, ..] => Command::Unknown(format!("unknown argument '{first}'")),
    }
}

/// `run --local <input.json> [--plugin DIR] [--timeout SECONDS]`, in any order.
fn parse_run(args: &[&str]) -> Command {
    let mut local = false;
    let mut input = None;
    let mut plugin_dir = None;
    let mut timeout = None;
    let mut rest = args.iter();
    while let Some(arg) = rest.next() {
        match *arg {
            "--local" => local = true,
            "--plugin" => match rest.next() {
                Some(dir) => plugin_dir = Some(PathBuf::from(dir)),
                None => return Command::Unknown("--plugin needs a directory".into()),
            },
            "--timeout" => match rest.next().and_then(|s| s.parse::<u64>().ok()) {
                Some(seconds) if seconds > 0 => timeout = Some(seconds),
                _ => return Command::Unknown("--timeout needs a number of seconds".into()),
            },
            flag if flag.starts_with('-') => {
                return Command::Unknown(format!("unknown option '{flag}'"));
            }
            path if input.is_none() => input = Some(PathBuf::from(path)),
            extra => return Command::Unknown(format!("unexpected argument '{extra}'")),
        }
    }
    match (local, input) {
        (false, _) => Command::Unknown(
            "run needs --local: it runs an input.json on this machine, with no server".into(),
        ),
        (true, None) => Command::Unknown("run --local needs an input.json".into()),
        (true, Some(input)) => Command::RunLocal(local::Options {
            input,
            plugin_dir,
            timeout,
        }),
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
Usage: iki-worker login <server> [--no-browser]
                                   Sign in on your own computer, to run your client-tier
                                   runs (opens your browser to confirm a code)
       iki-worker logout           Sign this computer's worker out
       iki-worker start [--once]   Lease jobs and run them until stopped (or one job)
       iki-worker run --local <input.json> [--plugin DIR] [--timeout SECONDS]
                                   Run one action's command here, with no server: its
                                   output goes to stdout, what it prints to stderr.
                                   DIR is the plugin's (the current directory unless set).
       iki-worker --version

Set in the environment:
  IKI_URL                  The server, such as https://iki.example.com (once signed
                           in with login, the one you signed in to)
  IKI_REGISTRATION_TOKEN   From Compute settings, to register a deployment's worker with
  IKI_WORKER_STATE         A file to keep the worker's own token in across restarts
                           (after login, ~/.config/iki/worker.json unless set)
  IKI_PLUGINS_DIR          Each plugin's code, in a directory named for it
  IKI_WORK_DIR             Where jobs run (a temporary directory unless set)
  IKI_WORKER_NAME          How people tell it apart (the host name unless set)
  IKI_WORKER_CPU, IKI_WORKER_MEMORY, IKI_WORKER_GPUS, IKI_WORKER_GPU_MEMORY,
  IKI_WORKER_SCRATCH_DISK  What one job may use (CPU and memory are detected on Linux)
  HTTPS_PROXY, NO_PROXY    A proxy to reach the server through
  SSL_CERT_FILE            Extra CA certificates, if the system's don't cover the server
  IKI_ALLOW_HTTP           1 allows a plain http:// IKI_URL anywhere: development clusters only";

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
        Command::Login { url, open_browser } => finish(sign_in(&url, open_browser)),
        Command::Logout => finish(
            state_file(&|name| std::env::var(name).ok())
                .ok_or_else(|| NO_STATE.to_owned())
                .and_then(|path| login::logout(&path)),
        ),
        Command::RunLocal(options) => {
            let work_dir = std::env::var_os("IKI_WORK_DIR")
                .map_or_else(|| std::env::temp_dir().join("iki-worker"), PathBuf::from);
            match local::run(&options, &work_dir) {
                Ok(local::Ran::Succeeded) => ExitCode::SUCCESS,
                Ok(local::Ran::Failed) => ExitCode::FAILURE,
                Ok(local::Ran::Interrupted) => ExitCode::from(130),
                Err(message) => {
                    say::error(&message, &[]);
                    ExitCode::from(2)
                }
            }
        }
        Command::Version => write_or_fail(&mut io::stdout(), &version_line(), ExitCode::SUCCESS),
        Command::Help => write_or_fail(&mut io::stdout(), USAGE, ExitCode::SUCCESS),
        Command::Unknown(arg) => write_or_fail(
            &mut io::stderr(),
            &format!("iki-worker: {arg}\n{USAGE}"),
            ExitCode::from(2),
        ),
    }
}

const NO_STATE: &str = "Set IKI_WORKER_STATE to where the worker's token is kept: there's \
    no home directory to keep it in.";

/// Where the signed-in worker's token is: `IKI_WORKER_STATE`, or the user's configuration.
fn state_file(var: &dyn Fn(&str) -> Option<String>) -> Option<PathBuf> {
    var("IKI_WORKER_STATE")
        .filter(|v| !v.trim().is_empty())
        .map(|v| PathBuf::from(v.trim()))
        .or_else(|| login::default_state_file(var))
}

fn sign_in(url: &str, open_browser: bool) -> Result<(), String> {
    let var = |name: &str| std::env::var(name).ok();
    let allow_http = var("IKI_ALLOW_HTTP").is_some_and(|v| v.trim() == "1" || v.trim() == "true");
    login::login(&login::Options {
        url: config::server_url(url, allow_http)?,
        state_file: state_file(&var).ok_or_else(|| NO_STATE.to_owned())?,
        name: config::name(&var),
        capacity: config::capacity(&var)?,
        open_browser,
    })
}

fn finish(done: Result<(), String>) -> ExitCode {
    match done {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => write_or_fail(
            &mut io::stderr(),
            &format!("iki-worker: {message}"),
            ExitCode::FAILURE,
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
        assert_eq!(
            parse(&args(&["--nope"])),
            Command::Unknown("unknown argument '--nope'".into())
        );
        assert_eq!(
            parse(&args(&["start", "--twice"])),
            Command::Unknown("unknown argument 'start'".into())
        );
    }

    #[test]
    fn parses_login_and_logout() {
        let url = "https://iki.example.com";
        assert_eq!(
            parse(&args(&["login", url])),
            Command::Login {
                url: url.into(),
                open_browser: true
            }
        );
        for order in [
            ["login", url, "--no-browser"],
            ["login", "--no-browser", url],
        ] {
            assert_eq!(
                parse(&args(&order)),
                Command::Login {
                    url: url.into(),
                    open_browser: false
                }
            );
        }
        assert!(matches!(parse(&args(&["login"])), Command::Unknown(_)));
        assert!(matches!(
            parse(&args(&["login", "--twice"])),
            Command::Unknown(_)
        ));
        assert_eq!(parse(&args(&["logout"])), Command::Logout);
    }

    #[test]
    fn parses_local_runs() {
        assert_eq!(
            parse(&args(&["run", "--local", "in.json"])),
            Command::RunLocal(local::Options {
                input: "in.json".into(),
                plugin_dir: None,
                timeout: None,
            })
        );
        assert_eq!(
            parse(&args(&[
                "run",
                "in.json",
                "--timeout",
                "60",
                "--local",
                "--plugin",
                "p"
            ])),
            Command::RunLocal(local::Options {
                input: "in.json".into(),
                plugin_dir: Some("p".into()),
                timeout: Some(60),
            })
        );
        let refused = |list: &[&str]| matches!(parse(&args(list)), Command::Unknown(_));
        assert!(refused(&["run", "in.json"]));
        assert!(refused(&["run", "--local"]));
        assert!(refused(&["run", "--local", "a.json", "b.json"]));
        assert!(refused(&["run", "--local", "a.json", "--timeout", "0"]));
        assert!(refused(&["run", "--local", "a.json", "--timeout"]));
        assert!(refused(&["run", "--local", "a.json", "--plugin"]));
        assert!(refused(&["run", "--local", "a.json", "--twice"]));
    }

    #[test]
    fn version_line_includes_protocol_version() {
        assert_eq!(
            version_line(),
            format!("iki-worker {} (protocol v1)", env!("CARGO_PKG_VERSION"))
        );
    }
}
