//! `iki-worker run --local input.json`: runs an action's command on this machine, with
//! no server, through the same code as a leased job (ADR 0046). For plugin authors, and
//! for `iki plugin test`.
//!
//! The input is a run input (`run-input/v1`), and may leave out `job`, the lease. Each
//! of its `files` gives a `path`, relative to input.json. Its `name`, `media_type`,
//! `size` and `sha256` are filled in from the file when left out, and checked when
//! given, so a fixture can't drift from the file it describes.
//!
//! What the command prints goes to stderr as it prints it. If it succeeds, its output
//! goes to stdout, with each `{"$file": …}` checked as a leased job's would be and
//! described with the file's size, SHA-256 and path. The job's directory is kept, to
//! look at.

use std::collections::BTreeMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use iki_protocol::{FailInReason, RunInput, RunInputFile};

use crate::files;
use crate::job::{self, Dirs, Host, Report};
use crate::say;

/// What `run --local` was asked to do.
#[derive(Debug, PartialEq, Eq)]
pub struct Options {
    pub input: PathBuf,
    /// The plugin's directory. The current directory unless given.
    pub plugin_dir: Option<PathBuf>,
    /// Seconds. The input's `job.timeout`, or an hour, unless given.
    pub timeout: Option<u64>,
}

/// How a local run ended.
#[derive(Debug, PartialEq, Eq)]
pub enum Ran {
    Succeeded,
    Failed,
    Interrupted,
}

/// The longest a local run may go without `--timeout` or a `job.timeout`.
const DEFAULT_TIMEOUT: u64 = 3600;

struct Local {
    id: String,
}

impl Host for Local {
    fn id(&self) -> &str {
        &self.id
    }

    fn stage(
        &self,
        wanted: &BTreeMap<String, RunInputFile>,
        dir: &Path,
    ) -> Result<BTreeMap<String, RunInputFile>, files::Failed> {
        files::copy_in(wanted, dir).map_err(files::Failed::Job)
    }

    fn results(
        &self,
        out_dir: &Path,
        output: &mut serde_json::Map<String, serde_json::Value>,
    ) -> Result<usize, files::Failed> {
        files::describe_results(out_dir, output)
    }

    fn results_done(&self) -> &'static str {
        "Checked the result files."
    }

    fn line(&self, text: &str) {
        let mut err = std::io::stderr().lock();
        let _ = err.write_all(text.as_bytes());
        if !text.ends_with('\n') {
            let _ = err.write_all(b"\n");
        }
    }
}

/// Fills in what a local input may leave out: the lease, and what each file's own bytes
/// say about it. Each file's `path` becomes absolute, for staging to copy from.
fn complete(
    raw: &mut serde_json::Value,
    base: &Path,
    id: &str,
    timeout: Option<u64>,
) -> Result<(), String> {
    let object = raw
        .as_object_mut()
        .ok_or("input.json must hold a JSON object (run-input/v1).")?;
    let job = object.entry("job").or_insert_with(|| {
        serde_json::json!({
            "id": id,
            "lease_token": "",
            "lease_expires_at": "",
            "heartbeat_every": 20,
            "timeout": DEFAULT_TIMEOUT,
            "resources": {},
        })
    });
    if let (Some(seconds), Some(job)) = (timeout, job.as_object_mut()) {
        job.insert("timeout".into(), seconds.into());
    }
    if let Some(entries) = object.get_mut("files").and_then(|f| f.as_object_mut()) {
        for (file_id, entry) in entries.iter_mut() {
            let entry = entry
                .as_object_mut()
                .ok_or_else(|| format!("input.json's file {file_id} must be an object."))?;
            files::describe_local(entry, base)
                .map_err(|detail| format!("input.json's file {file_id}: {detail}"))?;
        }
    }
    Ok(())
}

