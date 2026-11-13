//! `iki-worker host <config.json>`: a cloud machine's one run container (ADR 0074).
//!
//! The machine's boot script copies this binary out of Iki's worker image and starts it
//! as a systemd service. It pulls the environment's image, runs it with this same binary
//! mounted in as its entrypoint (`iki-worker start`), and shuts the machine down when the
//! container ends, which terminates it. Locked down as the container tier's pods are: a
//! non-root user, a read-only root, no capabilities or privilege escalation, and only the
//! scratch directory writable.
//!
//! A spot machine's two-minute warning, read from its instance metadata, stops the
//! container as Kubernetes would evict a pod: the worker is sent SIGTERM, checkpoints, and
//! reports its job interrupted, so it's retried elsewhere (ADR 0069).
//!
//! When it gives up before the worker could start, it says why on the machine's console
//! as `IKI-HOST-ERROR <reason>: <message>`, which the provisioner reads back.
//!
//! An environment's volumes come as disks attached to the machine (ADR 0076): for a run,
//! each a new copy of the version's snapshot, mounted read-only; for a setup build, each
//! empty, formatted here, mounted writable for the script to fill, and unmounted once the
//! container ends so the provisioner can snapshot it whole.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use serde::Deserialize;

use crate::say;

/// The run container's name: a machine runs only one.
const CONTAINER: &str = "iki-run";
/// How many times to try pulling the image before giving up.
const PULLS: u32 = 5;
/// How often to look for a spot interruption notice: it comes two minutes ahead.
const SPOT_EVERY: Duration = Duration::from_secs(5);

/// What the machine's boot script wrote (`ec2.host_config` on the server).
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub name: String,
    pub image: String,
    pub once: bool,
    pub spot: bool,
    pub env: BTreeMap<String, String>,
    /// This binary, on the machine.
    pub worker: PathBuf,
    /// Where it's mounted in the container.
    pub worker_path: String,
    /// The registration token, in a file of its own that's deleted once it's handed over.
    pub token_file: PathBuf,
    pub scratch: PathBuf,
    pub scratch_path: String,
    pub user: String,
    pub cpus: Option<f64>,
    pub memory: Option<u64>,
    pub gpus: Option<u32>,
    /// Seconds a stopped run has to finish: as long as a pod's grace period.
    pub stop_timeout: u64,
    /// Seconds it has when the machine is being reclaimed: under EC2's two minutes.
    pub spot_stop_timeout: u64,
    /// A disk for each of the environment's volumes.
    #[serde(default)]
    pub volumes: Vec<Volume>,
}

/// One of the environment's volumes, on a disk attached to the machine.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Volume {
    pub name: String,
    /// The device it's attached as, such as `/dev/sdf`.
    pub device: String,
    /// Where it's mounted on the machine, and where the container sees it.
    pub path: PathBuf,
    pub mount: String,
    /// A setup build's: empty, to format and fill. Otherwise a copy of a snapshot, read-only.
    pub writable: bool,
}

/// How it reaches the machine: Docker, its metadata, and its own shutdown. Tests swap
/// in their own.
pub struct Machine {
    pub docker: PathBuf,
    pub shutdown: Vec<String>,
    pub metadata: String,
    /// The first wait between pulls; each one after waits longer.
    pub pause: Duration,
    /// Where `blkid`, `mkfs.ext4`, `mountpoint`, `mount`, `umount` and `chown` are: on the
    /// `PATH` when empty.
    pub tools: PathBuf,
    /// How long a volume's disk may take to be attached.
    pub attach_within: Duration,
}

impl Default for Machine {
    fn default() -> Self {
        Self {
            docker: PathBuf::from("docker"),
            shutdown: ["shutdown", "-h", "now"].map(str::to_owned).to_vec(),
            metadata: "http://169.254.169.254".to_owned(),
            pause: Duration::from_secs(10),
            tools: PathBuf::new(),
            attach_within: Duration::from_secs(300),
        }
    }
}

/// How the container ended.
#[derive(Debug, PartialEq, Eq)]
pub enum Ended {
    /// It exited by itself: the machine shuts down.
    Exited(i32),
    /// The machine is being reclaimed: it was stopped, and the machine shuts down.
    Reclaimed(i32),
    /// The service was stopped: whoever stopped it decides what happens to the machine.
    Stopped,
}

/// Something went wrong before the worker could start: why, for the provisioner.
#[derive(Debug, PartialEq, Eq)]
pub struct Trouble {
    pub reason: &'static str,
    pub message: String,
}

