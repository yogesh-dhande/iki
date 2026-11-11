//! Running one leased job: stage it, run its command, stream what it writes, and report
//! how it ended (ADR 0010, ADR 0043).
//!
//! The command runs in a process group of its own, from its plugin's directory, with an
//! environment that holds only what it needs: never the worker's token. It finds its
//! files through `IKI_*` variables:
//!
//! - `IKI_INPUT`: `input.json`, the run input as the server sent it
//! - `IKI_OUTPUT`: where to write `output.json`
//! - `IKI_OUT_DIR`: a directory for files it makes
//! - `TMPDIR` and `HOME`: directories of the job's own, removed afterwards
//!
//! Lines it prints like `::progress 0.4 Folding chain A` report progress.

use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex, PoisonError};
use std::thread;
use std::time::{Duration, Instant};

use iki_protocol::{
    CompleteIn, CompleteOut, FailIn, FailInReason, FailOut, HeartbeatIn, HeartbeatInState,
    HeartbeatOut, LogIn, LogOut, ProgressIn, RunInput, fill, paths,
};
use rustix::process::{Pid, Signal, kill_process_group};

use crate::api::{self, Api, Error};
use crate::config::Config;
use crate::files;
use crate::logs::{self, Log};
use crate::say;

/// The most `output.json` may hold, as the server accepts.
pub const MAX_OUTPUT: u64 = 2_500_000;
/// After SIGTERM, how long the command has to stop before SIGKILL.
pub const STOP_GRACE: Duration = Duration::from_secs(10);
/// The longest a line is read before it's sent as it is.
const MAX_LINE: u64 = 64 * 1024;
/// How long to keep trying to report how a job ended.
const REPORT_FOR: Duration = Duration::from_secs(300);

/// Why the worker stopped a command.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Stop {
    Cancelled,
    TimedOut,
    Interrupted,
    /// The lease went to someone else: nothing to report.
    Lost,
}

/// What became of a job, for the worker's loop.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ended {
    /// Reported (or dropped): lease the next one.
    Done,
    /// The server no longer takes this worker's token: stop.
    Unauthorized,
}

/// What the heartbeat and sender threads learn, for the supervisor to act on.
#[derive(Debug, Default)]
struct Signals {
    cancel: AtomicBool,
    lost: AtomicBool,
    unauthorized: AtomicBool,
    /// The sender stops when this is set, once the command's output is all read.
    output_read: AtomicBool,
    /// Heartbeats stop when this is set, once the end is reported.
    finished: AtomicBool,
}

struct Job {
    api: Api,
    id: String,
    lease_token: String,
    state: Mutex<HeartbeatInState>,
    signals: Signals,
    log: Log,
    progress: Mutex<Option<(f64, String)>>,
}

impl Job {
    fn path(&self, template: &str) -> String {
        fill(template, &[&self.id])
    }

    fn set_state(&self, state: HeartbeatInState) {
        *self.state.lock().unwrap_or_else(PoisonError::into_inner) = state;
    }

    fn note(&self, error: &Error) {
        match error {
            Error::Stale(_) => self.signals.lost.store(true, Ordering::SeqCst),
            Error::Unauthorized(_) => {
                self.signals.unauthorized.store(true, Ordering::SeqCst);
                self.signals.lost.store(true, Ordering::SeqCst);
            }
            Error::Refused { .. } | Error::Transient(_) => {}
        }
    }

    fn heartbeat(&self) -> Result<HeartbeatOut, Error> {
        let state = *self.state.lock().unwrap_or_else(PoisonError::into_inner);
        let body = HeartbeatIn {
            lease_token: self.lease_token.clone(),
            state: Some(state),
        };
        self.api
            .post::<_, HeartbeatOut>(&self.path(paths::JOBS_HEARTBEAT), &body)?
            .ok_or_else(|| Error::Transient("The heartbeat had no answer.".into()))
    }

