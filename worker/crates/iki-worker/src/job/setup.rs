//! An environment's setup build (ADR 0075): the worker on the machine started for it runs
//! the environment's setup script, which fills the volumes mounted writable for it, then
//! says how it ended. The server snapshots the volumes once the machine has gone.
//!
//! The script runs with `sh`, from the plugin's directory, as an action's command does:
//! nothing of the worker's environment but what programs need, its own `HOME` and
//! `TMPDIR`, `IKI_INPUT` (the setup input, without the lease), `IKI_VOLUME_<NAME>` for each
//! volume's mount, and the egress proxy when its setup declares hosts. It may write
//! `{"data_version": "PDB 2026-10-01"}` to `IKI_OUTPUT` to say what the data is. Its
//! output, progress and cancelling work as a job's do.

use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use iki_protocol::{SetupFinishIn, SetupFinishInOutcome, SetupFinishOut, SetupInput, fill, paths};

use super::{
    Ended, Job, Signals, Stop, base_environment, clip, create_private_dir, describe, exit,
    heartbeats, proxy_environment, read_lines, sender, signal_group, stop,
};
use crate::api::{self, Api, Error};
use crate::config::Config;
use crate::logs::{self, Log};
use crate::plugins::{self, Built, Kind};
use crate::say;

/// The most `IKI_OUTPUT` may hold: it only says what the data is.
const MAX_OUTPUT: u64 = 64 * 1024;
/// How long to keep trying to report how the build ended.
const REPORT_FOR: Duration = Duration::from_secs(300);

/// How the build ended, to report.
#[derive(Debug, PartialEq)]
struct Finish {
    outcome: SetupFinishInOutcome,
    message: String,
    data_version: String,
}

impl Finish {
    fn failed(message: impl Into<String>) -> Self {
        Self {
            outcome: SetupFinishInOutcome::Failed,
            message: message.into(),
            data_version: String::new(),
        }
    }
}

/// Where a build's files go: its own directory, as a job's.
struct Dirs {
    root: PathBuf,
    input: PathBuf,
    output: PathBuf,
    tmp: PathBuf,
    home: PathBuf,
}

impl Dirs {
    fn new(work_dir: &Path, id: &str) -> Self {
        let root = work_dir.join("setups").join(id);
        Self {
            input: root.join("input.json"),
            output: root.join("output.json"),
            tmp: root.join("tmp"),
            home: root.join("home"),
            root,
        }
    }

    fn create(&self) -> std::io::Result<()> {
        if self.root.exists() {
            std::fs::remove_dir_all(&self.root)?;
        }
        for dir in [&self.root, &self.tmp, &self.home] {
            create_private_dir(dir)?;
        }
        Ok(())
    }
}

/// Runs a setup build the worker just leased, from the setup input as the server sent it.
pub fn run(
    api: &Api,
    config: &Config,
    have: &plugins::Have,
    interrupted: &AtomicBool,
    raw: &serde_json::Value,
) -> Ended {
    let input: SetupInput = match serde_json::from_value(raw.clone()) {
        Ok(input) => input,
        Err(error) => return unreadable(api, raw, &error.to_string()),
    };
    let job = Arc::new(Job {
        api: api.clone(),
        id: input.setup.id.clone(),
        lease_token: input.job.lease_token.clone(),
        setup: true,
        state: Mutex::new(iki_protocol::HeartbeatInState::Running),
        signals: Signals::default(),
        log: Log::default(),
        progress: Mutex::new(None),
    });
    say::info(
        "Leased a setup build.",
        &[
            ("setup", &input.setup.hrid),
            ("environment", &input.setup.environment),
            ("version", &input.setup.version),
        ],
    );
    let every = Duration::from_secs(u64::try_from(input.job.heartbeat_every.max(1)).unwrap_or(20));
    let beating = {
        let job = Arc::clone(&job);
        thread::spawn(move || heartbeats(&job, every))
    };
    let sending = {
        let job = Arc::clone(&job);
        thread::spawn(move || sender(&job))
    };
    let name = &input.setup.plugin.name;
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
    let dirs = Dirs::new(&config.work_dir, &job.id);
    let finish = match have.for_run(
        name,
        input.setup.plugin.sha.as_deref(),
        &input.setup.environment,
    ) {
        Err(message) => Some(Finish::failed(message)),
        Ok(_) if !plugin_dir.is_dir() => Some(Finish::failed(format!(
            "This worker has no code for the plugin {name} (looked in {}).",
            plugin_dir.display()
        ))),
        Ok(built) => execute(&job, &input, raw, &plugin_dir, built, &dirs, interrupted),
    };
    job.signals.output_read.store(true, Ordering::SeqCst);
    let _ = sending.join();
    let ended = report(&job, finish);
    job.signals.finished.store(true, Ordering::SeqCst);
    let _ = beating.join();
    if !config.keep_job_dirs
        && let Err(error) = std::fs::remove_dir_all(&dirs.root)
        && error.kind() != std::io::ErrorKind::NotFound
    {
        say::warn(
            "Couldn't remove a setup build's directory.",
            &[("path", &dirs.root.display()), ("detail", &error)],
        );
    }
    ended
}

