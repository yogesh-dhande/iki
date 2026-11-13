//! The `iki-worker` binary: leases jobs from an Iki server and runs them (ADR 0010).
//!
//! `iki-worker start` registers with `IKI_REGISTRATION_TOKEN` and runs jobs until it's
//! stopped. Everything else it needs comes from the environment too; see `config`.
//! On someone's own computer, `iki-worker login <server>` signs it in as them instead
//! (ADR 0058), and `iki-worker logout` signs it out.
//! `iki-worker run --local input.json` runs one action's command offline, the same way.
//! `iki-worker host config.json` runs a cloud machine's run container (ADR 0074).

mod api;
mod between;
mod bundles;
mod checkpoint;
mod config;
mod files;
mod globs;
mod host;
mod job;
mod local;
mod login;
mod logs;
mod plugins;
mod say;
mod shared;
mod update;
mod worker;

use std::io::{self, Write};
use std::path::PathBuf;
use std::process::ExitCode;

/// What the command line asked for.
#[derive(Debug, PartialEq, Eq)]
enum Command {
    Start {
        once: bool,
    },
    Login {
        url: String,
        open_browser: bool,
    },
    Logout,
    Verify {
        file: PathBuf,
        bundle: PathBuf,
        identity: String,
    },
    RunLocal(local::Options),
    Install(PathBuf),
    Prepare {
        root: PathBuf,
        directories: Vec<String>,
    },
    Remove {
        root: PathBuf,
        directories: Vec<String>,
    },
    Host(PathBuf),
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
        ["install", dir] => Command::Install(PathBuf::from(dir)),
        ["prepare", root, directories @ ..] if !directories.is_empty() => Command::Prepare {
            root: PathBuf::from(root),
            directories: directories.iter().map(|d| (*d).to_owned()).collect(),
        },
        ["prepare", ..] => {
            Command::Unknown("prepare needs a root and the directories under it to make".into())
        }
        ["remove", root, directories @ ..] if !directories.is_empty() => Command::Remove {
            root: PathBuf::from(root),
            directories: directories.iter().map(|d| (*d).to_owned()).collect(),
        },
        ["remove", ..] => {
            Command::Unknown("remove needs a root and the directories under it to remove".into())
        }
        ["host", config] if !config.starts_with('-') => Command::Host(PathBuf::from(config)),
        ["host", ..] => Command::Unknown("host needs the machine's config.json".into()),
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
        ["verify", file, bundle, "--identity", identity] => Command::Verify {
            file: PathBuf::from(file),
            bundle: PathBuf::from(bundle),
            identity: (*identity).to_owned(),
        },
        ["verify", file, bundle, "--version", version] => match update::REPOSITORY {
            Some(repository) => Command::Verify {
                file: PathBuf::from(file),
                bundle: PathBuf::from(bundle),
                identity: update::identity(repository, version),
            },
            None => Command::Unknown(
                "this iki-worker was built from source: give --identity instead of --version"
                    .into(),
            ),
        },
        ["verify", ..] => Command::Unknown(
            "verify needs a release, its .sigstore.json, and --version or --identity".into(),
        ),
        ["--version" | "-V"] => Command::Version,
        [] | ["--help" | "-h"] => Command::Help,
        [first, ..] => Command::Unknown(format!("unknown argument '{first}'")),
    }
}