impl Trouble {
    fn new(reason: &'static str, message: impl Into<String>) -> Self {
        Self {
            reason,
            message: message.into(),
        }
    }
}

/// Runs the machine until its container ends, then shuts it down unless the service
/// itself was stopped.
pub fn host(config_file: &Path) -> Result<(), String> {
    let interrupted = Arc::new(AtomicBool::new(false));
    for signal in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        signal_hook::flag::register(signal, Arc::clone(&interrupted))
            .map_err(|error| format!("Couldn't watch for signals: {error}"))?;
    }
    let machine = Machine::default();
    let config = read(config_file);
    let ended = match &config {
        Ok(config) => run(config, config_file, &machine, &interrupted),
        Err(message) => Err(Trouble::new("bad_config", message.clone())),
    };
    if let (Ok(config), Ok(Ended::Exited(_) | Ended::Reclaimed(_))) = (&config, &ended) {
        release(config, &machine);
    }
    let outcome = match ended {
        Ok(Ended::Stopped) => return Ok(()),
        Ok(Ended::Exited(code) | Ended::Reclaimed(code)) if code != 0 => {
            Err(format!("The run's container exited with {code}."))
        }
        Ok(_) => Ok(()),
        Err(trouble) => {
            tell(&trouble);
            Err(trouble.message)
        }
    };
    shut_down(&machine);
    outcome
}

/// Reads the machine's config.
pub fn read(path: &Path) -> Result<Config, String> {
    let text = std::fs::read_to_string(path)
        .map_err(|e| format!("Couldn't read {}: {e}", path.display()))?;
    serde_json::from_str(&text).map_err(|e| format!("{} isn't a host config: {e}", path.display()))
}

/// Pulls the image, creates the container, and waits for it to end.
pub fn run(
    config: &Config,
    config_file: &Path,
    machine: &Machine,
    interrupted: &AtomicBool,
) -> Result<Ended, Trouble> {
    say::info(
        "Starting the run's container.",
        &[("machine", &config.name), ("image", &config.image)],
    );
    attach(config, machine, interrupted)?;
    pull(config, machine, interrupted)?;
    if interrupted.load(Ordering::SeqCst) {
        return Ok(Ended::Stopped);
    }
    let env_file = config_file.with_file_name("run.env");
    let created = write_env(config, &env_file).and_then(|()| create(config, machine, &env_file));
    // The token is in Docker's hands now, or isn't needed: it stays on disk no longer.
    let _ = std::fs::remove_file(&env_file);
    created?;
    let _ = std::fs::remove_file(&config.token_file);
    docker(machine, &["start", CONTAINER])
        .map_err(|e| Trouble::new("start_failed", format!("Couldn't start the container: {e}")))?;
    say::info("The run's container started.", &[("machine", &config.name)]);
    watch(config, machine, interrupted)
}

/// Mounts each volume's disk: a setup build's formatted first if it's empty, writable by
/// the run's user; a run's read-only.
fn attach(config: &Config, machine: &Machine, interrupted: &AtomicBool) -> Result<(), Trouble> {
    for volume in &config.volumes {
        let trouble = |message: String| {
            Trouble::new(
                "volume_unavailable",
                format!("its disk for {}: {message}", volume.name),
            )
        };
        let Some(device) = device(volume, machine, interrupted) else {
            if interrupted.load(Ordering::SeqCst) {
                return Ok(());
            }
            return Err(trouble(format!(
                "{} wasn't attached within {}s (the machine image needs udev rules that \
                 name EBS disks by their device, as Amazon Linux's do)",
                volume.device,
                machine.attach_within.as_secs()
            )));
        };
        let device = device.to_string_lossy().into_owned();
        let path = volume.path.to_string_lossy().into_owned();
        if tool(machine, "mountpoint", &["-q", &path]).is_ok() {
            continue; // an earlier start of this service mounted it
        }
        let formatted = tool(machine, "blkid", &["-p", &device]).is_ok();
        if !formatted {
            if !volume.writable {
                return Err(trouble(format!("{device} has no file system")));
            }
            say::info(
                "Formatting a volume's disk.",
                &[("volume", &volume.name), ("device", &device)],
            );
            tool(
                machine,
                "mkfs.ext4",
                &[
                    "-q",
                    "-m",
                    "0",
                    "-E",
                    "lazy_itable_init=1,lazy_journal_init=1",
                    &device,
                ],
            )
            .map_err(&trouble)?;
        }
        std::fs::create_dir_all(&volume.path)
            .map_err(|e| trouble(format!("couldn't make {path}: {e}")))?;
        // A copy of a snapshot was unmounted cleanly: its journal needn't be replayed, and
        // it can't be, read-only.
        let options = if volume.writable {
            "rw,nodev,nosuid"
        } else {
            "ro,noload,nodev,nosuid"
        };
        tool(
            machine,
            "mount",
            &["-t", "ext4", "-o", options, &device, &path],
        )
        .map_err(&trouble)?;
        if volume.writable {
            tool(machine, "chown", &[&config.user, &path]).map_err(&trouble)?;
        }
        say::info(
            "Mounted a volume's disk.",
            &[
                ("volume", &volume.name),
                ("path", &path),
                ("writable", &volume.writable),
            ],
        );
    }
    Ok(())
}