    /// Sends what's waiting in the log, chunk by chunk. False if it should stop trying.
    fn send_log(&self) -> bool {
        while let Some((offset, text)) = self.log.next_chunk() {
            let size = text.len() as u64;
            let body = LogIn {
                lease_token: self.lease_token.clone(),
                offset: i64::try_from(offset).unwrap_or(i64::MAX),
                text,
            };
            match self
                .api
                .post::<_, LogOut>(&self.path(paths::JOBS_LOGS), &body)
            {
                Ok(Some(LogOut { next_offset })) => {
                    self.log
                        .acknowledge(u64::try_from(next_offset).unwrap_or_default());
                }
                Ok(None) => self.log.acknowledge(offset + size),
                Err(Error::Refused { code, body, .. }) if code == "log_gap" => {
                    // The server has more than this worker knew: carry on from there.
                    let next = body.get("next_offset").and_then(serde_json::Value::as_u64);
                    self.log.acknowledge(next.unwrap_or(offset + size));
                }
                Err(Error::Refused { detail, .. }) => {
                    say::warn(
                        "The server refused part of the log; skipping it.",
                        &[("job", &self.id), ("detail", &detail)],
                    );
                    self.log.acknowledge(offset + size);
                }
                Err(error) => {
                    self.note(&error);
                    return false;
                }
            }
        }
        true
    }

    fn send_progress(&self) {
        let latest = self
            .progress
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        if let Some((fraction, message)) = latest {
            let body = ProgressIn {
                lease_token: self.lease_token.clone(),
                fraction,
                message: Some(message),
            };
            if let Err(error) = self
                .api
                .post::<_, serde_json::Value>(&self.path(paths::JOBS_PROGRESS), &body)
            {
                self.note(&error);
            }
        }
    }

    fn read_output(&self, stream: impl Read) {
        let mut reader = BufReader::new(stream);
        let mut line = Vec::new();
        loop {
            line.clear();
            match reader.by_ref().take(MAX_LINE).read_until(b'\n', &mut line) {
                Ok(0) | Err(_) => return,
                Ok(_) => {
                    let text = String::from_utf8_lossy(&line);
                    if let Some(progress) = logs::progress(&text) {
                        *self.progress.lock().unwrap_or_else(PoisonError::into_inner) =
                            Some(progress);
                    }
                    self.log.push(&text);
                }
            }
        }
    }
}

/// Renews the lease every `every` until the job is finished, noting a cancel or a lost
/// lease for the supervisor.
fn heartbeats(job: &Job, every: Duration) {
    let mut next = Instant::now();
    while !job.signals.finished.load(Ordering::SeqCst) {
        if Instant::now() >= next {
            match job.heartbeat() {
                Ok(beat) => {
                    if beat.cancel_requested {
                        job.signals.cancel.store(true, Ordering::SeqCst);
                    }
                    next = Instant::now() + every;
                }
                Err(Error::Transient(detail)) => {
                    say::warn(
                        "A heartbeat failed; trying again.",
                        &[("job", &job.id), ("detail", &detail)],
                    );
                    next = Instant::now() + every.min(Duration::from_secs(5));
                }
                Err(Error::Refused { detail, .. }) => {
                    say::warn(
                        "The server refused a heartbeat.",
                        &[("job", &job.id), ("detail", &detail)],
                    );
                    next = Instant::now() + every;
                }
                Err(error) => {
                    job.note(&error);
                    return;
                }
            }
        }
        thread::sleep(Duration::from_millis(100));
    }
}

/// What a job's watch answers (`paths::JOBS_WATCH`, an async view the generated types
/// don't cover).
#[derive(Debug, serde::Deserialize)]
struct WatchOut {
    cancel_requested: bool,
}

/// Holds a long-poll that answers the moment the run is cancelled, so the command stops
/// at once rather than at the next heartbeat. A server without it leaves cancels to
/// heartbeats.
fn watcher(job: &Job) {
    let path = format!("{}?wait=25", job.path(paths::JOBS_WATCH));
    let body = serde_json::json!({"lease_token": job.lease_token});
    while !job.signals.finished.load(Ordering::SeqCst) {
        match job.api.post::<_, WatchOut>(&path, &body) {
            Ok(Some(WatchOut {
                cancel_requested: true,
            })) => {
                job.signals.cancel.store(true, Ordering::SeqCst);
                return;
            }
            Ok(_) => {}
            Err(Error::Transient(_)) => thread::sleep(Duration::from_secs(2)),
            Err(Error::Refused { .. }) => return,
            Err(error) => {
                job.note(&error);
                return;
            }
        }
    }
}