/// The script's path in the plugin, if it's one: relative, and never outside it.
fn script_path(plugin_dir: &Path, script: &str) -> Result<PathBuf, String> {
    let relative = Path::new(script);
    let inside = relative
        .components()
        .all(|part| matches!(part, Component::Normal(_) | Component::CurDir));
    if !inside {
        return Err(format!(
            "The setup script {script} isn't a path in the plugin."
        ));
    }
    let path = plugin_dir.join(relative);
    if !path.is_file() {
        return Err(format!("The plugin has no setup script {script}."));
    }
    Ok(path)
}

/// `IKI_VOLUME_AF_DB` for the volume `af-db`.
fn volume_variable(name: &str) -> String {
    let upper: String = name
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_uppercase()
            } else {
                '_'
            }
        })
        .collect();
    format!("IKI_VOLUME_{upper}")
}

/// Each volume must be mounted, and writable, where the environment says: the machine
/// was started with it there. Better to say so now than after hours of downloading.
fn check_volumes(input: &SetupInput) -> Result<(), String> {
    for (name, volume) in &input.volumes {
        let mount = Path::new(&volume.mount);
        if !mount.is_dir() {
            return Err(format!(
                "The volume {name} isn't mounted at {}.",
                volume.mount
            ));
        }
        let probe = mount.join(format!(".iki-write-check-{}", std::process::id()));
        let written = std::fs::write(&probe, b"");
        let _ = std::fs::remove_file(&probe);
        if let Err(error) = written {
            return Err(format!(
                "The volume {name} at {} can't be written: {error}.",
                volume.mount
            ));
        }
    }
    Ok(())
}

/// The script's environment.
fn environment(dirs: &Dirs, input: &SetupInput, built: Option<&Built>) -> Vec<(String, String)> {
    let path = |p: &Path| p.to_string_lossy().into_owned();
    let mut env = base_environment(built);
    env.extend([
        ("HOME".into(), path(&dirs.home)),
        ("TMPDIR".into(), path(&dirs.tmp)),
        ("IKI_INPUT".into(), path(&dirs.input)),
        ("IKI_OUTPUT".into(), path(&dirs.output)),
        ("IKI_SETUP_ID".into(), input.setup.id.clone()),
        ("IKI_SETUP_VERSION".into(), input.setup.version.to_string()),
        ("PYTHONUNBUFFERED".into(), "1".into()),
    ]);
    env.extend(
        input
            .volumes
            .iter()
            .map(|(name, volume)| (volume_variable(name), volume.mount.clone())),
    );
    // Only the hosts its setup declares, through the egress proxy (ADR 0053).
    if let Some(proxy) = &input.job.proxy {
        env.extend(proxy_environment(proxy));
    }
    env
}

/// What the script reads from `IKI_INPUT`: the input, without the lease or the proxy's
/// credentials.
fn write_input(dirs: &Dirs, raw: &serde_json::Value) -> Result<(), String> {
    let mut written = raw.clone();
    if let Some(object) = written.as_object_mut() {
        object.remove("job");
    }
    let bytes = serde_json::to_vec_pretty(&written)
        .map_err(|e| format!("Couldn't write input.json: {e}."))?;
    std::fs::write(&dirs.input, bytes).map_err(|e| format!("Couldn't write input.json: {e}."))
}