/// The volume's disk, once it's attached: its device, or the `xvd` name some images give
/// it.
fn device(volume: &Volume, machine: &Machine, interrupted: &AtomicBool) -> Option<PathBuf> {
    let named = PathBuf::from(&volume.device);
    let other = PathBuf::from(volume.device.replacen("/sd", "/xvd", 1));
    let until = Instant::now() + machine.attach_within;
    loop {
        for each in [&named, &other] {
            if each.exists() {
                return Some(std::fs::canonicalize(each).unwrap_or_else(|_| each.clone()));
            }
        }
        if Instant::now() >= until || pause(Duration::from_millis(500), interrupted) {
            return None;
        }
    }
}

/// Unmounts a setup build's disks once its container has ended, so each is whole when
/// the provisioner snapshots it.
fn release(config: &Config, machine: &Machine) {
    for volume in config.volumes.iter().filter(|v| v.writable) {
        let path = volume.path.to_string_lossy();
        if let Err(error) = tool(machine, "umount", &[&path]) {
            say::error(
                "Couldn't unmount a volume's disk.",
                &[("volume", &volume.name), ("error", &error)],
            );
        }
    }
}

/// Runs one of the machine's tools, with what it printed to stderr as the error.
fn tool(machine: &Machine, name: &str, args: &[&str]) -> Result<(), String> {
    let program = if machine.tools.as_os_str().is_empty() {
        PathBuf::from(name)
    } else {
        machine.tools.join(name)
    };
    let output = Command::new(&program)
        .args(args)
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("couldn't run {name}: {e}"))?;
    if output.status.success() {
        return Ok(());
    }
    let said = String::from_utf8_lossy(&output.stderr);
    let said = said.trim();
    Err(if said.is_empty() {
        format!("{name} failed ({})", output.status)
    } else {
        format!("{name}: {}", said.chars().take(500).collect::<String>())
    })
}

fn pull(config: &Config, machine: &Machine, interrupted: &AtomicBool) -> Result<(), Trouble> {
    let mut last = String::new();
    for attempt in 1..=PULLS {
        match docker(machine, &["pull", "--quiet", &config.image]) {
            Ok(()) => return Ok(()),
            Err(error) => last = error,
        }
        if attempt < PULLS {
            say::warn(
                "Couldn't pull the image; trying again.",
                &[("image", &config.image), ("error", &last)],
            );
            if pause(machine.pause * attempt, interrupted) {
                return Ok(());
            }
        }
    }
    Err(Trouble::new(
        "image_unavailable",
        format!("couldn't pull {}: {last}", config.image),
    ))
}

/// Sleeps, unless interrupted first: true if it was.
fn pause(wait: Duration, interrupted: &AtomicBool) -> bool {
    let until = Instant::now() + wait;
    while Instant::now() < until {
        if interrupted.load(Ordering::SeqCst) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(200).min(until - Instant::now()));
    }
    interrupted.load(Ordering::SeqCst)
}

/// The worker's environment, for `docker create --env-file`: readable by root only, and
/// deleted as soon as the container's made.
fn write_env(config: &Config, path: &Path) -> Result<(), Trouble> {
    let token = std::fs::read_to_string(&config.token_file)
        .map_err(|e| Trouble::new("bad_config", format!("Couldn't read the token: {e}")))?;
    let mut env = config.env.clone();
    env.insert("IKI_REGISTRATION_TOKEN".into(), token.trim().to_owned());
    let mut text = String::new();
    for (name, value) in &env {
        // An env file has one variable a line, unquoted.
        if name.is_empty() || name.contains(['=', '\n']) || value.contains('\n') {
            return Err(Trouble::new(
                "bad_config",
                format!("{name} can't be passed to the container"),
            ));
        }
        let _ = writeln!(text, "{name}={value}");
    }
    let _ = std::fs::remove_file(path);
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
        .open(path)
        .and_then(|mut file| file.write_all(text.as_bytes()))
        .map_err(|e| {
            Trouble::new(
                "bad_config",
                format!("Couldn't write {}: {e}", path.display()),
            )
        })
}

