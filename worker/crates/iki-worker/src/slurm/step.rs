//! `iki-worker slurm-step <dir>`: what a Slurm job runs on its compute node (ADR 0133).
//!
//! The login node's worker staged the job into `<dir>` on the cluster's shared file
//! system, with `step.json` saying what to run, and `secrets.json` the secrets and proxy
//! credentials it alone may have. This reads both, deletes `secrets.json` so nothing else
//! of the job's reads it later, and runs the command in the job's image under Apptainer:
//! with only the job's directory bound in, none of the host's environment, home or
//! `/tmp`, and the job's own variables.
//!
//! What the command prints goes to the job's log with its secrets and credentials hidden
//! (ADR 0156), so the log file never holds them: the worker can read it again from any
//! point, after a restart, without them. This stays Apptainer's parent to do so: it passes
//! on the signals Slurm sends, and exits as Apptainer did, so Slurm sees the command's
//! exit status.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus};
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use serde::{Deserialize, Serialize};

use crate::job;
use crate::logs::Mask;

/// What `step.json` says to run.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Step {
    /// Apptainer, as the login node found it: the same path on the compute nodes.
    pub apptainer: String,
    /// The image's SIF file.
    pub image: PathBuf,
    /// The job's directory, bound in at the same path.
    pub dir: PathBuf,
    /// Where the command runs: its plugin's code, in the job's directory.
    pub workdir: PathBuf,
    /// Whether to give it the node's NVIDIA GPUs (`--nv`).
    pub gpus: bool,
    pub program: String,
    pub args: Vec<String>,
    /// Its environment, but for its secrets.
    pub env: Vec<(String, String)>,
}

/// What `secrets.json` holds: the variables only the command may see, and what to hide
/// in what it prints.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Secrets {
    pub env: Vec<(String, String)>,
    /// Its secrets' values and the attempt's credentials (`job::hidden`).
    #[serde(default)]
    pub mask: Vec<String>,
}

pub const STEP: &str = "step.json";
pub const SECRETS: &str = "secrets.json";

/// Apptainer's arguments, and the environment it's given, for `step`.
pub fn apptainer(step: &Step, secrets: &Secrets) -> (Vec<String>, Vec<(String, String)>) {
    let path = |p: &Path| p.to_string_lossy().into_owned();
    // Apptainer sets HOME only from --home: it refuses APPTAINERENV_HOME.
    let home = step.env.iter().find(|(name, _)| name == "HOME");
    let mut args = vec![
        "--quiet".to_owned(),
        "exec".to_owned(),
        // Nothing of the host's: its environment, home, /tmp, devices or PID space.
        "--containall".to_owned(),
        "--cleanenv".to_owned(),
    ];
    match home {
        // The job's own home, in its directory, rather than the service account's.
        Some((_, dir)) => args.extend(["--home".to_owned(), dir.clone()]),
        None => args.push("--no-home".to_owned()),
    }
    args.extend([
        "--bind".to_owned(),
        path(&step.dir),
        "--pwd".to_owned(),
        path(&step.workdir),
    ]);
    if step.gpus {
        args.push("--nv".to_owned());
    }
    args.push(path(&step.image));
    args.push(step.program.clone());
    args.extend(step.args.iter().cloned());
    // Apptainer gives the command each APPTAINERENV_ variable, without its prefix.
    let mut env: Vec<(String, String)> = std::env::var("PATH")
        .ok()
        .map(|p| ("PATH".to_owned(), p))
        .into_iter()
        .collect();
    env.extend(
        step.env
            .iter()
            .filter(|(name, _)| name != "HOME")
            .chain(&secrets.env)
            .map(|(name, value)| (format!("APPTAINERENV_{name}"), value.clone())),
    );
    (args, env)
}