/// Runs `input.json` locally, as `options` says, with jobs' directories in `work_dir`.
/// An `Err` is for an input that can't be run at all.
pub fn run(options: &Options, work_dir: &Path) -> Result<Ran, String> {
    let interrupted = Arc::new(AtomicBool::new(false));
    for signal in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        signal_hook::flag::register(signal, Arc::clone(&interrupted))
            .map_err(|error| format!("Couldn't watch for signals: {error}"))?;
    }
    let shown = options.input.display();
    let text = std::fs::read(&options.input)
        .map_err(|error| format!("Couldn't read {shown}: {error}."))?;
    let mut raw: serde_json::Value = serde_json::from_slice(&text)
        .map_err(|error| format!("{shown} isn't valid JSON: {error}."))?;
    let base = options
        .input
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."))
        .to_path_buf();
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis());
    let id = format!("local-{millis}-{}", std::process::id());
    complete(&mut raw, &base, &id, options.timeout)?;
    let input: RunInput = serde_json::from_value(raw.clone())
        .map_err(|error| format!("{shown} isn't a run input (run-input/v1): {error}."))?;

    let plugin_dir = match &options.plugin_dir {
        Some(dir) => dir.clone(),
        None => std::env::current_dir()
            .map_err(|error| format!("Couldn't tell the current directory: {error}."))?,
    };
    if !plugin_dir.is_dir() {
        return Err(format!(
            "There's no plugin directory at {}.",
            plugin_dir.display()
        ));
    }
    let timeout =
        Duration::from_secs(u64::try_from(input.job.timeout.max(1)).unwrap_or(DEFAULT_TIMEOUT));
    let dirs = Dirs::new(work_dir, &id);
    say::info(
        "Running the command locally.",
        &[
            ("action", &input.run.action),
            ("plugin_dir", &plugin_dir.display()),
            ("job_dir", &dirs.root.display()),
        ],
    );
    let host = Arc::new(Local { id });
    let report = job::execute(
        &host,
        &input,
        &plugin_dir,
        &dirs,
        &raw,
        &interrupted,
        timeout,
    );
    Ok(match report {
        Some(Report::Complete(output)) => {
            let text = serde_json::to_string_pretty(&output)
                .map_err(|error| format!("Couldn't write the output: {error}."))?;
            let mut out = std::io::stdout().lock();
            writeln!(out, "{text}")
                .map_err(|error| format!("Couldn't write the output: {error}."))?;
            say::info(
                "The command succeeded.",
                &[("job_dir", &dirs.root.display())],
            );
            Ran::Succeeded
        }
        Some(Report::Fail(reason, message)) => {
            say::error(
                &message,
                &[
                    ("reason", &format!("{reason:?}")),
                    ("job_dir", &dirs.root.display()),
                ],
            );
            if reason == FailInReason::Interrupted {
                Ran::Interrupted
            } else {
                Ran::Failed
            }
        }
        // Only a lease can be lost, and a local run has none.
        None => Ran::Failed,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fills_in_the_lease_and_what_files_say_about_themselves() {
        let dir = std::env::temp_dir().join(format!("iki-local-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("fixtures")).unwrap();
        std::fs::write(dir.join("fixtures/abl.pdb"), b"abc").unwrap();
        let mut raw = serde_json::json!({
            "files": {"f1": {"path": "fixtures/abl.pdb"}},
        });
        complete(&mut raw, &dir, "local-1", Some(30)).unwrap();
        assert_eq!(raw["job"]["id"], "local-1");
        assert_eq!(raw["job"]["timeout"], 30);
        let file = &raw["files"]["f1"];
        assert_eq!(file["name"], "abl.pdb");
        assert_eq!(file["media_type"], "chemical/x-pdb");
        assert_eq!(file["size"], 3);
        assert_eq!(
            file["sha256"],
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert!(Path::new(file["path"].as_str().unwrap()).is_absolute());

        let mut missing = serde_json::json!({"files": {"f1": {"name": "x.pdb"}}});
        let error = complete(&mut missing, &dir, "local-2", None).unwrap_err();
        assert!(error.contains("f1") && error.contains("path"), "{error}");
        let mut absent = serde_json::json!({"files": {"f1": {"path": "nope.pdb"}}});
        assert!(complete(&mut absent, &dir, "local-3", None).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
