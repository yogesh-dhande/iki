//! `iki-worker slurm-step <dir>`: what a Slurm job runs on its compute node (ADR 0133).
//!
//! The login node's worker staged the job into `<dir>` on the cluster's shared file
//! system, with `step.json` saying what to run, and `secrets.json` the secrets and proxy
//! credentials it alone may have. This reads both, deletes `secrets.json` so nothing else
//! of the job's reads it later, and runs the command in the job's image under Apptainer:
//! with only the job's directory bound in, none of the host's environment, home or
//! `/tmp`, and the job's own variables. Apptainer takes this process's place, so Slurm
//! sees the command's exit status and signals it directly.

use std::path::{Path, PathBuf};
use std::process::Command;

use serde::{Deserialize, Serialize};

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

/// What `secrets.json` holds: the variables only the command may see.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Secrets {
    pub env: Vec<(String, String)>,
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

/// Reads the step, and its secrets once, then becomes Apptainer. Returns only if it
/// couldn't.
pub fn run(dir: &Path) -> String {
    let read = || -> Result<(Step, Secrets), String> {
        let text = std::fs::read(dir.join(STEP))
            .map_err(|e| format!("Couldn't read {}: {e}", dir.join(STEP).display()))?;
        let step: Step = serde_json::from_slice(&text).map_err(|e| format!("{STEP}: {e}"))?;
        let secrets = match std::fs::read(dir.join(SECRETS)) {
            Ok(text) => {
                let _ = std::fs::remove_file(dir.join(SECRETS));
                serde_json::from_slice(&text).map_err(|e| format!("{SECRETS}: {e}"))?
            }
            // A job Slurm started again (it isn't requeued, but an admin may) has none.
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Secrets::default(),
            Err(e) => return Err(format!("Couldn't read {SECRETS}: {e}")),
        };
        Ok((step, secrets))
    };
    let (step, secrets) = match read() {
        Ok(read) => read,
        Err(error) => return error,
    };
    let (args, env) = apptainer(&step, &secrets);
    let mut command = Command::new(&step.apptainer);
    command.args(&args).env_clear().envs(env);
    #[cfg(unix)]
    {
        let error = std::os::unix::process::CommandExt::exec(&mut command);
        format!("Couldn't start {}: {error}", step.apptainer)
    }
    #[cfg(not(unix))]
    {
        let _ = command;
        "Slurm jobs run only on Unix.".to_owned()
    }
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
        let error = run(&dir);
        assert!(error.starts_with("Couldn't start"), "{error}");
        assert!(!dir.join(SECRETS).exists());
    }
}