/// Sends the log and progress about once a second while the command runs.
fn sender(job: &Job) {
    while !job.signals.output_read.load(Ordering::SeqCst) {
        if !job.send_log() {
            thread::sleep(Duration::from_secs(2));
        }
        job.send_progress();
        for _ in 0..10 {
            if job.signals.output_read.load(Ordering::SeqCst) {
                return;
            }
            thread::sleep(Duration::from_millis(100));
        }
    }
}

/// Where a job's files go.
struct Dirs {
    root: PathBuf,
    input: PathBuf,
    output: PathBuf,
    out: PathBuf,
    tmp: PathBuf,
    home: PathBuf,
    /// Input files, each in a directory named for its ID.
    inputs: PathBuf,
}

impl Dirs {
    fn new(work_dir: &Path, job_id: &str) -> Self {
        let root = work_dir.join("jobs").join(job_id);
        Self {
            input: root.join("input.json"),
            output: root.join("output.json"),
            out: root.join("out"),
            tmp: root.join("tmp"),
            home: root.join("home"),
            inputs: root.join("inputs"),
            root,
        }
    }

    fn create(&self) -> std::io::Result<()> {
        if self.root.exists() {
            std::fs::remove_dir_all(&self.root)?;
        }
        for dir in [&self.root, &self.out, &self.tmp, &self.home, &self.inputs] {
            create_private_dir(dir)?;
        }
        Ok(())
    }
}

/// A directory only this user can read, as job data may be confidential.
fn create_private_dir(dir: &Path) -> std::io::Result<()> {
    let mut builder = std::fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    std::os::unix::fs::DirBuilderExt::mode(&mut builder, 0o700);
    builder.create(dir)
}

/// The command's environment: nothing of the worker's but what programs need to run.
fn environment(dirs: &Dirs, input: &RunInput) -> Vec<(String, String)> {
    const PASSED: &[&str] = &["PATH", "LANG", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR"];
    let mut env: Vec<(String, String)> = PASSED
        .iter()
        .filter_map(|name| {
            std::env::var(name)
                .ok()
                .map(|value| ((*name).to_owned(), value))
        })
        .chain(std::env::vars().filter(|(name, _)| name.starts_with("LC_")))
        .collect();
    if !env.iter().any(|(name, _)| name == "PATH") {
        env.push(("PATH".into(), "/usr/local/bin:/usr/bin:/bin".into()));
    }
    let path = |p: &Path| p.to_string_lossy().into_owned();
    env.extend([
        ("HOME".into(), path(&dirs.home)),
        ("TMPDIR".into(), path(&dirs.tmp)),
        ("IKI_INPUT".into(), path(&dirs.input)),
        ("IKI_OUTPUT".into(), path(&dirs.output)),
        ("IKI_OUT_DIR".into(), path(&dirs.out)),
        ("IKI_RUN_ID".into(), input.run.id.clone()),
        ("IKI_RUN_ATTEMPT".into(), input.run.attempt.to_string()),
        // Output reaches the log as it's written, not when a buffer fills.
        ("PYTHONUNBUFFERED".into(), "1".into()),
    ]);
    env
}

fn spawn(input: &RunInput, plugin_dir: &Path, dirs: &Dirs) -> Result<Child, String> {
    let (program, args) = input
        .command
        .split_first()
        .ok_or("The action has no command.")?;
    // A relative path to a program (`bin/fold`) is the plugin's; a bare name is on PATH.
    let program = if program.contains('/') && !Path::new(program).is_absolute() {
        plugin_dir.join(program)
    } else {
        PathBuf::from(program)
    };
    let mut command = Command::new(&program);
    command
        .args(args)
        .current_dir(plugin_dir)
        .env_clear()
        .envs(environment(dirs, input))
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(unix)]
    std::os::unix::process::CommandExt::process_group(&mut command, 0);
    command
        .spawn()
        .map_err(|error| format!("Couldn't start {}: {error}.", program.display()))
}

