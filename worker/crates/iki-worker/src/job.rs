//! Running one leased job: stage it, run its command, stream what it writes, and report
//! how it ended (ADR 0010, ADR 0043). `run --local` runs a command through the same
//! `execute`, with a `Host` of its own in place of the server (`local`).
//!
//! The command runs in a process group of its own, from its plugin's directory, with an
//! environment that holds only what it needs: never the worker's token. It finds its
//! files through `IKI_*` variables:
//!
//! - `IKI_INPUT`: `input.json`, the run input as the server sent it
//! - `IKI_OUTPUT`: where to write `output.json`
//! - `IKI_OUT_DIR`: a directory for files it makes
//! - `TMPDIR` and `HOME`: directories of the job's own, removed afterwards
//! - `IKI_SEED`: a seed for its random numbers, given again when it's reproduced
//! - `IKI_CHECKPOINT_DIR`: for a `checkpointable` action, where it saves its progress. The
//!   worker saves it every so often and restores it for a later attempt (`checkpoint`)
//!
//! Lines it prints like `::progress 0.4 Folding chain A` report progress.

use std::collections::BTreeMap;
use std::fmt::Write as _;
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
    HeartbeatOut, LogIn, LogOut, ProgressIn, RunInput, RunInputFile,
    RunInputJobCheckpointSaveOnItem as SaveOn, RunInputJobCheckpointSaved, fill, paths,
};
use rustix::process::{Pid, Signal, kill_process_group};

use crate::api::{self, Api, Error};
use crate::checkpoint;
use crate::config::Config;
use crate::files;
use crate::logs::{self, Log};
use crate::plugins::{self, Built, Kind};
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
pub enum Stop {
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

/// What differs between a leased job and one run with `run --local`: where input files
/// come from, where results and what the command prints go, and what else may stop it.
/// Everything else, from the job's directory to how it ended, is the same code.
pub trait Host: Send + Sync + 'static {
    /// The job's ID, for its directory and what the worker says.
    fn id(&self) -> &str;
    /// Puts the input files in `dir` and returns them with their paths.
    fn stage(
        &self,
        wanted: &BTreeMap<String, RunInputFile>,
        dir: &Path,
    ) -> Result<BTreeMap<String, RunInputFile>, files::Failed>;
    /// Puts what the results should hold in place of each `$file`. Returns how many.
    fn results(
        &self,
        out_dir: &Path,
        output: &mut serde_json::Map<String, serde_json::Value>,
    ) -> Result<usize, files::Failed>;
    /// What to say once `results` has placed some files.
    fn results_done(&self) -> &'static str {
        "Uploaded the result files."
    }
    /// Keeps a file the command wrote as an artifact; `relative` is its path in the output
    /// directory.
    fn keep(&self, relative: &str, path: &Path) -> Result<(), files::Failed>;
    /// What to say once `keep` has kept some artifacts.
    fn kept(&self) -> &'static str {
        "Kept"
    }
    /// A line the command printed.
    fn line(&self, text: &str);
    /// Where the job has got to.
    fn state(&self, _state: HeartbeatInState) {}
    /// Why to stop the command, besides the worker stopping or its timeout.
    fn stop_for(&self) -> Option<Stop> {
        None
    }
    /// A transfer found the lease gone.
    fn lost(&self, _error: &Error) {}
    /// Unpacks the checkpoint an earlier attempt saved into `dir`, using `packed` for its
    /// bytes, and records that the attempt resumed from it.
    fn restore(
        &self,
        _saved: &RunInputJobCheckpointSaved,
        _packed: &Path,
        _dir: &Path,
    ) -> Result<(), files::Failed> {
        Err(files::Failed::Job(
            "there's nowhere to restore it from".into(),
        ))
    }
    /// Saves a packed checkpoint.
    fn save(&self, _packed: &checkpoint::Packed) -> Result<(), files::Failed> {
        Ok(())
    }
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

    fn lease(&self) -> files::Lease<'_> {
        files::Lease {
            api: &self.api,
            job: &self.id,
            token: &self.lease_token,
        }
    }
}

impl Host for Job {
    fn id(&self) -> &str {
        &self.id
    }

    fn stage(
        &self,
        wanted: &BTreeMap<String, RunInputFile>,
        dir: &Path,
    ) -> Result<BTreeMap<String, RunInputFile>, files::Failed> {
        files::stage(&self.lease(), &files::storage_agent(), wanted, dir)
    }