fn spawn(
    script: &Path,
    plugin_dir: &Path,
    built: Option<&Built>,
    env: Vec<(String, String)>,
) -> Result<Child, String> {
    let script = script.to_string_lossy().into_owned();
    let (program, args) = match built {
        // Through `micromamba run`, so a conda environment's activation scripts run.
        Some(built) if built.kind == Kind::Conda => (
            "micromamba",
            vec![
                "run".to_owned(),
                "--prefix".to_owned(),
                built.prefix.to_string_lossy().into_owned(),
                "sh".to_owned(),
                script,
            ],
        ),
        _ => ("sh", vec![script]),
    };
    let mut command = Command::new(program);
    command
        .args(&args)
        .current_dir(plugin_dir)
        .env_clear()
        .envs(env)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(unix)]
    std::os::unix::process::CommandExt::process_group(&mut command, 0);
    command
        .spawn()
        .map_err(|error| format!("Couldn't start the setup script: {error}."))
}

/// What the script said the data is, from `IKI_OUTPUT`; nothing if it wrote nothing.
fn data_version(path: &Path) -> Result<String, String> {
    let Ok(meta) = std::fs::metadata(path) else {
        return Ok(String::new());
    };
    if meta.len() > MAX_OUTPUT {
        return Err(format!(
            "IKI_OUTPUT is {} bytes, over the limit of {MAX_OUTPUT}: it only says what the \
             data is.",
            meta.len()
        ));
    }
    let text = std::fs::read(path).map_err(|e| format!("Couldn't read IKI_OUTPUT: {e}."))?;
    let value: serde_json::Value =
        serde_json::from_slice(&text).map_err(|e| format!("IKI_OUTPUT isn't valid JSON: {e}."))?;
    let object = value
        .as_object()
        .ok_or("IKI_OUTPUT must hold a JSON object, such as {\"data_version\": \"2026-10\"}.")?;
    match object.get("data_version") {
        None | Some(serde_json::Value::Null) => Ok(String::new()),
        Some(serde_json::Value::String(text)) => Ok(clip(text.trim(), 200)),
        Some(_) => Err("IKI_OUTPUT's data_version must be text.".to_owned()),
    }
}

/// Runs the script and waits for it, or stops it: when the lease is lost, the worker is
/// stopping, the build is cancelled, or it runs past its timeout. `None` if the lease was
/// lost.
fn execute(
    job: &Arc<Job>,
    input: &SetupInput,
    raw: &serde_json::Value,
    plugin_dir: &Path,
    built: Option<&Built>,
    dirs: &Dirs,
    interrupted: &AtomicBool,
) -> Option<Finish> {
    let script = match script_path(plugin_dir, &input.script) {
        Ok(script) => script,
        Err(message) => return Some(Finish::failed(message)),
    };
    if let Err(message) = check_volumes(input) {
        return Some(Finish::failed(message));
    }
    if let Err(error) = dirs.create() {
        return Some(Finish::failed(format!(
            "Couldn't make the setup's directory: {error}."
        )));
    }
    if let Err(message) = write_input(dirs, raw) {
        return Some(Finish::failed(message));
    }
    let mut child = match spawn(&script, plugin_dir, built, environment(dirs, input, built)) {
        Ok(child) => child,
        Err(message) => return Some(Finish::failed(message)),
    };
    let (done, readers_done) = mpsc::channel::<()>();
    let said: Arc<Mutex<Option<String>>> = Arc::default();
    // The proxy's password is the lease token: never in the log.
    let mask = logs::Mask::new([input.job.lease_token.as_str()]);
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
        let (job, done, said, mask) = (
            Arc::clone(job),
            done.clone(),
            Arc::clone(&said),
            mask.clone(),
        );
        thread::spawn(move || {
            read_lines(job.as_ref(), stream, &said, &mask);
            let _ = done.send(());
        });
    }
    drop(done);
    let timeout = Duration::from_secs(u64::try_from(input.job.timeout.max(1)).unwrap_or(3600));
    let (status, stopped) = supervise(job, &mut child, interrupted, Instant::now() + timeout);
    let wait_until = Instant::now() + Duration::from_secs(5);
    while readers_done
        .recv_timeout(wait_until.saturating_duration_since(Instant::now()))
        .is_ok()
    {}
    let why = said.lock().unwrap_or_else(PoisonError::into_inner).take();
    ending(dirs, status, stopped, timeout, why)
}