/// Signals the command's whole process group, so nothing it started outlives it.
fn signal_group(child: &Child, signal: Signal) {
    if let Some(pid) = i32::try_from(child.id()).ok().and_then(Pid::from_raw) {
        let _ = kill_process_group(pid, signal);
    }
}

/// SIGTERM, then SIGKILL after the grace period if it's still running.
fn stop(child: &mut Child) -> Option<ExitStatus> {
    signal_group(child, Signal::TERM);
    let deadline = Instant::now() + STOP_GRACE;
    while Instant::now() < deadline {
        if let Ok(Some(status)) = child.try_wait() {
            signal_group(child, Signal::KILL);
            return Some(status);
        }
        thread::sleep(Duration::from_millis(100));
    }
    signal_group(child, Signal::KILL);
    child.wait().ok()
}

fn describe(status: ExitStatus) -> String {
    #[cfg(unix)]
    if let Some(signal) = std::os::unix::process::ExitStatusExt::signal(&status) {
        return format!("The command was killed by signal {signal}.");
    }
    match status.code() {
        Some(code) => format!("The command exited with status {code}."),
        None => "The command ended without an exit status.".to_owned(),
    }
}

/// How the attempt ended, to report.
enum Report {
    Fail(FailInReason, String),
    Complete(serde_json::Map<String, serde_json::Value>),
}

fn read_output(path: &Path) -> Result<serde_json::Map<String, serde_json::Value>, String> {
    let size = match std::fs::metadata(path) {
        Ok(meta) => meta.len(),
        Err(_) => return Err("The command exited without writing output.json.".to_owned()),
    };
    if size > MAX_OUTPUT {
        return Err(format!(
            "output.json is {size} bytes, over the limit of {MAX_OUTPUT}. Put large results in files."
        ));
    }
    let text =
        std::fs::read(path).map_err(|error| format!("Couldn't read output.json: {error}."))?;
    match serde_json::from_slice::<serde_json::Value>(&text) {
        Ok(serde_json::Value::Object(map)) => Ok(map),
        Ok(_) => Err("output.json must hold a JSON object (run-output/v1).".to_owned()),
        Err(error) => Err(format!("output.json isn't valid JSON: {error}.")),
    }
}

fn clip(text: &str, max: usize) -> String {
    if text.chars().count() <= max {
        return text.to_owned();
    }
    let mut out: String = text.chars().take(max - 1).collect();
    out.push('…');
    out
}

/// Runs a job the worker just leased, from the run input as the server sent it.
pub fn run(api: &Api, config: &Config, interrupted: &AtomicBool, raw: &serde_json::Value) -> Ended {
    let input: RunInput = match serde_json::from_value(raw.clone()) {
        Ok(input) => input,
        Err(error) => return unreadable(api, raw, &error.to_string()),
    };
    let job = Arc::new(Job {
        api: api.clone(),
        id: input.job.id.clone(),
        lease_token: input.job.lease_token.clone(),
        state: Mutex::new(HeartbeatInState::Staging),
        signals: Signals::default(),
        log: Log::default(),
        progress: Mutex::new(None),
    });
    say::info(
        "Leased a job.",
        &[
            ("job", &job.id),
            ("run", &input.run.hrid),
            ("action", &input.run.action),
            ("attempt", &input.run.attempt),
        ],
    );
    let every = Duration::from_secs(u64::try_from(input.job.heartbeat_every.max(1)).unwrap_or(20));
    let beating = {
        let job = Arc::clone(&job);
        thread::spawn(move || heartbeats(&job, every))
    };
    {
        // Not joined: it ends with its long-poll, up to half a minute after the job.
        let job = Arc::clone(&job);
        thread::spawn(move || watcher(&job));
    }
    let dirs = Dirs::new(&config.work_dir, &job.id);
    let report = execute(&job, &input, config, &dirs, raw, interrupted);
    let ended = finish(&job, report);
    job.signals.finished.store(true, Ordering::SeqCst);
    let _ = beating.join();
    if !config.keep_job_dirs
        && let Err(error) = std::fs::remove_dir_all(&dirs.root)
        && error.kind() != std::io::ErrorKind::NotFound
    {
        say::warn(
            "Couldn't remove a job's directory.",
            &[("path", &dirs.root.display()), ("detail", &error)],
        );
    }
    ended
}