/// `docker create`'s arguments: the container tier's pod, as a container.
pub fn create_args(config: &Config, env_file: &Path) -> Vec<String> {
    let mut args: Vec<String> = [
        "create",
        "--name",
        CONTAINER,
        "--user",
        &config.user,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "32768",
        "--env-file",
        &env_file.to_string_lossy(),
        "--mount",
        &format!(
            "type=bind,src={},dst={},readonly",
            config.worker.display(),
            config.worker_path
        ),
        "--mount",
        &format!(
            "type=bind,src={},dst={}",
            config.scratch.display(),
            config.scratch_path
        ),
        "--log-opt",
        "max-size=10m",
        "--log-opt",
        "max-file=3",
        "--stop-timeout",
        &config.stop_timeout.to_string(),
        "--pull",
        "never",
    ]
    .map(str::to_owned)
    .to_vec();
    for volume in &config.volumes {
        let mut mount = format!(
            "type=bind,src={},dst={}",
            volume.path.display(),
            volume.mount
        );
        if !volume.writable {
            mount.push_str(",readonly");
        }
        args.extend(["--mount".into(), mount]);
    }
    if let Some(cpus) = config.cpus {
        args.extend(["--cpus".into(), cpus.to_string()]);
    }
    if let Some(memory) = config.memory {
        // No swap: what a run may use is what it asked for.
        args.extend(["--memory".into(), memory.to_string()]);
        args.extend(["--memory-swap".into(), memory.to_string()]);
    }
    if config.gpus.is_some_and(|n| n > 0) {
        args.extend(["--gpus".into(), "all".into()]);
    }
    args.extend(["--entrypoint".into(), config.worker_path.clone()]);
    args.extend([config.image.clone(), "start".into()]);
    if config.once {
        args.push("--once".into());
    }
    args
}

fn create(config: &Config, machine: &Machine, env_file: &Path) -> Result<(), Trouble> {
    // One left by an earlier start of this service is replaced.
    let _ = docker(machine, &["rm", "--force", CONTAINER]);
    let args = create_args(config, env_file);
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    docker(machine, &args).map_err(|e| {
        Trouble::new(
            "start_failed",
            format!("Couldn't create the container: {e}"),
        )
    })
}

/// Waits for the container to end, stopping it when the service is stopped or the
/// machine is about to be reclaimed.
fn watch(config: &Config, machine: &Machine, interrupted: &AtomicBool) -> Result<Ended, Trouble> {
    let mut wait = Command::new(&machine.docker)
        .args(["wait", CONTAINER])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .map_err(|e| Trouble::new("start_failed", format!("Couldn't run docker: {e}")))?;
    let mut metadata = config.spot.then(|| Metadata::new(&machine.metadata));
    let mut looked: Option<Instant> = None;
    let mut stopping: Option<bool> = None;
    loop {
        if let Some(code) = exited(&mut wait)? {
            return Ok(match stopping {
                Some(true) => Ended::Reclaimed(code),
                Some(false) => Ended::Stopped,
                None => {
                    say::info(
                        "The run's container exited.",
                        &[("machine", &config.name), ("code", &code)],
                    );
                    Ended::Exited(code)
                }
            });
        }
        if stopping.is_none() {
            if interrupted.load(Ordering::SeqCst) {
                say::info(
                    "Stopping the run's container.",
                    &[("machine", &config.name)],
                );
                stop(machine, config.stop_timeout);
                stopping = Some(false);
            } else if let Some(metadata) = metadata.as_mut()
                && looked.is_none_or(|at| at.elapsed() >= SPOT_EVERY)
            {
                looked = Some(Instant::now());
                if let Some(notice) = metadata.spot_notice() {
                    say::warn(
                        "EC2 is reclaiming this spot machine: stopping the run's container.",
                        &[("machine", &config.name), ("notice", &notice)],
                    );
                    stop(machine, config.spot_stop_timeout);
                    stopping = Some(true);
                }
            }
        }
        std::thread::sleep(Duration::from_millis(250));
    }
}

/// The container's exit code, once `docker wait` has it.
fn exited(wait: &mut Child) -> Result<Option<i32>, Trouble> {
    let Some(status) = wait
        .try_wait()
        .map_err(|e| Trouble::new("start_failed", format!("Lost docker wait: {e}")))?
    else {
        return Ok(None);
    };
    let mut out = String::new();
    if let Some(mut stdout) = wait.stdout.take() {
        let _ = std::io::Read::read_to_string(&mut stdout, &mut out);
    }
    match out.trim().parse() {
        Ok(code) if status.success() => Ok(Some(code)),
        _ => Err(Trouble::new(
            "start_failed",
            format!("docker wait failed ({status}): {}", out.trim()),
        )),
    }
}