/// `run --local <input.json> [--plugin DIR] [--timeout SECONDS] [--checkpoint-dir DIR]`,
/// in any order.
fn parse_run(args: &[&str]) -> Command {
    let mut local = false;
    let mut input = None;
    let mut plugin_dir = None;
    let mut timeout = None;
    let mut checkpoint_dir = None;
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
            "--checkpoint-dir" => match rest.next() {
                Some(dir) => checkpoint_dir = Some(PathBuf::from(dir)),
                None => return Command::Unknown("--checkpoint-dir needs a directory".into()),
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
            checkpoint_dir,
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
       iki-worker verify <release.tar.gz> <release.tar.gz.sigstore.json> --version V
                                   Check a release was signed by Iki's release workflow at
                                   that version (offline), as updating does
       iki-worker start [--once]   Lease jobs and run them until stopped (or one job)
       iki-worker run --local <input.json> [--plugin DIR] [--timeout SECONDS]
                              [--checkpoint-dir DIR]
                                   Run one action's command here, with no server: its
                                   output goes to stdout, what it prints to stderr.
                                   --plugin is the plugin's directory (the current one
                                   unless set). --checkpoint-dir gives a checkpointable
                                   action that directory as IKI_CHECKPOINT_DIR and keeps
                                   it, so running again resumes from what it saved.
       iki-worker install <dir>    Copy this binary into <dir>, for a run's pod to run it
                                   in a plugin's image (the container tier does this)
       iki-worker prepare <root> <dir>...
                                   Make versions' directories on a shared file system,
                                   under <root> (a setup build's pod does this)
       iki-worker remove <root> <dir>...
                                   Delete versions of a volume from a shared file
                                   system, under <root> (the provisioner does this)
       iki-worker host <config.json>
                                   Run a cloud machine's run container from the config
                                   its boot script wrote, then shut the machine down
                                   (the VM tier does this)
       iki-worker --version

Set in the environment:
  IKI_URL                  The server, such as https://iki.example.com (once signed
                           in with login, the one you signed in to)
  IKI_REGISTRATION_TOKEN   From Compute settings, to register a deployment's worker with
  IKI_WORKER_STATE         A file to keep the worker's own token in across restarts
                           (after login, ~/.config/iki/worker.json unless set)
  IKI_PLUGINS_DIR          Each plugin's code, in a directory named for it. Unless set,
                           the worker fetches the server's and builds their environments
                           with uv or micromamba
  IKI_WORKER_CACHE         Where it keeps what it fetched and built (~/.cache/iki/worker)
  IKI_WORK_DIR             Where jobs run (a temporary directory unless set)
  IKI_WORKER_NAME          How people tell it apart (the host name unless set)
  IKI_WORKER_CPU, IKI_WORKER_MEMORY, IKI_WORKER_GPUS, IKI_WORKER_GPU_MEMORY,
  IKI_WORKER_SCRATCH_DISK  What one job may use (CPU and memory are detected on Linux)
  HTTPS_PROXY, NO_PROXY    A proxy to reach the server through
  SSL_CERT_FILE            Extra CA certificates, if the system's don't cover the server
  IKI_WORKER_AUTO_UPDATE   0 leaves updates to whoever installed it, rather than updating
                           to the release the server says while idle
  IKI_WORKER_IDLE_EXIT     Stop after this many seconds without a job
  IKI_WORKER_CLEAN_DIR     A directory that holds only what jobs leave (a pod's scratch
                           volume): emptied after each job, so the next starts clean
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
        Command::Verify {
            file,
            bundle,
            identity,
        } => finish(
            std::fs::read_to_string(&bundle)
                .map_err(|e| format!("Couldn't read {}: {e}", bundle.display()))
                .and_then(|text| update::verify(&file, &text, &identity))
                .map(|()| {
                    let _ = writeln!(io::stdout(), "{} is signed by {identity}.", file.display());
                })
                .map_err(|e| format!("{}: {e}", file.display())),
        ),
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
        Command::Install(dir) => finish(install(&dir)),
        Command::Prepare { root, directories } => finish(shared::prepare(&root, &directories)),
        Command::Remove { root, directories } => finish(shared::remove(&root, &directories)),
        Command::Host(config) => finish(host::host(&config)),
        Command::Version => write_or_fail(&mut io::stdout(), &version_line(), ExitCode::SUCCESS),
        Command::Help => write_or_fail(&mut io::stdout(), USAGE, ExitCode::SUCCESS),
        Command::Unknown(arg) => write_or_fail(
            &mut io::stderr(),
            &format!("iki-worker: {arg}\n{USAGE}"),
            ExitCode::from(2),
        ),
    }
}

/// Copies this binary into `dir` as `iki-worker`, executable, in one step: a run's init
/// container does this, so the run's own image needs nothing of Iki's (ADR 0061).
fn install(dir: &std::path::Path) -> Result<(), String> {
    let exe =
        std::env::current_exe().map_err(|e| format!("Couldn't tell where iki-worker is: {e}"))?;
    let target = dir.join("iki-worker");
    let partial = dir.join(".iki-worker.partial");
    let copy = || -> std::io::Result<()> {
        std::fs::copy(&exe, &partial)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&partial, std::fs::Permissions::from_mode(0o755))?;
        }
        std::fs::rename(&partial, &target)
    };
    copy().map_err(|e| format!("Couldn't copy iki-worker to {}: {e}", target.display()))?;
    say::info("Installed iki-worker.", &[("path", &target.display())]);
    Ok(())
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
        assert_eq!(
            parse(&args(&["host", "/etc/iki/host.json"])),
            Command::Host(PathBuf::from("/etc/iki/host.json"))
        );
        assert!(matches!(parse(&args(&["host"])), Command::Unknown(_)));
        assert_eq!(
            parse(&args(&["prepare", "/iki/shared", "env/v/SETUP-000001"])),
            Command::Prepare {
                root: PathBuf::from("/iki/shared"),
                directories: vec!["env/v/SETUP-000001".into()],
            }
        );
        assert_eq!(
            parse(&args(&["remove", "/iki/shared", "a", "b"])),
            Command::Remove {
                root: PathBuf::from("/iki/shared"),
                directories: vec!["a".into(), "b".into()],
            }
        );
        assert!(matches!(
            parse(&args(&["prepare", "/iki/shared"])),
            Command::Unknown(_)
        ));
        assert!(matches!(
            parse(&args(&["remove", "/iki/shared"])),
            Command::Unknown(_)
        ));
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
                checkpoint_dir: None,
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
                "p",
                "--checkpoint-dir",
                "saved"
            ])),
            Command::RunLocal(local::Options {
                input: "in.json".into(),
                plugin_dir: Some("p".into()),
                timeout: Some(60),
                checkpoint_dir: Some("saved".into()),
            })
        );
        let refused = |list: &[&str]| matches!(parse(&args(list)), Command::Unknown(_));
        assert!(refused(&["run", "in.json"]));
        assert!(refused(&["run", "--local"]));
        assert!(refused(&["run", "--local", "a.json", "b.json"]));
        assert!(refused(&["run", "--local", "a.json", "--timeout", "0"]));
        assert!(refused(&["run", "--local", "a.json", "--timeout"]));
        assert!(refused(&["run", "--local", "a.json", "--plugin"]));
        assert!(refused(&["run", "--local", "a.json", "--checkpoint-dir"]));
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