/// Waits for the command to end, or stops it: when its lease is lost, the worker is
/// stopping, the run is cancelled, or it runs past `deadline`.
fn supervise(
    job: &Job,
    child: &mut Child,
    interrupted: &AtomicBool,
    deadline: Instant,
) -> (Option<ExitStatus>, Option<Stop>) {
    loop {
        if let Ok(Some(status)) = child.try_wait() {
            // Anything it left running in its group goes with it.
            signal_group(child, Signal::KILL);
            return (Some(status), None);
        }
        let why = if job.signals.lost.load(Ordering::SeqCst) {
            Some(Stop::Lost)
        } else if interrupted.load(Ordering::SeqCst) {
            Some(Stop::Interrupted)
        } else if job.signals.cancel.load(Ordering::SeqCst) {
            Some(Stop::Cancelled)
        } else if Instant::now() >= deadline {
            Some(Stop::TimedOut)
        } else {
            None
        };
        if let Some(why) = why {
            say::info(
                "Stopping the command.",
                &[("job", &job.id), ("why", &format!("{why:?}"))],
            );
            return (stop(child), Some(why));
        }
        thread::sleep(Duration::from_millis(100));
    }
}

/// Downloads the input files, then writes input.json, saying where they are.
fn stage_input(
    job: &Job,
    input: &RunInput,
    dirs: &Dirs,
    raw: &serde_json::Value,
) -> Result<(), files::Failed> {
    let mut written = raw.clone();
    if let Some(wanted) = input.files.as_ref().filter(|f| !f.is_empty()) {
        let lease = files::Lease {
            api: &job.api,
            job: &job.id,
            token: &job.lease_token,
        };
        let staged = files::stage(&lease, &files::storage_agent(), wanted, &dirs.inputs)?;
        say::info(
            "Staged the input files.",
            &[("job", &job.id), ("files", &staged.len())],
        );
        written["files"] = serde_json::to_value(staged)
            .map_err(|e| files::Failed::Job(format!("Couldn't write input.json: {e}")))?;
    }
    let bytes = serde_json::to_vec_pretty(&written)
        .map_err(|e| files::Failed::Job(format!("Couldn't write input.json: {e}")))?;
    std::fs::write(&dirs.input, bytes)
        .map_err(|e| files::Failed::Job(format!("Couldn't write input.json: {e}.")))
}

/// Uploads the files output.json names with `$file`, putting their IDs in its place.
fn upload_results(
    job: &Job,
    dirs: &Dirs,
    output: &mut serde_json::Map<String, serde_json::Value>,
) -> Result<(), files::Failed> {
    let lease = files::Lease {
        api: &job.api,
        job: &job.id,
        token: &job.lease_token,
    };
    let count = files::upload_results(&lease, &files::storage_agent(), &dirs.out, output)?;
    if count > 0 {
        say::info(
            "Uploaded the result files.",
            &[("job", &job.id), ("files", &count)],
        );
    }
    Ok(())
}