/// SIGTERM, then SIGKILL after `timeout` seconds. It doesn't wait: `docker wait` tells.
fn stop(machine: &Machine, timeout: u64) {
    let spawned = Command::new(&machine.docker)
        .args(["stop", "--time", &timeout.to_string(), CONTAINER])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::inherit())
        .spawn();
    if let Err(error) = spawned {
        say::error("Couldn't stop the run's container.", &[("error", &error)]);
    }
}

/// Runs a docker command, with what it printed to stderr as the error.
fn docker(machine: &Machine, args: &[&str]) -> Result<(), String> {
    let output = Command::new(&machine.docker)
        .args(args)
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("couldn't run docker: {e}"))?;
    if output.status.success() {
        return Ok(());
    }
    let said = String::from_utf8_lossy(&output.stderr);
    let said = said.trim();
    Err(if said.is_empty() {
        format!("docker {} failed ({})", args[0], output.status)
    } else {
        said.chars().take(500).collect()
    })
}

/// Says why it gave up where the provisioner reads it: the machine's console.
fn tell(trouble: &Trouble) {
    let line = format!(
        "IKI-HOST-ERROR {}: {}\n",
        trouble.reason,
        trouble.message.replace('\n', " ")
    );
    let _ = std::io::stderr().lock().write_all(line.as_bytes());
}

fn shut_down(machine: &Machine) {
    let Some((program, args)) = machine.shutdown.split_first() else {
        return;
    };
    say::info("Shutting the machine down.", &[]);
    if let Err(error) = Command::new(program).args(args).status() {
        say::error("Couldn't shut the machine down.", &[("error", &error)]);
    }
}

/// The machine's own instance metadata, version 2: a session token first, then each read with it.
/// It's reached directly, never through a proxy.
struct Metadata {
    agent: ureq::Agent,
    base: String,
    token: Option<String>,
}

impl Metadata {
    fn new(base: &str) -> Self {
        let agent = ureq::Agent::config_builder()
            .proxy(None)
            .http_status_as_error(false)
            .max_redirects(0)
            .timeout_global(Some(Duration::from_secs(2)))
            .build()
            .new_agent();
        Self {
            agent,
            base: base.trim_end_matches('/').to_owned(),
            token: None,
        }
    }

    fn session(&mut self) -> Option<String> {
        if self.token.is_none() {
            let mut response = self
                .agent
                .put(&format!("{}/latest/api/token", self.base))
                .header("X-aws-ec2-metadata-token-ttl-seconds", "21600")
                .send_empty()
                .ok()?;
            if response.status().as_u16() == 200 {
                self.token = response.body_mut().read_to_string().ok();
            }
        }
        self.token.clone()
    }

    /// EC2's notice that it's reclaiming the machine, if it's given one.
    fn spot_notice(&mut self) -> Option<String> {
        for _ in 0..2 {
            let token = self.session()?;
            let mut response = self
                .agent
                .get(&format!(
                    "{}/latest/meta-data/spot/instance-action",
                    self.base
                ))
                .header("X-aws-ec2-metadata-token", &token)
                .call()
                .ok()?;
            match response.status().as_u16() {
                200 => return response.body_mut().read_to_string().ok(),
                // The session expired: a new one, and once more.
                401 => self.token = None,
                // 404: no notice.
                _ => return None,
            }
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::net::TcpListener;

    struct Dir(PathBuf);

    impl Dir {
        fn new(name: &str) -> Self {
            let path =
                std::env::temp_dir().join(format!("iki-host-test-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&path);
            std::fs::create_dir_all(&path).unwrap();
            Self(path)
        }
    }

    impl Drop for Dir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    /// A docker that logs what it's asked, waits until it's stopped (or a second has gone
    /// with `exit` set), and fails `pull` with `fail_pull`.
    fn fake_docker(dir: &Path) -> PathBuf {
        let script = dir.join("docker");
        let text = format!(
            r#"#!/bin/sh
d='{dir}'
echo "$*" >> "$d/calls"
case "$1" in
  pull) [ -e "$d/fail_pull" ] && {{ echo "denied: no access" >&2; exit 1; }}; exit 0 ;;
  create) env_file=$(echo "$*" | sed 's/.*--env-file \([^ ]*\).*/\1/'); cp "$env_file" "$d/seen.env"; ls -l "$env_file" | cut -c1-10 > "$d/env.mode"; exit 0 ;;
  stop) touch "$d/stopped"; exit 0 ;;
  wait)
    n=0
    while [ ! -e "$d/stopped" ]; do
      n=$((n + 1))
      [ -e "$d/exit" ] && [ "$n" -gt 5 ] && {{ cat "$d/exit"; exit 0; }}
      sleep 0.2
    done
    echo 143 ;;
esac
"#,
            dir = dir.display()
        );
        std::fs::write(&script, text).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        script
    }