/// The signals Slurm sends a job's processes that the command should get too: to stop
/// (`scancel`, its time limit) or as a warning (`--signal`).
const PASSED_ON: [i32; 6] = [
    signal_hook::consts::SIGTERM,
    signal_hook::consts::SIGINT,
    signal_hook::consts::SIGHUP,
    signal_hook::consts::SIGQUIT,
    signal_hook::consts::SIGUSR1,
    signal_hook::consts::SIGUSR2,
];

/// Reads the step, and its secrets once, then runs Apptainer, writing what it prints to
/// this process's output with `mask` applied. Returns how Apptainer exited.
pub fn run(dir: &Path) -> Result<ExitStatus, String> {
    let text = std::fs::read(dir.join(STEP))
        .map_err(|e| format!("Couldn't read {}: {e}", dir.join(STEP).display()))?;
    let step: Step = serde_json::from_slice(&text).map_err(|e| format!("{STEP}: {e}"))?;
    let secrets: Secrets = match std::fs::read(dir.join(SECRETS)) {
        Ok(text) => {
            let _ = std::fs::remove_file(dir.join(SECRETS));
            serde_json::from_slice(&text).map_err(|e| format!("{SECRETS}: {e}"))?
        }
        // A job Slurm started again (it isn't requeued, but an admin may) has none.
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Secrets::default(),
        Err(e) => return Err(format!("Couldn't read {SECRETS}: {e}")),
    };
    let (args, env) = apptainer(&step, &secrets);
    let mask = Mask::new(secrets.mask.iter().map(String::as_str));
    drop(secrets);
    let mut command = Command::new(&step.apptainer);
    command.args(&args).env_clear().envs(env);
    run_masked(&mut command, mask, std::io::stdout())
        .map_err(|e| format!("Couldn't run {}: {e}", step.apptainer))
}

/// Runs `command` with what it prints, out and error alike, in the order it prints it,
/// written to `out` with `mask` applied. Signals Slurm sends this process reach it too.
fn run_masked(
    command: &mut Command,
    mask: Mask,
    mut out: impl Write + Send + 'static,
) -> std::io::Result<ExitStatus> {
    // Registered first: a signal from now on goes to the command, not to this process.
    let mut signals = signal_hook::iterator::Signals::new(PASSED_ON)?;
    let (reader, writer) = std::io::pipe()?;
    command.stdout(writer.try_clone()?).stderr(writer);
    let spawned = command.spawn();
    // Its copies of the pipe's writing end: the reader sees the end once the command's
    // are closed.
    command.stdout(std::process::Stdio::null());
    command.stderr(std::process::Stdio::null());
    let mut child = spawned?;
    let handle = signals.handle();
    let pid = i32::try_from(child.id())
        .ok()
        .and_then(rustix::process::Pid::from_raw);
    let passing = thread::spawn(move || {
        for signal in signals.forever() {
            if let (Some(pid), Some(signal)) =
                (pid, rustix::process::Signal::from_named_raw(signal))
            {
                let _ = rustix::process::kill_process(pid, signal);
            }
        }
    });
    let (done, finished) = mpsc::channel::<()>();
    thread::spawn(move || {
        job::each_line(reader, |line| {
            let text = String::from_utf8_lossy(line);
            let _ = out.write_all(mask.apply(&text).as_bytes());
            let _ = out.flush();
        });
        let _ = done.send(());
    });
    let status = child.wait();
    // The reader ends when the pipe closes; one a stray process holds open isn't waited on.
    let _ = finished.recv_timeout(Duration::from_secs(5));
    handle.close();
    let _ = passing.join();
    status
}