/// Stages the job and runs its command. `None` if the lease was lost.
fn execute(
    job: &Arc<Job>,
    input: &RunInput,
    config: &Config,
    dirs: &Dirs,
    raw: &serde_json::Value,
    interrupted: &AtomicBool,
) -> Option<Report> {
    let plugin_dir = config.plugins_dir.join(&input.run.plugin.name);
    if !plugin_dir.is_dir() {
        return Some(Report::Fail(
            FailInReason::Failed,
            format!(
                "This worker has no code for the plugin {} (looked in {}).",
                input.run.plugin.name,
                plugin_dir.display()
            ),
        ));
    }
    if let Err(error) = dirs.create() {
        let message = format!("Couldn't stage the job: {error}.");
        return Some(Report::Fail(FailInReason::Failed, message));
    }
    match stage_input(job, input, dirs, raw) {
        Ok(()) => {}
        Err(files::Failed::Lost(error)) => {
            job.note(&error);
            return None;
        }
        Err(files::Failed::Job(message)) => {
            return Some(Report::Fail(FailInReason::Failed, message));
        }
    }
    job.set_state(HeartbeatInState::Running);
    let mut child = match spawn(input, &plugin_dir, dirs) {
        Ok(child) => child,
        Err(message) => return Some(Report::Fail(FailInReason::Failed, message)),
    };
    let (done, readers_done) = mpsc::channel::<()>();
    for stream in [
        child
            .stdout
            .take()
            .map(|s| Box::new(s) as Box<dyn Read + Send>),
        child
            .stderr
            .take()
            .map(|s| Box::new(s) as Box<dyn Read + Send>),
    ]
    .into_iter()
    .flatten()
    {
        let (job, done) = (Arc::clone(job), done.clone());
        thread::spawn(move || {
            job.read_output(stream);
            let _ = done.send(());
        });
    }
    drop(done);
    let sending = {
        let job = Arc::clone(job);
        thread::spawn(move || sender(&job))
    };

    let timeout = Duration::from_secs(u64::try_from(input.job.timeout.max(1)).unwrap_or(3600));
    let deadline = Instant::now() + timeout;
    let (status, stopped) = supervise(job, &mut child, interrupted, deadline);
    // The readers end when the pipes close; one a stray process holds open isn't waited on.
    let wait_until = Instant::now() + Duration::from_secs(5);
    while readers_done
        .recv_timeout(wait_until.saturating_duration_since(Instant::now()))
        .is_ok()
    {}
    job.signals.output_read.store(true, Ordering::SeqCst);
    let _ = sending.join();

    outcome(job, dirs, status, stopped, timeout)
}

/// What to report, once the command has ended. `None` if the lease was lost.
fn outcome(
    job: &Job,
    dirs: &Dirs,
    status: Option<ExitStatus>,
    stopped: Option<Stop>,
    timeout: Duration,
) -> Option<Report> {
    Some(match stopped {
        Some(Stop::Lost) => return None,
        Some(Stop::Cancelled) => Report::Fail(
            FailInReason::Cancelled,
            "Stopped: the run was cancelled.".into(),
        ),
        Some(Stop::Interrupted) => Report::Fail(
            FailInReason::Interrupted,
            "Stopped: the worker was shut down.".into(),
        ),
        Some(Stop::TimedOut) => Report::Fail(
            FailInReason::TimedOut,
            format!(
                "Stopped: it ran past its timeout of {} second{}.",
                timeout.as_secs(),
                if timeout.as_secs() == 1 { "" } else { "s" }
            ),
        ),
        None => match status {
            Some(status) if status.success() => {
                job.set_state(HeartbeatInState::Uploading);
                match read_output(&dirs.output) {
                    Ok(mut output) => match upload_results(job, dirs, &mut output) {
                        Ok(()) => Report::Complete(output),
                        Err(files::Failed::Lost(error)) => {
                            job.note(&error);
                            return None;
                        }
                        Err(files::Failed::Job(message)) => {
                            Report::Fail(FailInReason::Failed, message)
                        }
                    },
                    Err(message) => Report::Fail(FailInReason::Failed, message),
                }
            }
            Some(status) => Report::Fail(FailInReason::Failed, describe(status)),
            None => Report::Fail(FailInReason::Failed, "Lost track of the command.".into()),
        },
    })
}