    fn setup(dir: &Path, spot: bool) -> (Config, PathBuf, Machine) {
        let token = dir.join("token");
        std::fs::write(&token, "iki_wlch_secret\n").unwrap();
        let config_file = dir.join("host.json");
        let json = serde_json::json!({
            "name": "iki-run-1",
            "image": "ghcr.io/acme/alphafold@sha256:99",
            "once": true,
            "spot": spot,
            "env": {"IKI_URL": "https://iki.example.com", "IKI_WORKER_NAME": "iki-run-1"},
            "worker": "/opt/iki/bin/iki-worker",
            "worker_path": "/iki/bin/iki-worker",
            "token_file": token,
            "scratch": "/var/lib/iki/scratch",
            "scratch_path": "/iki/scratch",
            "user": "10001:10001",
            "cpus": 96.0,
            "memory": 1_236_950_581_248_u64,
            "gpus": 8,
            "stop_timeout": 300,
            "spot_stop_timeout": 100,
        });
        std::fs::write(&config_file, json.to_string()).unwrap();
        let config = read(&config_file).unwrap();
        let machine = Machine {
            docker: fake_docker(dir),
            shutdown: vec![],
            metadata: "http://127.0.0.1:9".into(),
            pause: Duration::ZERO,
            tools: fake_tools(dir),
            attach_within: Duration::from_secs(5),
        };
        (config, config_file, machine)
    }