    fn results(
        &self,
        out_dir: &Path,
        output: &mut serde_json::Map<String, serde_json::Value>,
    ) -> Result<usize, files::Failed> {
        files::upload_results(&self.lease(), &files::storage_agent(), out_dir, output)
    }

    fn keep(&self, relative: &str, path: &Path) -> Result<(), files::Failed> {
        files::upload_artifact(&self.lease(), &files::storage_agent(), relative, path)
    }

    fn line(&self, text: &str) {
        if let Some(progress) = logs::progress(text) {
            *self.progress.lock().unwrap_or_else(PoisonError::into_inner) = Some(progress);
        }
        self.log.push(text);
    }

    fn state(&self, state: HeartbeatInState) {
        *self.state.lock().unwrap_or_else(PoisonError::into_inner) = state;
    }

    fn stop_for(&self) -> Option<Stop> {
        if self.signals.lost.load(Ordering::SeqCst) {
            Some(Stop::Lost)
        } else if self.signals.cancel.load(Ordering::SeqCst) {
            Some(Stop::Cancelled)
        } else {
            None
        }
    }

    fn lost(&self, error: &Error) {
        self.note(error);
    }

    fn restore(
        &self,
        saved: &RunInputJobCheckpointSaved,
        packed: &Path,
        dir: &Path,
    ) -> Result<(), files::Failed> {
        files::download_checkpoint(&self.lease(), &files::storage_agent(), saved, packed)?;
        let unpacked = checkpoint::unpack(packed, dir);
        let _ = std::fs::remove_file(packed);
        unpacked.map_err(|e| files::Failed::Job(format!("Couldn't unpack it: {e}")))?;
        files::restored_checkpoint(&self.lease(), &saved.id)
    }

    fn save(&self, packed: &checkpoint::Packed) -> Result<(), files::Failed> {
        files::upload_checkpoint(&self.lease(), &files::storage_agent(), packed)
    }
}