/// Sends the rest of the log and how the attempt ended.
fn finish(job: &Job, report: Option<Report>) -> Ended {
    let deadline = Instant::now() + REPORT_FOR;
    let unauthorized = || {
        if job.signals.unauthorized.load(Ordering::SeqCst) {
            Ended::Unauthorized
        } else {
            Ended::Done
        }
    };
    let Some(report) = report else {
        say::warn(
            "The job's lease went elsewhere; dropped it.",
            &[("job", &job.id)],
        );
        return unauthorized();
    };
    let _ = api::retrying(deadline, || {
        if job.send_log() || job.signals.lost.load(Ordering::SeqCst) {
            Ok(())
        } else {
            Err(Error::Transient("The rest of the log didn't go.".into()))
        }
    });
    job.send_progress();
    if job.signals.lost.load(Ordering::SeqCst) {
        say::warn(
            "The job's lease went elsewhere; dropped it.",
            &[("job", &job.id)],
        );
        return unauthorized();
    }
    let result = match report {
        Report::Fail(reason, message) => {
            let body = FailIn {
                lease_token: job.lease_token.clone(),
                reason,
                message: Some(clip(&message, 2000)),
            };
            api::retrying(deadline, || {
                job.api
                    .post::<_, FailOut>(&job.path(paths::JOBS_FAIL), &body)
            })
            .map(|out| {
                let status = out.map(|o| o.run_status).unwrap_or_default();
                say::info(
                    "Reported the job ended without results.",
                    &[
                        ("job", &job.id),
                        ("reason", &format!("{reason:?}")),
                        ("detail", &message),
                        ("run_status", &status),
                    ],
                );
            })
        }
        Report::Complete(output) => {
            let body = CompleteIn {
                lease_token: job.lease_token.clone(),
                output,
            };
            api::retrying(deadline, || {
                job.api
                    .post::<_, CompleteOut>(&job.path(paths::JOBS_COMPLETE), &body)
            })
            .map(|out| {
                let out = out.unwrap_or(CompleteOut {
                    code: None,
                    errors: None,
                    message: None,
                    run_status: String::new(),
                });
                say::info(
                    "Reported the job's results.",
                    &[
                        ("job", &job.id),
                        ("run_status", &out.run_status),
                        ("detail", &out.message.unwrap_or_default()),
                    ],
                );
            })
        }
    };
    if let Err(error) = result {
        job.note(&error);
        say::error(
            "Couldn't report how the job ended.",
            &[("job", &job.id), ("detail", &error)],
        );
    }
    unauthorized()
}

/// The server sent a job this worker can't read: say so, so the run doesn't wait for a
/// lease to lapse. The worker is probably older than the server.
fn unreadable(api: &Api, raw: &serde_json::Value, error: &str) -> Ended {
    let field = |key: &str| {
        raw.pointer(&format!("/job/{key}"))
            .and_then(serde_json::Value::as_str)
    };
    say::error(
        "Couldn't read a leased job; is iki-worker out of date?",
        &[("detail", &error)],
    );
    let (Some(id), Some(token)) = (field("id"), field("lease_token")) else {
        return Ended::Done;
    };
    let body = FailIn {
        lease_token: token.to_owned(),
        reason: FailInReason::Failed,
        message: Some(clip(
            &format!(
                "iki-worker {} couldn't read this job: {error}",
                env!("CARGO_PKG_VERSION")
            ),
            2000,
        )),
    };
    match api.post::<_, FailOut>(&fill(paths::JOBS_FAIL, &[id]), &body) {
        Err(Error::Unauthorized(_)) => Ended::Unauthorized,
        _ => Ended::Done,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clips_long_messages() {
        assert_eq!(clip("short", 10), "short");
        assert_eq!(clip("abcdef", 4), "abc…");
    }

    #[test]
    fn output_must_be_a_small_json_object() {
        let dir = std::env::temp_dir().join(format!("iki-worker-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("output.json");
        assert!(read_output(&path).unwrap_err().contains("without writing"));
        std::fs::write(&path, "[1]").unwrap();
        assert!(read_output(&path).unwrap_err().contains("JSON object"));
        std::fs::write(&path, "{").unwrap();
        assert!(read_output(&path).unwrap_err().contains("isn't valid JSON"));
        std::fs::write(&path, r#"{"apiVersion": "iki/v1"}"#).unwrap();
        assert_eq!(read_output(&path).unwrap()["apiVersion"], "iki/v1");
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