/// Waits for the script to end, or stops it.
fn supervise(
    job: &Job,
    child: &mut Child,
    interrupted: &AtomicBool,
    deadline: Instant,
) -> (Option<ExitStatus>, Option<Stop>) {
    loop {
        if let Ok(Some(status)) = child.try_wait() {
            // Anything it left running in its group goes with it.
            signal_group(child, rustix::process::Signal::KILL);
            return (Some(status), None);
        }
        let why = if job.signals.lost.load(Ordering::SeqCst) {
            Some(Stop::Lost)
        } else if job.signals.cancel.load(Ordering::SeqCst) {
            Some(Stop::Cancelled)
        } else if interrupted.load(Ordering::SeqCst) {
            Some(Stop::Interrupted)
        } else if Instant::now() >= deadline {
            Some(Stop::TimedOut)
        } else {
            None
        };
        if let Some(why) = why {
            say::info(
                "Stopping the setup script.",
                &[("setup", &job.id), ("why", &format!("{why:?}"))],
            );
            return (stop(child), Some(why));
        }
        thread::sleep(Duration::from_millis(100));
    }
}

/// How the build ended, to report. `None` if the lease was lost.
fn ending(
    dirs: &Dirs,
    status: Option<ExitStatus>,
    stopped: Option<Stop>,
    timeout: Duration,
    why: Option<String>,
) -> Option<Finish> {
    let stopped_with = |outcome, message: String| {
        Some(Finish {
            outcome,
            message,
            data_version: String::new(),
        })
    };
    match stopped {
        Some(Stop::Lost) => None,
        Some(Stop::Cancelled) => stopped_with(
            SetupFinishInOutcome::Cancelled,
            "Stopped: the setup build was cancelled.".into(),
        ),
        Some(Stop::Interrupted) => stopped_with(
            SetupFinishInOutcome::Interrupted,
            "Stopped: the worker was shut down.".into(),
        ),
        Some(Stop::TimedOut) => stopped_with(
            SetupFinishInOutcome::TimedOut,
            format!(
                "Stopped: it ran past its timeout of {} seconds.",
                timeout.as_secs()
            ),
        ),
        None => Some(match status {
            Some(status) if status.success() => match data_version(&dirs.output) {
                Ok(data_version) => Finish {
                    outcome: SetupFinishInOutcome::Succeeded,
                    message: String::new(),
                    data_version,
                },
                Err(message) => Finish::failed(message),
            },
            Some(status) => Finish::failed(why.map_or_else(
                || describe(status),
                |why| format!("{why} ({})", exit(status)),
            )),
            None => Finish::failed("Lost track of the setup script."),
        }),
    }
}

/// Sends the rest of the log and how the build ended.
fn report(job: &Job, finish: Option<Finish>) -> Ended {
    let deadline = Instant::now() + REPORT_FOR;
    let unauthorized = || {
        if job.signals.unauthorized.load(Ordering::SeqCst) {
            Ended::Unauthorized
        } else {
            Ended::Done
        }
    };
    let dropped = || {
        say::warn(
            "The setup build's lease went elsewhere; dropped it.",
            &[("setup", &job.id)],
        );
    };
    let Some(finish) = finish else {
        dropped();
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
        dropped();
        return unauthorized();
    }
    let body = SetupFinishIn {
        lease_token: job.lease_token.clone(),
        outcome: finish.outcome,
        message: Some(clip(&finish.message, 2000)).filter(|m| !m.is_empty()),
        data_version: Some(finish.data_version.clone()).filter(|v| !v.is_empty()),
    };
    match api::retrying(deadline, || {
        job.api
            .post::<_, SetupFinishOut>(&job.path(paths::SETUPS_FINISH), &body)
    }) {
        Ok(out) => say::info(
            "Reported how the setup build ended.",
            &[
                ("setup", &job.id),
                ("outcome", &format!("{:?}", finish.outcome)),
                ("detail", &finish.message),
                ("state", &out.map(|o| o.state).unwrap_or_default()),
            ],
        ),
        Err(error) => {
            job.note(&error);
            say::error(
                "Couldn't report how the setup build ended.",
                &[("setup", &job.id), ("detail", &error)],
            );
        }
    }
    unauthorized()
}