/// Reads what the command prints, line by line, for its host.
fn read_lines(
    host: &impl Host,
    stream: impl Read,
    said: &Mutex<Option<String>>,
    mask: &logs::Mask,
) {
    let mut reader = BufReader::new(stream);
    let mut line = Vec::new();
    loop {
        line.clear();
        match reader.by_ref().take(MAX_LINE).read_until(b'\n', &mut line) {
            Ok(0) | Err(_) => return,
            Ok(_) => {
                let raw = String::from_utf8_lossy(&line);
                let text = mask.apply(&raw);
                if let Some(why) = logs::error(&text) {
                    *said.lock().unwrap_or_else(PoisonError::into_inner) = Some(why);
                }
                host.line(&text);
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
pub struct Dirs {
    pub root: PathBuf,
    input: PathBuf,
    output: PathBuf,
    out: PathBuf,
    tmp: PathBuf,
    home: PathBuf,
    /// Input files, each in a directory named for its ID.
    inputs: PathBuf,
    /// For a checkpointable action: where its command saves progress (ADR 0069). Inside
    /// the job's directory, unless a local run names one of its own to keep.
    pub checkpoint: PathBuf,
    /// Where a checkpoint is packed to upload, or downloaded to unpack.
    packed_checkpoint: PathBuf,
}

impl Dirs {
    pub fn new(work_dir: &Path, job_id: &str) -> Self {
        let root = work_dir.join("jobs").join(job_id);
        Self {
            input: root.join("input.json"),
            output: root.join("output.json"),
            out: root.join("out"),
            tmp: root.join("tmp"),
            home: root.join("home"),
            inputs: root.join("inputs"),
            checkpoint: root.join("checkpoint"),
            packed_checkpoint: root.join("checkpoint.tar"),
            root,
        }
    }

    fn create(&self) -> std::io::Result<()> {
        if self.root.exists() {
            std::fs::remove_dir_all(&self.root)?;
        }
        let dirs = [
            &self.root,
            &self.out,
            &self.tmp,
            &self.home,
            &self.inputs,
            &self.checkpoint,
        ];
        for dir in dirs {
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

/// What to run, and how.
pub struct Plan<'a> {
    pub input: &'a RunInput,
    /// The plugin's code, which it runs from.
    pub plugin_dir: &'a Path,
    /// The environment to run it in; `None` for the host's own software.
    pub built: Option<&'a Built>,
    pub timeout: Duration,
}

/// The command's environment: nothing of the worker's but what programs need to run.
fn environment(dirs: &Dirs, input: &RunInput, built: Option<&Built>) -> Vec<(String, String)> {
    const PASSED: &[&str] = &[
        "PATH",
        "LANG",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        // Where micromamba keeps its packages, for conda environments.
        "MAMBA_ROOT_PREFIX",
    ];
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
    if let Some(built) = built {
        // Its programs first, so `python` is the environment's.
        let bin = path(&built.prefix.join("bin"));
        for (name, value) in &mut env {
            if name == "PATH" {
                *value = format!("{bin}:{value}");
            }
        }
        let variable = match built.kind {
            Kind::Python => "VIRTUAL_ENV",
            Kind::Conda => "CONDA_PREFIX",
        };
        env.push((variable.into(), path(&built.prefix)));
    }
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
    if input.job.checkpoint.is_some() {
        env.push(("IKI_CHECKPOINT_DIR".into(), path(&dirs.checkpoint)));
    }
    // For its random numbers: the same again when the run is reproduced (ADR 0057).
    if let Some(seed) = input.run.seed {
        env.push(("IKI_SEED".into(), seed.to_string()));
    }
    // An action that declares hosts reaches them through the egress proxy, with this
    // attempt's credentials (ADR 0053). Plain-HTTP requests go there too, to be refused
    // with a reason rather than time out.
    if let Some(proxy) = &input.job.proxy {
        for name in ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"] {
            env.push((name.into(), proxy.clone()));
        }
    }
    // The secrets the action declares, for this command alone (ADR 0054).
    if let Some(secrets) = &input.job.secrets {
        env.extend(secrets.iter().map(|(n, v)| (n.clone(), v.clone())));
    }
    env
}

/// The program and its arguments: the action's command, run through `micromamba run` in
/// a conda environment so its activation scripts run.
fn command_line(plan: &Plan) -> Result<(PathBuf, Vec<String>), String> {
    let (program, args) = plan
        .input
        .command
        .split_first()
        .ok_or("The action has no command.")?;
    // A relative path to a program (`bin/fold`) is the plugin's; a bare name is on PATH.
    let program = if program.contains('/') && !Path::new(program).is_absolute() {
        plan.plugin_dir.join(program)
    } else {
        PathBuf::from(program)
    };
    Ok(match plan.built {
        Some(built) if built.kind == Kind::Conda => {
            let mut wrapped = vec![
                "run".to_owned(),
                "--prefix".to_owned(),
                built.prefix.to_string_lossy().into_owned(),
                // micromamba 2 takes no `--`: options after the program are the program's.
                program.to_string_lossy().into_owned(),
            ];
            wrapped.extend(args.iter().cloned());
            (PathBuf::from("micromamba"), wrapped)
        }
        _ => (program, args.to_vec()),
    })
}

fn spawn(plan: &Plan, dirs: &Dirs) -> Result<Child, String> {
    let (program, args) = command_line(plan)?;
    let mut command = Command::new(&program);
    command
        .args(&args)
        .current_dir(plan.plugin_dir)
        .env_clear()
        .envs(environment(dirs, plan.input, plan.built))
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

/// How the command exited, briefly: `exit status 3`, or `signal 9`.
fn exit(status: ExitStatus) -> String {
    #[cfg(unix)]
    if let Some(signal) = std::os::unix::process::ExitStatusExt::signal(&status) {
        return format!("signal {signal}");
    }
    status.code().map_or_else(
        || "no exit status".to_owned(),
        |code| format!("exit status {code}"),
    )
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
pub enum Report {
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
pub fn run(
    api: &Api,
    config: &Config,
    have: &plugins::Have,
    interrupted: &AtomicBool,
    raw: &serde_json::Value,
) -> Ended {
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
    let sending = {
        let job = Arc::clone(&job);
        thread::spawn(move || sender(&job))
    };
    let dirs = Dirs::new(&config.work_dir, &job.id);
    let name = &input.run.plugin.name;
    let plugin_dir = have.dir(name).map_or_else(
        || {
            config
                .plugins_dir
                .as_deref()
                .unwrap_or(&config.cache_dir)
                .join(name)
        },
        Path::to_path_buf,
    );
    let wanted = have.for_run(
        &input.run.plugin.name,
        input.run.plugin.sha.as_deref(),
        &input.environment,
    );
    let report = match wanted {
        Err(message) => Some(Report::Fail(FailInReason::Failed, message)),
        Ok(_) if !plugin_dir.is_dir() => Some(Report::Fail(
            FailInReason::Failed,
            format!(
                "This worker has no code for the plugin {} (looked in {}).",
                input.run.plugin.name,
                plugin_dir.display()
            ),
        )),
        Ok(built) => {
            let plan = Plan {
                input: &input,
                plugin_dir: &plugin_dir,
                built,
                timeout: Duration::from_secs(
                    u64::try_from(input.job.timeout.max(1)).unwrap_or(3600),
                ),
            };
            execute(&job, &plan, &dirs, raw, interrupted)
        }
    };
    job.signals.output_read.store(true, Ordering::SeqCst);
    let _ = sending.join();
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
    host: &impl Host,
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
        let why = host.stop_for().or_else(|| {
            if interrupted.load(Ordering::SeqCst) {
                Some(Stop::Interrupted)
            } else if Instant::now() >= deadline {
                Some(Stop::TimedOut)
            } else {
                None
            }
        });
        if let Some(why) = why {
            say::info(
                "Stopping the command.",
                &[("job", &host.id()), ("why", &format!("{why:?}"))],
            );
            return (stop(child), Some(why));
        }
        thread::sleep(Duration::from_millis(100));
    }
}

/// Stages the input files, then writes input.json, saying where they are.
fn stage_input(
    host: &impl Host,
    input: &RunInput,
    dirs: &Dirs,
    raw: &serde_json::Value,
) -> Result<(), files::Failed> {
    let mut written = raw.clone();
    // The command gets its secrets in its environment, never in a file it might copy.
    if let Some(job) = written
        .get_mut("job")
        .and_then(serde_json::Value::as_object_mut)
    {
        job.remove("secrets");
    }
    if let Some(wanted) = input.files.as_ref().filter(|f| !f.is_empty()) {
        let staged = host.stage(wanted, &dirs.inputs)?;
        say::info(
            "Staged the input files.",
            &[("job", &host.id()), ("files", &staged.len())],
        );
        written["files"] = serde_json::to_value(staged)
            .map_err(|e| files::Failed::Job(format!("Couldn't write input.json: {e}")))?;
    }
    let bytes = serde_json::to_vec_pretty(&written)
        .map_err(|e| files::Failed::Job(format!("Couldn't write input.json: {e}")))?;
    std::fs::write(&dirs.input, bytes)
        .map_err(|e| files::Failed::Job(format!("Couldn't write input.json: {e}.")))
}

/// Puts what the results hold in place of each `$file` output.json names.
fn upload_results(
    host: &impl Host,
    dirs: &Dirs,
    output: &mut serde_json::Map<String, serde_json::Value>,
) -> Result<(), files::Failed> {
    let count = host.results(&dirs.out, output)?;
    if count > 0 {
        say::info(
            host.results_done(),
            &[("job", &host.id()), ("files", &count)],
        );
    }
    Ok(())
}

/// Stages the job and runs its command from `plugin_dir`, the same for a leased job and
/// a local one. `None` if the lease was lost.
pub fn execute<H: Host>(
    host: &Arc<H>,
    plan: &Plan,
    dirs: &Dirs,
    raw: &serde_json::Value,
    interrupted: &AtomicBool,
) -> Option<Report> {
    let (input, timeout) = (plan.input, plan.timeout);
    if let Err(error) = dirs.create() {
        let message = format!("Couldn't stage the job: {error}.");
        return Some(Report::Fail(FailInReason::Failed, message));
    }
    match stage_input(host.as_ref(), input, dirs, raw) {
        Ok(()) => {}
        Err(files::Failed::Lost(error)) => {
            host.lost(&error);
            return None;
        }
        Err(files::Failed::Job(message)) => {
            return Some(Report::Fail(FailInReason::Failed, message));
        }
    }
    let saving = input.job.checkpoint.as_ref();
    if let Some(saved) = saving.and_then(|c| c.saved.as_ref())
        && restore(host.as_ref(), saved, dirs).is_err()
    {
        return None;
    }
    let saver = saving.map(|_| Saver::new(host, dirs));
    host.state(HeartbeatInState::Running);
    let mut child = match spawn(plan, dirs) {
        Ok(child) => child,
        Err(message) => return Some(Report::Fail(FailInReason::Failed, message)),
    };
    let saving_every = saver.as_ref().and_then(|saver| {
        let every = saving?.save_every?;
        let every = Duration::from_secs(u64::try_from(every.max(1)).unwrap_or(600));
        let saver = Arc::clone(saver);
        Some(thread::spawn(move || saver.every(every)))
    });
    let (done, readers_done) = mpsc::channel::<()>();
    // The last `::error` line the command printed: why it failed, if it fails.
    let said: Arc<Mutex<Option<String>>> = Arc::default();
    let mask = logs::Mask::new(
        input
            .job
            .secrets
            .iter()
            .flat_map(|secrets| secrets.values().map(String::as_str)),
    );
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
        let (host, done, said, mask) = (
            Arc::clone(host),
            done.clone(),
            Arc::clone(&said),
            mask.clone(),
        );
        thread::spawn(move || {
            read_lines(host.as_ref(), stream, &said, &mask);
            let _ = done.send(());
        });
    }
    drop(done);

    let deadline = Instant::now() + timeout;
    let (status, stopped) = supervise(host.as_ref(), &mut child, interrupted, deadline);
    // The readers end when the pipes close; one a stray process holds open isn't waited on.
    let wait_until = Instant::now() + Duration::from_secs(5);
    while readers_done
        .recv_timeout(wait_until.saturating_duration_since(Instant::now()))
        .is_ok()
    {}

    let why = said.lock().unwrap_or_else(PoisonError::into_inner).take();
    if let Some(saver) = &saver {
        let save_on = saving.map(|c| c.save_on.as_slice()).unwrap_or_default();
        if let Err(error) = saver.finish(saving_every, save_on, stopped, status) {
            host.lost(&error);
            return None;
        }
    }
    // Whether it succeeded, failed or ran out of time: its files help see why. Not when
    // it was cancelled, or the worker is stopping.
    if (stopped.is_none() && status.is_some()) || stopped == Some(Stop::TimedOut) {
        let patterns = input.job.artifacts.as_deref().unwrap_or_default();
        if !patterns.is_empty() {
            host.state(HeartbeatInState::Uploading);
            if let Err(error) = keep_artifacts(host.as_ref(), dirs, patterns) {
                host.lost(&error);
                return None;
            }
        }
    }
    outcome(host.as_ref(), dirs, status, stopped, timeout, why)
}

/// Restores the checkpoint an earlier attempt saved before the command starts. One that
/// can't be restored is said in the log, and the command starts afresh. Errs if the lease
/// was lost.
fn restore(host: &impl Host, saved: &RunInputJobCheckpointSaved, dirs: &Dirs) -> Result<(), ()> {
    let which = format!(
        "the checkpoint attempt {} saved at {}",
        saved.attempt, saved.saved_at
    );
    match host.restore(saved, &dirs.packed_checkpoint, &dirs.checkpoint) {
        Ok(()) => {
            host.line(&format!("iki-worker: Resumed from {which}.\n"));
            say::info(
                "Restored the checkpoint.",
                &[("job", &host.id()), ("attempt", &saved.attempt)],
            );
            Ok(())
        }
        Err(files::Failed::Lost(error)) => {
            host.lost(&error);
            Err(())
        }
        Err(files::Failed::Job(message)) => {
            let _ = std::fs::remove_file(&dirs.packed_checkpoint);
            if let Err(error) = checkpoint::clear(&dirs.checkpoint) {
                say::warn(
                    "Couldn't empty the checkpoint directory.",
                    &[("detail", &error)],
                );
            }
            host.line(&format!(
                "iki-worker: Couldn't restore {which}, so this attempt starts afresh. {message}.\n"
            ));
            say::warn(
                "Couldn't restore the checkpoint.",
                &[("job", &host.id()), ("detail", &message)],
            );
            Ok(())
        }
    }
}

/// Saves the checkpoint directory if it changed: every so often while the command runs,
/// and once more when the attempt ends in a way the run is retried for.
struct Saver<H: Host> {
    host: Arc<H>,
    dir: PathBuf,
    packed: PathBuf,
    /// The directory's fingerprint when it was last saved or restored; held while saving,
    /// so saves never overlap.
    last: Mutex<String>,
    stop: AtomicBool,
}

impl<H: Host> Saver<H> {
    fn new(host: &Arc<H>, dirs: &Dirs) -> Arc<Self> {
        // What was restored needn't be saved again.
        let last = checkpoint::fingerprint(&dirs.checkpoint).unwrap_or_default();
        Arc::new(Self {
            host: Arc::clone(host),
            dir: dirs.checkpoint.clone(),
            packed: dirs.packed_checkpoint.clone(),
            last: Mutex::new(last),
            stop: AtomicBool::new(false),
        })
    }

    /// Stops saving every so often, then saves once more if the run is retried for how
    /// the attempt ended: a later one resumes from what it got to. Errs if the lease was
    /// lost.
    fn finish(
        &self,
        every: Option<thread::JoinHandle<()>>,
        save_on: &[SaveOn],
        stopped: Option<Stop>,
        status: Option<ExitStatus>,
    ) -> Result<(), Error> {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(every) = every {
            let _ = every.join();
        }
        let ending = match (stopped, status) {
            (Some(Stop::Interrupted), _) => Some(SaveOn::Interrupted),
            (Some(Stop::TimedOut), _) => Some(SaveOn::TimedOut),
            (None, Some(status)) if !status.success() => Some(SaveOn::Failed),
            _ => None,
        };
        if ending.is_some_and(|e| save_on.contains(&e)) {
            self.host.state(HeartbeatInState::Uploading);
            self.save()?;
        }
        Ok(())
    }

    fn every(&self, every: Duration) {
        let mut next = Instant::now() + every;
        while !self.stop.load(Ordering::SeqCst) {
            if Instant::now() >= next {
                if let Err(error) = self.save() {
                    self.host.lost(&error);
                    return;
                }
                next = Instant::now() + every;
            }
            thread::sleep(Duration::from_millis(200));
        }
    }

    /// Saves the directory, if it changed. One that can't be saved is said in the log, and
    /// doesn't fail the run. Errs if the lease was lost.
    fn save(&self) -> Result<(), Error> {
        let mut last = self.last.lock().unwrap_or_else(PoisonError::into_inner);
        let now = match checkpoint::fingerprint(&self.dir) {
            Ok(now) => now,
            Err(error) => {
                self.say(&format!(
                    "Couldn't save a checkpoint: couldn't read it: {error}."
                ));
                return Ok(());
            }
        };
        if now == *last {
            return Ok(());
        }
        let packed = match checkpoint::pack(&self.dir, &self.packed) {
            Ok(packed) => packed,
            Err(error) => {
                let _ = std::fs::remove_file(&self.packed);
                self.say(&format!(
                    "Couldn't save a checkpoint: {error}. The next save tries again."
                ));
                return Ok(());
            }
        };
        let saved = self.host.save(&packed);
        let _ = std::fs::remove_file(&packed.path);
        match saved {
            Ok(()) => {
                last.clone_from(&packed.fingerprint);
                let noun = if packed.files == 1 { "file" } else { "files" };
                let mut said = format!(
                    "Saved a checkpoint: {} {noun}, {}.",
                    packed.files,
                    human_size(packed.size)
                );
                if packed.left_out > 0 {
                    let _ = write!(
                        said,
                        " Left out {} link(s) or other entries: only files are saved.",
                        packed.left_out
                    );
                }
                self.say(&said);
                say::info(
                    "Saved a checkpoint.",
                    &[("job", &self.host.id()), ("bytes", &packed.size)],
                );
                Ok(())
            }
            Err(files::Failed::Lost(error)) => Err(error),
            Err(files::Failed::Job(message)) => {
                self.say(&format!("{message}."));
                Ok(())
            }
        }
    }

    fn say(&self, text: &str) {
        self.host.line(&format!("iki-worker: {text}\n"));
    }
}

/// Bytes, for people: `512 B`, `3.4 MiB`.
fn human_size(bytes: u64) -> String {
    const UNITS: [&str; 5] = ["KiB", "MiB", "GiB", "TiB", "PiB"];
    if bytes < 1024 {
        return format!("{bytes} B");
    }
    #[allow(clippy::cast_precision_loss)]
    let mut value = bytes as f64 / 1024.0;
    let mut unit = 0;
    while value >= 1024.0 && unit < UNITS.len() - 1 {
        value /= 1024.0;
        unit += 1;
    }
    format!("{value:.1} {}", UNITS[unit])
}

/// Keeps the files in the output directory the action's `artifacts.keep` patterns match.
/// One that can't be kept is said in the log, and doesn't fail the run. Errs if the lease
/// was lost.
fn keep_artifacts(host: &impl Host, dirs: &Dirs, patterns: &[String]) -> Result<(), Error> {
    let (found, over) = files::find_artifacts(&dirs.out, patterns);
    let mut kept = 0usize;
    for (relative, path) in &found {
        match host.keep(relative, path) {
            Ok(()) => kept += 1,
            Err(files::Failed::Lost(error)) => return Err(error),
            Err(files::Failed::Job(message)) => {
                host.line(&format!("iki-worker: {message}\n"));
            }
        }
    }
    if over > 0 {
        host.line(&format!(
            "iki-worker: Kept the first {} artifacts; {over} more weren't kept.\n",
            files::MAX_ARTIFACTS
        ));
    }
    if kept > 0 {
        let noun = if kept == 1 { "artifact" } else { "artifacts" };
        host.line(&format!("iki-worker: {} {kept} {noun}.\n", host.kept()));
        say::info(
            "Kept the artifacts.",
            &[("job", &host.id()), ("artifacts", &kept)],
        );
    }
    Ok(())
}

/// What to report, once the command has ended. `None` if the lease was lost.
fn outcome(
    host: &impl Host,
    dirs: &Dirs,
    status: Option<ExitStatus>,
    stopped: Option<Stop>,
    timeout: Duration,
    why: Option<String>,
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
                host.state(HeartbeatInState::Uploading);
                match read_output(&dirs.output) {
                    Ok(mut output) => match upload_results(host, dirs, &mut output) {
                        Ok(()) => Report::Complete(output),
                        Err(files::Failed::Lost(error)) => {
                            host.lost(&error);
                            return None;
                        }
                        Err(files::Failed::Job(message)) => {
                            Report::Fail(FailInReason::Failed, message)
                        }
                    },
                    Err(message) => Report::Fail(FailInReason::Failed, message),
                }
            }
            Some(status) => Report::Fail(
                FailInReason::Failed,
                // What the command said, if it said why; otherwise how it exited.
                why.map_or_else(
                    || describe(status),
                    |why| format!("{why} ({})", exit(status)),
                ),
            ),
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

    fn input(command: &[&str]) -> RunInput {
        serde_json::from_value(serde_json::json!({
            "apiVersion": "iki/v1",
            "kind": "RunInput",
            "run": {
                "id": "r", "hrid": "RUN-1", "attempt": 1, "action": "dna.gc",
                "plugin": {"name": "dna", "version": "1.0.0", "sha": null}, "project": "p",
            },
            "job": {
                "id": "j", "lease_token": "t", "lease_expires_at": "", "heartbeat_every": 20,
                "timeout": 60, "resources": {},
            },
            "command": command,
            "environment": "dna.tools",
            "params": {},
            "inputs": {},
        }))
        .unwrap()
    }

    /// A host that keeps what's saved and said, and can't restore.
    #[derive(Default)]
    struct Saving {
        saved: Mutex<Vec<usize>>,
        lines: Mutex<Vec<String>>,
    }

    impl Host for Saving {
        fn id(&self) -> &'static str {
            "j"
        }

        fn stage(
            &self,
            _wanted: &BTreeMap<String, RunInputFile>,
            _dir: &Path,
        ) -> Result<BTreeMap<String, RunInputFile>, files::Failed> {
            Ok(BTreeMap::new())
        }

        fn results(
            &self,
            _out_dir: &Path,
            _output: &mut serde_json::Map<String, serde_json::Value>,
        ) -> Result<usize, files::Failed> {
            Ok(0)
        }

        fn keep(&self, _relative: &str, _path: &Path) -> Result<(), files::Failed> {
            Ok(())
        }

        fn line(&self, text: &str) {
            self.lines.lock().unwrap().push(text.to_owned());
        }

        fn restore(
            &self,
            _saved: &RunInputJobCheckpointSaved,
            _packed: &Path,
            _dir: &Path,
        ) -> Result<(), files::Failed> {
            Err(files::Failed::Job("It was gone".into()))
        }

        fn save(&self, packed: &checkpoint::Packed) -> Result<(), files::Failed> {
            self.saved.lock().unwrap().push(packed.files);
            Ok(())
        }
    }

    #[test]
    fn saves_the_checkpoint_as_it_runs_and_once_more_when_it_fails() {
        let dir = std::env::temp_dir().join(format!("iki-job-saves-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let script = r#"echo 1 > "$IKI_CHECKPOINT_DIR/a"; sleep 2; echo 2 > "$IKI_CHECKPOINT_DIR/b"; exit 3"#;
        let mut raw = serde_json::to_value(input(&["sh", "-c", script])).unwrap();
        raw["job"]["checkpoint"] = serde_json::json!({
            "save_every": 1,
            "save_on": ["failed"],
            "saved": {
                "id": "c", "attempt": 1, "saved_at": "2026-11-12T10:00:00+00:00", "size": 1,
                "sha256": "0".repeat(64),
            },
        });
        let input: RunInput = serde_json::from_value(raw.clone()).unwrap();
        let plan = Plan {
            input: &input,
            plugin_dir: &dir,
            built: None,
            timeout: Duration::from_secs(30),
        };
        let dirs = Dirs::new(&dir, "j");
        let host = Arc::new(Saving::default());

        let report = execute(&host, &plan, &dirs, &raw, &AtomicBool::new(false));

        assert!(matches!(
            report,
            Some(Report::Fail(FailInReason::Failed, _))
        ));
        // Once while it ran, with one file; once more as it failed, with both.
        assert_eq!(*host.saved.lock().unwrap(), vec![1, 2]);
        let lines = host.lines.lock().unwrap().join("");
        assert!(
            lines.contains(
                "iki-worker: Couldn't restore the checkpoint attempt 1 saved at \
                 2026-11-12T10:00:00+00:00, so this attempt starts afresh. It was gone."
            ),
            "{lines}"
        );
        assert!(
            lines.contains("iki-worker: Saved a checkpoint: 1 file, 2.0 KiB."),
            "{lines}"
        );
        assert!(
            lines.contains("iki-worker: Saved a checkpoint: 2 files, 3.0 KiB."),
            "{lines}"
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn says_sizes_for_people() {
        assert_eq!(human_size(512), "512 B");
        assert_eq!(human_size(2560), "2.5 KiB");
        assert_eq!(human_size(3 << 30), "3.0 GiB");
    }

    #[test]
    fn commands_run_in_their_built_environment() {
        let input = input(&["python", "gc.py"]);
        let venv = Built {
            digest: "sha256:x".into(),
            kind: Kind::Python,
            prefix: "/opt/iki/envs/dna/tools".into(),
        };
        let plan = Plan {
            input: &input,
            plugin_dir: Path::new("/opt/iki/plugins/dna"),
            built: Some(&venv),
            timeout: Duration::from_secs(60),
        };
        assert_eq!(
            command_line(&plan).unwrap(),
            (PathBuf::from("python"), vec!["gc.py".to_owned()])
        );
        let dirs = Dirs::new(Path::new("/work"), "j");
        let env: std::collections::HashMap<_, _> = environment(&dirs, &input, Some(&venv))
            .into_iter()
            .collect();
        assert!(env["PATH"].starts_with("/opt/iki/envs/dna/tools/bin:"));
        assert_eq!(env["VIRTUAL_ENV"], "/opt/iki/envs/dna/tools");

        let conda = Built {
            kind: Kind::Conda,
            ..venv.clone()
        };
        let plan = Plan {
            built: Some(&conda),
            ..plan
        };
        let (program, args) = command_line(&plan).unwrap();
        assert_eq!(program, PathBuf::from("micromamba"));
        assert_eq!(
            args,
            [
                "run",
                "--prefix",
                "/opt/iki/envs/dna/tools",
                "python",
                "gc.py"
            ]
        );
        // Without one, the host's own software.
        let plan = Plan {
            built: None,
            ..plan
        };
        assert_eq!(command_line(&plan).unwrap().0, PathBuf::from("python"));
        let env: std::collections::HashMap<_, _> =
            environment(&dirs, &input, None).into_iter().collect();
        assert!(!env.contains_key("VIRTUAL_ENV") && !env.contains_key("CONDA_PREFIX"));
    }

    #[test]
    fn only_runs_that_declare_hosts_get_the_proxy() {
        let dirs = Dirs::new(Path::new("/work"), "j");
        let mut input = input(&["python", "fetch.py"]);
        let env: std::collections::HashMap<_, _> =
            environment(&dirs, &input, None).into_iter().collect();
        assert!(
            !env.keys()
                .any(|name| name.to_lowercase().ends_with("_proxy"))
        );

        let proxy = "http://j:t@iki-egress-proxy:3128".to_owned();
        input.job.proxy = Some(proxy.clone());
        let env: std::collections::HashMap<_, _> =
            environment(&dirs, &input, None).into_iter().collect();
        for name in ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"] {
            assert_eq!(env[name], proxy);
        }
    }

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