/// Exits as `status` says the command did: with its code, or by its signal.
pub fn exit_as(status: ExitStatus) -> std::process::ExitCode {
    #[cfg(unix)]
    if let Some(signal) = std::os::unix::process::ExitStatusExt::signal(&status) {
        let _ = signal_hook::low_level::emulate_default_handler(signal);
        return std::process::ExitCode::from(128 + u8::try_from(signal).unwrap_or(0));
    }
    std::process::ExitCode::from(
        status
            .code()
            .and_then(|code| u8::try_from(code).ok())
            .unwrap_or(1),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_command_runs_in_its_image_with_only_its_own_directory_and_variables() {
        let step = Step {
            apptainer: "/usr/bin/apptainer".into(),
            image: "/scratch/iki/images/sha256-c.sif".into(),
            dir: "/scratch/iki/jobs/0190".into(),
            workdir: "/scratch/iki/jobs/0190/plugin".into(),
            gpus: true,
            program: "colabfold_batch".into(),
            args: vec!["--input".into(), "protein.fasta".into()],
            env: vec![
                (
                    "IKI_INPUT".into(),
                    "/scratch/iki/jobs/0190/input.json".into(),
                ),
                ("HOME".into(), "/scratch/iki/jobs/0190/home".into()),
            ],
        };
        let secrets = Secrets {
            env: vec![("UNIPROT_TOKEN".into(), "s3cret".into())],
            mask: vec!["s3cret".into()],
        };
        let (args, env) = apptainer(&step, &secrets);
        assert_eq!(
            args,
            [
                "--quiet",
                "exec",
                "--containall",
                "--cleanenv",
                "--home",
                "/scratch/iki/jobs/0190/home",
                "--bind",
                "/scratch/iki/jobs/0190",
                "--pwd",
                "/scratch/iki/jobs/0190/plugin",
                "--nv",
                "/scratch/iki/images/sha256-c.sif",
                "colabfold_batch",
                "--input",
                "protein.fasta",
            ]
        );
        let names: Vec<&str> = env.iter().map(|(n, _)| n.as_str()).collect();
        assert!(names.contains(&"APPTAINERENV_IKI_INPUT"));
        assert!(names.contains(&"APPTAINERENV_UNIPROT_TOKEN"));
        assert!(!names.contains(&"APPTAINERENV_HOME"));
        assert!(
            names
                .iter()
                .all(|n| *n == "PATH" || n.starts_with("APPTAINERENV_"))
        );
    }

    #[test]
    fn what_the_command_prints_reaches_the_log_with_its_secrets_hidden() {
        // Its output goes to the job's log file, as Slurm's does.
        let out = std::env::temp_dir().join(format!("iki-step-out-{}", std::process::id()));
        let file = std::fs::File::create(&out).unwrap();
        let script = "echo \"token: $TOKEN\"; echo \"lease: $LEASE\" >&2; echo done; exit 7";
        let mask = Mask::new(["s3cr3t-token", "lease-0133"]);
        let mut command = Command::new("/bin/sh");
        command
            .args(["-c", script])
            .env("TOKEN", "s3cr3t-token")
            .env("LEASE", "lease-0133");
        let status = run_masked(&mut command, mask, file).unwrap();
        assert_eq!(status.code(), Some(7));
        let log = std::fs::read_to_string(&out).unwrap();
        assert_eq!(log, "token: ***\nlease: ***\ndone\n");
        std::fs::remove_file(&out).unwrap();
    }

    #[test]
    fn secrets_are_read_once() {
        let dir = std::env::temp_dir().join(format!("iki-step-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let step = Step {
            apptainer: dir.join("no-such-apptainer").to_string_lossy().into_owned(),
            image: dir.join("image.sif"),
            dir: dir.clone(),
            workdir: dir.clone(),
            gpus: false,
            program: "true".into(),
            args: vec![],
            env: vec![],
        };
        std::fs::write(dir.join(STEP), serde_json::to_vec(&step).unwrap()).unwrap();
        std::fs::write(dir.join(SECRETS), br#"{"env": [["TOKEN", "x"]]}"#).unwrap();
        let error = run(&dir).unwrap_err();
        assert!(error.starts_with("Couldn't run"), "{error}");
        assert!(!dir.join(SECRETS).exists());
    }
}