/// The server sent a setup build this worker can't read: say so, so it doesn't wait for
/// its lease to lapse.
fn unreadable(api: &Api, raw: &serde_json::Value, error: &str) -> Ended {
    let field = |pointer: &str| raw.pointer(pointer).and_then(serde_json::Value::as_str);
    say::error(
        "Couldn't read a leased setup build; is iki-worker out of date?",
        &[("detail", &error)],
    );
    let (Some(id), Some(token)) = (field("/setup/id"), field("/job/lease_token")) else {
        return Ended::Done;
    };
    let body = SetupFinishIn {
        lease_token: token.to_owned(),
        outcome: SetupFinishInOutcome::Failed,
        message: Some(clip(
            &format!(
                "iki-worker {} couldn't read this setup build: {error}",
                env!("CARGO_PKG_VERSION")
            ),
            2000,
        )),
        data_version: None,
    };
    match api.post::<_, SetupFinishOut>(&fill(paths::SETUPS_FINISH, &[id]), &body) {
        Err(Error::Unauthorized(_)) => Ended::Unauthorized,
        _ => Ended::Done,
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::io::{BufRead, BufReader, Write};
    use std::net::TcpListener;

    use iki_protocol::PluginCode;

    use super::*;

    struct Dir(PathBuf);

    impl Dir {
        fn new(name: &str) -> Self {
            let dir = std::env::temp_dir().join(format!("iki-setup-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(&dir).unwrap();
            Self(dir)
        }
    }

    impl Drop for Dir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn raw(volume: &Path, timeout: i64) -> serde_json::Value {
        serde_json::json!({
            "apiVersion": "iki/v1",
            "kind": "SetupInput",
            "setup": {
                "id": "s1", "hrid": "SETUP-000001", "version": 2,
                "environment": "structure_prediction.alphafold-gpu",
                "plugin": {"name": "structure_prediction", "version": "1.0.0", "sha": null},
            },
            "job": {
                "id": "s1", "lease_token": "secret-token", "lease_expires_at": "",
                "heartbeat_every": 1, "timeout": timeout,
                "proxy": "http://s1:secret-token@iki-egress:3128",
            },
            "script": "setup/fill.sh",
            "volumes": {"af-db": {"mount": volume.to_string_lossy(), "size": 1024}},
            "settings": {"mirror": "https://example.org"},
        })
    }

    /// A plugin whose setup script is `script`, and a volume, in `dir`.
    fn plugin(dir: &Path, script: &str) -> (PathBuf, PathBuf) {
        let plugin = dir.join("structure_prediction");
        std::fs::create_dir_all(plugin.join("setup")).unwrap();
        std::fs::write(plugin.join("setup/fill.sh"), script).unwrap();
        let volume = dir.join("volume");
        std::fs::create_dir_all(&volume).unwrap();
        (plugin, volume)
    }

    fn job() -> Arc<Job> {
        Arc::new(Job {
            api: Api::new("http://127.0.0.1:9", ""),
            id: "s1".into(),
            lease_token: "secret-token".into(),
            setup: true,
            state: Mutex::new(iki_protocol::HeartbeatInState::Running),
            signals: Signals::default(),
            log: Log::default(),
            progress: Mutex::new(None),
        })
    }

    fn execute_in(dir: &Path, script: &str, timeout: i64) -> (Option<Finish>, Arc<Job>) {
        let (plugin, volume) = plugin(dir, script);
        let raw = raw(&volume, timeout);
        let input: SetupInput = serde_json::from_value(raw.clone()).unwrap();
        let job = job();
        let dirs = Dirs::new(&dir.join("work"), "s1");
        let finish = execute(
            &job,
            &input,
            &raw,
            &plugin,
            None,
            &dirs,
            &AtomicBool::new(false),
        );
        (finish, job)
    }

    fn logged(job: &Job) -> String {
        let mut text = String::new();
        while let Some((offset, chunk)) = job.log.next_chunk() {
            text.push_str(&chunk);
            job.log.acknowledge(offset + chunk.len() as u64);
        }
        text
    }

    #[test]
    fn the_script_fills_its_volume_and_says_what_the_data_is() {
        let dir = Dir::new("fills");
        let script = r#"
            echo "into $IKI_VOLUME_AF_DB"
            echo "proxy $HTTPS_PROXY"
            echo db > "$IKI_VOLUME_AF_DB/params.bin"
            grep -q '"mirror": "https://example.org"' "$IKI_INPUT"
            ! grep -q secret-token "$IKI_INPUT"
            echo "::progress 0.5 Halfway"
            printf '{"data_version": " AlphaFold 2026-10 "}' > "$IKI_OUTPUT"
        "#;
        let (finish, job) = execute_in(&dir.0, script, 30);
        assert_eq!(
            finish,
            Some(Finish {
                outcome: SetupFinishInOutcome::Succeeded,
                message: String::new(),
                data_version: "AlphaFold 2026-10".into(),
            })
        );
        assert_eq!(
            std::fs::read_to_string(dir.0.join("volume/params.bin")).unwrap(),
            "db\n"
        );
        let text = logged(&job);
        assert!(text.contains(&format!("into {}", dir.0.join("volume").display())));
        // The lease token is the proxy's password: hidden.
        assert!(
            text.contains("proxy http://s1:***@iki-egress:3128"),
            "{text}"
        );
        assert_eq!(
            *job.progress.lock().unwrap(),
            Some((0.5, "Halfway".to_owned()))
        );
    }

    #[test]
    fn a_failing_script_says_why() {
        let dir = Dir::new("fails");
        let (finish, _) = execute_in(&dir.0, "echo '::error The mirror is down'\nexit 4\n", 30);
        assert_eq!(
            finish,
            Some(Finish::failed("The mirror is down (exit status 4)"))
        );
        let (finish, _) = execute_in(&dir.0, "echo '[1]' > \"$IKI_OUTPUT\"\n", 30);
        assert!(finish.unwrap().message.contains("JSON object"));
    }

    #[test]
    fn a_script_that_runs_too_long_is_stopped() {
        let dir = Dir::new("slow");
        let (finish, _) = execute_in(&dir.0, "sleep 30\n", 1);
        let finish = finish.unwrap();
        assert_eq!(finish.outcome, SetupFinishInOutcome::TimedOut);
        assert_eq!(
            finish.message,
            "Stopped: it ran past its timeout of 1 seconds."
        );
    }

    #[test]
    fn the_script_must_be_the_plugins_and_its_volumes_mounted() {
        let dir = Dir::new("checks");
        let (plugin, volume) = plugin(&dir.0, "true\n");
        assert!(script_path(&plugin, "setup/fill.sh").is_ok());
        for outside in ["../x.sh", "/bin/sh", "setup/../../x.sh"] {
            assert!(
                script_path(&plugin, outside)
                    .unwrap_err()
                    .contains("isn't a path in the plugin"),
                "{outside}"
            );
        }
        assert!(
            script_path(&plugin, "setup/missing.sh")
                .unwrap_err()
                .contains("has no setup script")
        );
        let mut raw = raw(&volume, 30);
        raw["volumes"]["af-db"]["mount"] = "/nonexistent/iki/af".into();
        let input: SetupInput = serde_json::from_value(raw).unwrap();
        assert_eq!(
            check_volumes(&input),
            Err("The volume af-db isn't mounted at /nonexistent/iki/af.".into())
        );
        assert_eq!(volume_variable("af-db"), "IKI_VOLUME_AF_DB");
    }

    /// Each request the fake server had: its path and body.
    type Seen = Arc<Mutex<Vec<(String, serde_json::Value)>>>;

    /// A worker API that answers the setup endpoints, cancelling at the first heartbeat
    /// if `cancel`, and records each request.
    fn fake_server(cancel: bool) -> (String, Seen) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let seen: Seen = Arc::default();
        let kept = Arc::clone(&seen);
        thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(stream) = stream else { return };
                let seen = Arc::clone(&kept);
                thread::spawn(move || serve(stream, cancel, &seen));
            }
        });
        (url, seen)
    }

    fn serve(mut stream: std::net::TcpStream, cancel: bool, seen: &Seen) {
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        loop {
            let mut first = String::new();
            if reader.read_line(&mut first).unwrap_or(0) == 0 {
                return;
            }
            let path = first.split(' ').nth(1).unwrap_or_default().to_owned();
            let mut length = 0;
            loop {
                let mut line = String::new();
                reader.read_line(&mut line).unwrap();
                if line == "\r\n" {
                    break;
                }
                if let Some((k, v)) = line.split_once(':')
                    && k.eq_ignore_ascii_case("content-length")
                {
                    length = v.trim().parse().unwrap();
                }
            }
            let mut body = vec![0; length];
            reader.read_exact(&mut body).unwrap();
            let body: serde_json::Value = serde_json::from_slice(&body).unwrap_or_default();
            let answer = if path.ends_with("/heartbeat") {
                serde_json::json!({"cancel_requested": cancel, "lease_expires_at": ""})
            } else if path.ends_with("/logs") {
                let end =
                    body["offset"].as_u64().unwrap() + body["text"].as_str().unwrap().len() as u64;
                serde_json::json!({"next_offset": end})
            } else {
                serde_json::json!({"state": "snapshotting"})
            };
            seen.lock().unwrap().push((path, body));
            let answer = answer.to_string();
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\
                 Content-Length: {}\r\n\r\n{answer}",
                answer.len()
            )
            .unwrap();
        }
    }

    fn config(dir: &Path, url: &str) -> Config {
        Config {
            url: url.to_owned(),
            registration_token: None,
            state_file: None,
            name: "iki-setup-x".into(),
            work_dir: dir.join("work"),
            plugins_dir: Some(dir.to_path_buf()),
            cache_dir: dir.join("cache"),
            capacity: iki_protocol::Capacity {
                cpu: 1.0,
                gpu_count: None,
                gpu_memory: None,
                memory: 1 << 30,
                scratch_disk: None,
            },
            keep_job_dirs: false,
            auto_update: false,
            allow_http: true,
            image: None,
            idle_exit: None,
            clean_dir: None,
        }
    }

    /// What a worker in the environment's image has, with the plugin's code in `dir`.
    fn have(dir: PathBuf) -> plugins::Have {
        plugins::Have::in_image(
            PluginCode {
                environments: None,
                name: "structure_prediction".into(),
                sha: None,
            },
            dir,
            "structure_prediction.alphafold-gpu",
        )
    }

    #[test]
    fn a_leased_build_streams_its_log_then_reports_how_it_ended() {
        let dir = Dir::new("leased");
        let (plugin, volume) = plugin(&dir.0, "echo fetching\nsleep 1.5\n");
        let (url, seen) = fake_server(false);
        let config = config(&dir.0, &url);
        let api = Api::new(&url, "iki_wkr_x");
        let ended = run(
            &api,
            &config,
            &have(plugin),
            &AtomicBool::new(false),
            &raw(&volume, 30),
        );
        assert_eq!(ended, Ended::Done);
        let seen = seen.lock().unwrap();
        let paths: HashMap<&str, usize> = seen.iter().fold(HashMap::new(), |mut m, (p, _)| {
            *m.entry(p.as_str()).or_default() += 1;
            m
        });
        assert!(paths["/worker/v1/setups/s1/heartbeat"] >= 1, "{paths:?}");
        let log: String = seen
            .iter()
            .filter(|(p, _)| p == "/worker/v1/setups/s1/logs")
            .map(|(_, b)| b["text"].as_str().unwrap().to_owned())
            .collect();
        assert_eq!(log, "fetching\n");
        let (last, body) = seen.last().unwrap();
        assert_eq!(last, "/worker/v1/setups/s1/finish");
        assert_eq!(
            *body,
            serde_json::json!({"lease_token": "secret-token", "outcome": "succeeded"})
        );
        // Its directory is gone.
        assert!(!dir.0.join("work/setups/s1").exists());
    }

    #[test]
    fn a_cancelled_build_stops_its_script() {
        let dir = Dir::new("cancelled");
        let (plugin, volume) = plugin(&dir.0, "sleep 30\n");
        let (url, seen) = fake_server(true);
        let config = config(&dir.0, &url);
        let started = Instant::now();
        let ended = run(
            &Api::new(&url, "iki_wkr_x"),
            &config,
            &have(plugin),
            &AtomicBool::new(false),
            &raw(&volume, 60),
        );
        assert_eq!(ended, Ended::Done);
        assert!(started.elapsed() < Duration::from_secs(20));
        let seen = seen.lock().unwrap();
        let (_, body) = seen.last().unwrap();
        assert_eq!(body["outcome"], "cancelled");
    }
}