    /// Disk tools that log what they're asked to `tools`: `blkid` finds a file system
    /// only once `formatted` exists (as `mkfs.ext4` makes it), and `mountpoint` says no.
    fn fake_tools(dir: &Path) -> PathBuf {
        let tools = dir.join("bin");
        std::fs::create_dir_all(&tools).unwrap();
        for (name, body) in [
            ("blkid", "[ -e \"$d/formatted\" ]"),
            ("mkfs.ext4", "touch \"$d/formatted\""),
            ("mountpoint", "exit 1"),
            ("mount", "exit 0"),
            ("umount", "exit 0"),
            ("chown", "exit 0"),
        ] {
            let script = tools.join(name);
            std::fs::write(
                &script,
                format!(
                    "#!/bin/sh\nd='{}'\necho \"{name} $*\" >> \"$d/tools\"\n{body}\n",
                    dir.display()
                ),
            )
            .unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
            }
        }
        tools
    }

    fn tools(dir: &Path) -> Vec<String> {
        std::fs::read_to_string(dir.join("tools"))
            .unwrap_or_default()
            .lines()
            .map(|line| line.replace(&dir.display().to_string(), "<dir>"))
            .collect()
    }

    /// A config whose machine has a disk attached for `af-db`, as `device` (made here).
    fn with_volume(dir: &Path, writable: bool, attached: bool) -> (Config, PathBuf, Machine) {
        let (_, file, machine) = setup(dir, false);
        let device = dir.join("sdf");
        if attached {
            std::fs::write(&device, "").unwrap();
        }
        let mut json: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&file).unwrap()).unwrap();
        json["volumes"] = serde_json::json!([{
            "name": "af-db",
            "device": device,
            "path": dir.join("volumes/af-db"),
            "mount": "/data/af-db",
            "writable": writable,
        }]);
        std::fs::write(&file, json.to_string()).unwrap();
        (read(&file).unwrap(), file, machine)
    }

    fn calls(dir: &Path) -> Vec<String> {
        std::fs::read_to_string(dir.join("calls"))
            .unwrap_or_default()
            .lines()
            .map(str::to_owned)
            .collect()
    }

    #[test]
    fn it_runs_the_worker_in_the_image_locked_down_and_hands_over_the_token() {
        let dir = Dir::new("runs");
        let (config, file, machine) = setup(&dir.0, false);
        std::fs::write(dir.0.join("exit"), "0\n").unwrap();
        let ended = run(&config, &file, &machine, &AtomicBool::new(false));
        assert_eq!(ended, Ok(Ended::Exited(0)));
        let calls = calls(&dir.0);
        assert_eq!(calls[0], "pull --quiet ghcr.io/acme/alphafold@sha256:99");
        let create = calls
            .iter()
            .find(|c| c.starts_with("create"))
            .expect("no create");
        for flag in [
            "--user 10001:10001",
            "--read-only",
            "--cap-drop ALL",
            "--security-opt no-new-privileges",
            "--mount type=bind,src=/opt/iki/bin/iki-worker,dst=/iki/bin/iki-worker,readonly",
            "--mount type=bind,src=/var/lib/iki/scratch,dst=/iki/scratch ",
            "--memory 1236950581248 --memory-swap 1236950581248",
            "--cpus 96",
            "--gpus all",
            "--pull never",
            "--entrypoint /iki/bin/iki-worker ghcr.io/acme/alphafold@sha256:99 start --once",
        ] {
            assert!(create.contains(flag), "{flag} not in {create}");
        }
        assert!(calls.contains(&"start iki-run".to_owned()), "{calls:?}");
        let seen = std::fs::read_to_string(dir.0.join("seen.env")).unwrap_or_default();
        assert!(
            seen.contains("IKI_REGISTRATION_TOKEN=iki_wlch_secret\n"),
            "{seen}"
        );
        assert!(seen.contains("IKI_URL=https://iki.example.com\n"), "{seen}");
        let mode = std::fs::read_to_string(dir.0.join("env.mode")).unwrap_or_default();
        assert_eq!(mode.trim(), "-rw-------");
        // Neither the token nor the env file is left on the machine.
        assert!(!config.token_file.exists());
        assert!(!file.with_file_name("run.env").exists());
    }

    #[test]
    fn a_setup_builds_disk_is_formatted_mounted_writable_and_released() {
        let dir = Dir::new("fill");
        let (config, file, machine) = with_volume(&dir.0, true, true);
        std::fs::write(dir.0.join("exit"), "0\n").unwrap();
        let ended = run(&config, &file, &machine, &AtomicBool::new(false));
        assert_eq!(ended, Ok(Ended::Exited(0)));
        release(&config, &machine);
        let device = std::fs::canonicalize(dir.0.join("sdf")).unwrap();
        let device = device
            .display()
            .to_string()
            .replace(&dir.0.display().to_string(), "<dir>");
        assert_eq!(
            tools(&dir.0),
            [
                "mountpoint -q <dir>/volumes/af-db".to_owned(),
                format!("blkid -p {device}"),
                format!("mkfs.ext4 -q -m 0 -E lazy_itable_init=1,lazy_journal_init=1 {device}"),
                format!("mount -t ext4 -o rw,nodev,nosuid {device} <dir>/volumes/af-db"),
                "chown 10001:10001 <dir>/volumes/af-db".to_owned(),
                "umount <dir>/volumes/af-db".to_owned(),
            ]
        );
        let create = calls(&dir.0)
            .into_iter()
            .find(|c| c.starts_with("create"))
            .expect("no create");
        let mount = format!(
            "--mount type=bind,src={}/volumes/af-db,dst=/data/af-db ",
            dir.0.display()
        );
        assert!(create.contains(&mount), "{create}");
    }

    #[test]
    fn a_runs_copy_of_a_snapshot_is_mounted_read_only_as_it_is() {
        let dir = Dir::new("copy");
        let (config, file, machine) = with_volume(&dir.0, false, true);
        std::fs::write(dir.0.join("formatted"), "").unwrap();
        std::fs::write(dir.0.join("exit"), "0\n").unwrap();
        let ended = run(&config, &file, &machine, &AtomicBool::new(false));
        assert_eq!(ended, Ok(Ended::Exited(0)));
        release(&config, &machine);
        let seen = tools(&dir.0);
        assert!(!seen.iter().any(|c| c.starts_with("mkfs")), "{seen:?}");
        assert!(!seen.iter().any(|c| c.starts_with("chown")), "{seen:?}");
        assert!(!seen.iter().any(|c| c.starts_with("umount")), "{seen:?}");
        assert!(
            seen.iter()
                .any(|c| c.starts_with("mount -t ext4 -o ro,noload,nodev,nosuid ")),
            "{seen:?}"
        );
        let create = calls(&dir.0)
            .into_iter()
            .find(|c| c.starts_with("create"))
            .expect("no create");
        let mount = format!(
            "--mount type=bind,src={}/volumes/af-db,dst=/data/af-db,readonly",
            dir.0.display()
        );
        assert!(create.contains(&mount), "{create}");
    }

    #[test]
    fn a_disk_never_attached_or_unformatted_is_why_it_gave_up() {
        let dir = Dir::new("detached");
        let (config, file, mut machine) = with_volume(&dir.0, false, false);
        machine.attach_within = Duration::from_millis(100);
        let trouble = run(&config, &file, &machine, &AtomicBool::new(false)).unwrap_err();
        assert_eq!(trouble.reason, "volume_unavailable");
        assert!(trouble.message.contains("wasn't attached"), "{trouble:?}");
        assert!(!calls(&dir.0).iter().any(|c| c.starts_with("pull")));
        // A copy with no file system isn't formatted: it isn't the snapshot.
        std::fs::write(dir.0.join("sdf"), "").unwrap();
        let trouble = run(&config, &file, &machine, &AtomicBool::new(false)).unwrap_err();
        assert!(
            trouble.message.contains("has no file system"),
            "{trouble:?}"
        );
        assert!(!tools(&dir.0).iter().any(|c| c.starts_with("mkfs")));
    }

    #[test]
    fn an_image_it_cant_pull_is_why_it_gave_up() {
        let dir = Dir::new("pull");
        let (config, file, machine) = setup(&dir.0, false);
        std::fs::write(dir.0.join("fail_pull"), "").unwrap();
        let ended = run(&config, &file, &machine, &AtomicBool::new(false));
        let trouble = ended.unwrap_err();
        assert_eq!(trouble.reason, "image_unavailable");
        assert!(trouble.message.contains("denied: no access"), "{trouble:?}");
        assert_eq!(
            calls(&dir.0)
                .iter()
                .filter(|c| c.starts_with("pull"))
                .count(),
            5
        );
        assert!(!calls(&dir.0).iter().any(|c| c.starts_with("create")));
    }

    #[test]
    fn stopping_the_service_gives_the_run_its_grace_period() {
        let dir = Dir::new("stop");
        let (config, file, machine) = setup(&dir.0, false);
        let interrupted = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&interrupted);
        let watched = dir.0.clone();
        // Once the container is running (its `docker wait` has begun), however long that
        // took on a busy machine.
        let stopper = std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(30);
            while !calls(&watched).iter().any(|c| c.starts_with("wait"))
                && Instant::now() < deadline
            {
                std::thread::sleep(Duration::from_millis(20));
            }
            flag.store(true, Ordering::SeqCst);
        });
        let ended = run(&config, &file, &machine, &interrupted);
        let _ = stopper.join();
        assert_eq!(ended, Ok(Ended::Stopped));
        assert!(calls(&dir.0).contains(&"stop --time 300 iki-run".to_owned()));
    }

    /// Instance metadata that issues a session, then answers instance-action with a notice.
    fn fake_metadata() -> (String, std::thread::JoinHandle<Vec<String>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let served = std::thread::spawn(move || {
            let mut seen = vec![];
            for stream in listener.incoming().take(2) {
                let mut stream = stream.unwrap();
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut request = String::new();
                loop {
                    let mut line = String::new();
                    if reader.read_line(&mut line).unwrap_or(0) == 0 || line == "\r\n" {
                        break;
                    }
                    request.push_str(&line);
                }
                let body = if request.starts_with("PUT /latest/api/token") {
                    "session-1"
                } else {
                    r#"{"action": "terminate", "time": "2026-11-12T08:22:00Z"}"#
                };
                let _ = write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                seen.push(request.to_lowercase());
            }
            seen
        });
        (url, served)
    }

    #[test]
    fn a_spot_notice_stops_the_run_in_time_to_checkpoint() {
        let dir = Dir::new("spot");
        let (config, file, mut machine) = setup(&dir.0, true);
        let (url, served) = fake_metadata();
        machine.metadata = url;
        let ended = run(&config, &file, &machine, &AtomicBool::new(false));
        assert_eq!(ended, Ok(Ended::Reclaimed(143)));
        assert!(calls(&dir.0).contains(&"stop --time 100 iki-run".to_owned()));
        let seen = served.join().unwrap();
        assert!(seen[0].contains("x-aws-ec2-metadata-token-ttl-seconds: 21600"));
        assert!(seen[1].starts_with("get /latest/meta-data/spot/instance-action"));
        assert!(seen[1].contains("x-aws-ec2-metadata-token: session-1"));
    }

    #[test]
    fn its_config_is_only_what_the_server_writes() {
        let dir = Dir::new("config");
        let file = dir.0.join("host.json");
        std::fs::write(&file, r#"{"name": "x", "surprise": true}"#).unwrap();
        assert!(read(&file).is_err());
    }
}
