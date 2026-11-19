//! A Slurm cluster's worker (ADR 0133): on the cluster's login node, it leases the jobs
//! the deployment's Slurm templates place on the cluster, many at once, and submits each
//! with `sbatch` rather than running it itself.
//!
//! Each job is staged on the login node into its own directory under
//! `IKI_SLURM_WORK_DIR`, on a file system the compute nodes share, so they need no way to
//! Iki or object storage: its input files, `input.json`, its plugin's code, and the
//! command to run (`step.json`). Its secrets and proxy credentials go in `secrets.json`,
//! which the job reads once and deletes; the worker's own token is never there. On its
//! node, `iki-worker slurm-step` runs the command in the environment's image under
//! Apptainer (`step`), pulled by its digest once its signature checks out (`image`).
//!
//! While it waits in the cluster's queue and runs, the worker renews its lease, says what
//! Slurm reports of it, and streams the log its command writes. One `squeue` asks after
//! every job the worker holds, every `IKI_SLURM_POLL_SECONDS`. A cancelled run is
//! `scancel`led. Once a job has left the queue, `sacct` says how it ended and what it
//! used, which the server prices it by; its results go up from the login node.
//!
//! The worker can restart while its jobs wait and run: their leases outlast it, and each
//! job's directory keeps what it needs (`slurm.json`) to pick the job up again. A job
//! whose lease went elsewhere meanwhile is `scancel`led.

pub mod commands;
pub mod image;
pub mod step;

use std::collections::{BTreeMap, BTreeSet};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::ExitStatus;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::thread;
use std::time::{Duration, Instant};

use iki_protocol::{FailInReason, HeartbeatInState, RunInput, RunInputJobSlurm, fill, paths};
use serde::{Deserialize, Serialize};

use crate::api::{self, Api, Error};
use crate::config::Config;
use crate::job::{self, Dirs, Ended, Held, Host, Job, Plan, Ran, Report, Stop};
use crate::logs;
use crate::plugins;
use crate::say;
use commands::{Accounted, Phase, Queued, Slurm};

/// How a cluster's worker is set up, from `IKI_SLURM_*`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Settings {
    /// Where each job's directory goes, on a file system the compute nodes mount at the
    /// same path.
    pub work_dir: PathBuf,
    /// Where images are kept as SIF files, shared by every job.
    pub image_cache: PathBuf,
    pub slurm: Slurm,
    pub tools: image::Tools,
    /// The most jobs it holds at once. The templates' `max_concurrent` limits them too.
    pub max_jobs: usize,
    /// How often it asks `squeue` after its jobs.
    pub poll_every: Duration,
}

impl Settings {
    /// The settings, if `IKI_SLURM_WORK_DIR` says this worker submits to Slurm.
    pub fn from_env(get: &dyn Fn(&str) -> Option<String>) -> Result<Option<Self>, String> {
        let Some(work_dir) = get("IKI_SLURM_WORK_DIR").map(PathBuf::from) else {
            return Ok(None);
        };
        if !work_dir.is_absolute() {
            return Err(
                "IKI_SLURM_WORK_DIR must be an absolute directory that the cluster's \
                 compute nodes mount at the same path."
                    .to_owned(),
            );
        }
        let number = |name: &str, default: u64| -> Result<u64, String> {
            get(name).map_or(Ok(default), |raw| {
                raw.parse::<u64>()
                    .ok()
                    .filter(|n| *n > 0)
                    .ok_or_else(|| format!("{name} must be a whole number above 0."))
            })
        };
        Ok(Some(Self {
            image_cache: get("IKI_SLURM_IMAGE_CACHE")
                .map_or_else(|| work_dir.join("images"), PathBuf::from),
            slurm: Slurm {
                bin: get("IKI_SLURM_BIN").map(PathBuf::from),
            },
            tools: image::Tools {
                // Compute nodes run it by the path the login node finds it at: a batch
                // job's PATH is the node's, not the worker's.
                apptainer: found(
                    &get("IKI_APPTAINER").unwrap_or_else(|| "apptainer".to_owned()),
                    get("PATH").as_deref(),
                )
                .ok_or(
                    "Couldn't find apptainer on PATH: install Apptainer on the login node and \
                     compute nodes, at the same path, or set IKI_APPTAINER.",
                )?,
                cosign: get("IKI_COSIGN").unwrap_or_else(|| "cosign".to_owned()),
            },
            max_jobs: usize::try_from(number("IKI_SLURM_MAX_JOBS", 100)?).unwrap_or(100),
            poll_every: Duration::from_secs(number("IKI_SLURM_POLL_SECONDS", 15)?),
            work_dir,
        }))
    }
}

/// `program`'s absolute path: as given if it's a path, else the first on `path`.
fn found(program: &str, path: Option<&str>) -> Option<String> {
    if program.contains('/') {
        return Some(program.to_owned());
    }
    std::env::split_paths(path?)
        .map(|dir| dir.join(program))
        .find(|candidate| candidate.is_file())
        .map(|candidate| candidate.to_string_lossy().into_owned())
}

/// A directory only this user can read, made if it isn't there.
pub fn private_dir(dir: &Path) -> std::io::Result<()> {
    let mut builder = std::fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    std::os::unix::fs::DirBuilderExt::mode(&mut builder, 0o700);
    builder.create(dir)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

/// Writes `bytes` to `path` where only this user can read it, replacing it in one step.
fn write_private(path: &Path, bytes: &[u8], mode: u32) -> std::io::Result<()> {
    use std::io::Write;
    let partial = path.with_extension("partial");
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, mode);
    #[cfg(not(unix))]
    let _ = mode;
    let mut file = options.open(&partial)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    std::fs::rename(partial, path)
}

/// What a job's directory keeps, to pick the job up again after a restart.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct Kept {
    job: String,
    lease_token: String,
    /// Its Slurm job's name, `iki-<run>-<attempt>`, to find it by if the worker stopped
    /// between submitting it and noting its ID.
    name: String,
    slurm_id: Option<String>,
    heartbeat_every: i64,
}

const KEPT: &str = "slurm.json";
/// The batch script's note of how the command exited, for a cluster without accounting.
const EXIT: &str = "exit";

fn keep(dirs: &Dirs, kept: &Kept) -> Result<(), String> {
    let bytes = serde_json::to_vec(kept).map_err(|e| e.to_string())?;
    write_private(&dirs.root.join(KEPT), &bytes, 0o600)
        .map_err(|e| format!("Couldn't note the job in its directory: {e}"))
}

// --- Asking after every job at once --------------------------------------------------------

/// What `squeue` last said of a job.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Seen {
    Queued(Queued),
    /// It's left the queue.
    Gone,
}

/// Asks `squeue` after every job the worker holds, in one call, every so often.
struct Poller {
    slurm: Slurm,
    every: Duration,
    tracked: Mutex<BTreeSet<String>>,
    seen: Mutex<BTreeMap<String, Seen>>,
}

impl Poller {
    fn start(slurm: Slurm, every: Duration, stopping: Arc<AtomicBool>) -> Arc<Self> {
        let poller = Arc::new(Self {
            slurm,
            every,
            tracked: Mutex::default(),
            seen: Mutex::default(),
        });
        let polling = Arc::clone(&poller);
        thread::spawn(move || {
            while !stopping.load(Ordering::SeqCst) {
                polling.poll();
                let until = Instant::now() + polling.every;
                while Instant::now() < until && !stopping.load(Ordering::SeqCst) {
                    thread::sleep(Duration::from_millis(100));
                }
            }
        });
        poller
    }

    fn poll(&self) {
        let ids: Vec<String> = self
            .tracked
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .cloned()
            .collect();
        if ids.is_empty() {
            return;
        }
        match self.slurm.queued(&ids) {
            Ok(mut found) => {
                let mut seen = self.seen.lock().unwrap_or_else(PoisonError::into_inner);
                for id in ids {
                    let now = found.remove(&id).map_or(Seen::Gone, Seen::Queued);
                    seen.insert(id, now);
                }
            }
            Err(error) => say::warn(
                "Couldn't ask squeue after the jobs; asking again soon.",
                &[("detail", &error)],
            ),
        }
    }

    fn track(&self, id: &str) {
        self.tracked
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .insert(id.to_owned());
    }

    fn forget(&self, id: &str) {
        self.tracked
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .remove(id);
        self.seen
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .remove(id);
    }

    fn seen(&self, id: &str) -> Option<Seen> {
        self.seen
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .get(id)
            .cloned()
    }
}

// --- Serving the cluster's jobs ------------------------------------------------------------

/// What every job's thread shares.
struct Cluster {
    api: Api,
    config: Config,
    settings: Settings,
    have: Mutex<plugins::Have>,
    poller: Arc<Poller>,
    interrupted: Arc<AtomicBool>,
    unauthorized: AtomicBool,
    held: AtomicUsize,
    /// This worker's binary, where compute nodes run it from.
    step_binary: PathBuf,
}

/// Seconds a lease request waits for work.
const LEASE_WAIT: u32 = 20;

/// Leases the cluster's jobs and submits them, holding up to `IKI_SLURM_MAX_JOBS` at once,
/// until SIGTERM or SIGINT. Its jobs run on: it picks them up again when it starts.
pub fn serve(
    api: &Api,
    config: &Config,
    settings: &Settings,
    have: plugins::Have,
    interrupted: &Arc<AtomicBool>,
    mut idle: impl FnMut(&mut plugins::Have) -> Option<Result<(), String>>,
) -> Result<(), String> {
    for dir in [&settings.work_dir, &settings.work_dir.join("jobs")] {
        private_dir(dir).map_err(|e| format!("Couldn't make {}: {e}", dir.display()))?;
    }
    let step_binary = install_step_binary(&settings.work_dir)?;
    let cluster = Arc::new(Cluster {
        api: api.clone(),
        config: config.clone(),
        settings: settings.clone(),
        have: Mutex::new(have),
        poller: Poller::start(
            settings.slurm.clone(),
            settings.poll_every,
            Arc::clone(interrupted),
        ),
        interrupted: Arc::clone(interrupted),
        unauthorized: AtomicBool::new(false),
        held: AtomicUsize::new(0),
        step_binary,
    });
    let mut threads = adopt(&cluster);
    let lease = format!("{}?wait={LEASE_WAIT}", paths::LEASE);
    let mut failures = 0;
    let mut outcome = Ok(());
    say::info(
        "Waiting for the cluster's jobs.",
        &[("server", &config.url), ("max_jobs", &settings.max_jobs)],
    );
    while !interrupted.load(Ordering::SeqCst) && !cluster.unauthorized.load(Ordering::SeqCst) {
        threads.retain(|t| !t.is_finished());
        if cluster.held.load(Ordering::SeqCst) >= settings.max_jobs {
            pause(Duration::from_secs(2), interrupted);
            continue;
        }
        match api.post::<_, serde_json::Value>(&lease, &serde_json::json!({})) {
            Ok(Some(raw)) => {
                failures = 0;
                cluster.held.fetch_add(1, Ordering::SeqCst);
                let cluster = Arc::clone(&cluster);
                threads.push(thread::spawn(move || {
                    let ended = submit(&cluster, &raw);
                    cluster.ended(ended);
                }));
            }
            Ok(None) => {
                failures = 0;
                if cluster.held.load(Ordering::SeqCst) == 0 {
                    let mut have = cluster.have.lock().unwrap_or_else(PoisonError::into_inner);
                    if let Some(stop) = idle(&mut have) {
                        outcome = stop;
                        break;
                    }
                }
            }
            Err(Error::Unauthorized(_)) => {
                cluster.unauthorized.store(true, Ordering::SeqCst);
            }
            Err(Error::Refused { code, detail, .. }) if code == "worker_outdated" => {
                outcome = Err(detail);
                break;
            }
            Err(error) => {
                failures += 1;
                let wait = api::backoff(failures);
                say::warn(
                    "Couldn't lease; trying again.",
                    &[("detail", &error), ("in_seconds", &wait.as_secs())],
                );
                pause(wait, interrupted);
            }
        }
    }
    if cluster.unauthorized.load(Ordering::SeqCst) {
        // Its jobs' threads see their leases go, and stop.
        interrupted.store(true, Ordering::SeqCst);
        outcome = Err(
            "The server no longer accepts this worker's token: a Compute admin \
                       revoked it. Its Slurm jobs were cancelled."
                .to_owned(),
        );
    } else if interrupted.load(Ordering::SeqCst) {
        say::info(
            "Stopping; the cluster's jobs run on, and are picked up again when the worker \
             starts.",
            &[("jobs", &cluster.held.load(Ordering::SeqCst))],
        );
    }
    for thread in threads {
        let _ = thread.join();
    }
    outcome
}

impl Cluster {
    fn ended(&self, ended: Ended) {
        if ended == Ended::Unauthorized {
            self.unauthorized.store(true, Ordering::SeqCst);
        }
        self.held.fetch_sub(1, Ordering::SeqCst);
    }

    fn dirs(&self, job: &str) -> Dirs {
        Dirs::new(&self.settings.work_dir, job)
    }

    /// Removes the job's directory, unless it's kept to debug.
    fn clean(&self, dirs: &Dirs) {
        if self.config.keep_job_dirs {
            return;
        }
        if let Err(error) = std::fs::remove_dir_all(&dirs.root)
            && error.kind() != std::io::ErrorKind::NotFound
        {
            say::warn(
                "Couldn't remove a job's directory.",
                &[("path", &dirs.root.display()), ("detail", &error)],
            );
        }
    }
}

fn pause(wait: Duration, interrupted: &AtomicBool) {
    let end = Instant::now() + wait;
    while Instant::now() < end && !interrupted.load(Ordering::SeqCst) {
        thread::sleep(Duration::from_millis(100));
    }
}

/// Copies this binary into the shared work directory, where compute nodes run it from:
/// the login node's own copy may be on a disk of its own.
fn install_step_binary(work_dir: &Path) -> Result<PathBuf, String> {
    let bin = work_dir.join("bin");
    private_dir(&bin).map_err(|e| format!("Couldn't make {}: {e}", bin.display()))?;
    let target = bin.join(format!("iki-worker-{}", env!("CARGO_PKG_VERSION")));
    let exe =
        std::env::current_exe().map_err(|e| format!("Couldn't tell where iki-worker is: {e}"))?;
    let bytes = std::fs::read(&exe).map_err(|e| format!("Couldn't read {}: {e}", exe.display()))?;
    if std::fs::read(&target).ok().as_deref() != Some(bytes.as_slice()) {
        write_private(&target, &bytes, 0o700)
            .map_err(|e| format!("Couldn't copy iki-worker to {}: {e}", target.display()))?;
    }
    Ok(target)
}

// --- One job -------------------------------------------------------------------------------

/// Submits a job the worker just leased, and sees it through.
fn submit(cluster: &Arc<Cluster>, raw: &serde_json::Value) -> Ended {
    let input: RunInput = match serde_json::from_value(raw.clone()) {
        Ok(input) => input,
        Err(error) => return job::unreadable(&cluster.api, raw, &error.to_string()),
    };
    let held = Held::start(
        &cluster.api,
        &input.job.id,
        &input.job.lease_token,
        HeartbeatInState::Staging,
        input.job.heartbeat_every,
        false,
    );
    say::info(
        "Leased a job for the cluster.",
        &[
            ("job", &input.job.id),
            ("run", &input.run.hrid),
            ("action", &input.run.action),
            ("attempt", &input.run.attempt),
        ],
    );
    let Some(placed) = input.job.slurm.clone() else {
        return held.end(Some(Report::Fail(
            FailInReason::Failed,
            "This worker submits jobs to a Slurm cluster, and the server sent one with \
             nowhere to go on it."
                .into(),
        )));
    };
    let dirs = cluster.dirs(&input.job.id);
    let plugin_dir = dirs.root.join("plugin");
    let plan = Plan {
        input: &input,
        plugin_dir: &plugin_dir,
        built: None,
        timeout: Duration::from_secs(u64::try_from(input.job.timeout.max(1)).unwrap_or(3600)),
    };
    let host = Arc::clone(&held.job);
    if let Err(report) = job::prepare(&host, &plan, &dirs, raw) {
        let ended = held.end(report);
        cluster.clean(&dirs);
        return ended;
    }
    let name = format!("iki-{}-{}", input.run.hrid, input.run.attempt);
    let kept = Kept {
        job: input.job.id.clone(),
        lease_token: input.job.lease_token.clone(),
        name,
        slurm_id: None,
        heartbeat_every: input.job.heartbeat_every,
    };
    let report = job::conclude(&host, &plan, &dirs, |host, said, mask| {
        let id = start(cluster, host, &plan, &placed, &dirs, &kept)?;
        let kept = Kept {
            slurm_id: Some(id),
            ..kept.clone()
        };
        Ok(watch(
            cluster, host, &plan, &placed, &dirs, &kept, said, mask,
        ))
    });
    see_through(cluster, held, &dirs, report)
}

/// Reports how the job ended, or lets it run on while the worker restarts.
fn see_through(cluster: &Cluster, held: Held, dirs: &Dirs, report: Option<Report>) -> Ended {
    if cluster.interrupted.load(Ordering::SeqCst)
        && !cluster.unauthorized.load(Ordering::SeqCst)
        && report.is_none()
        && !held.job.is_lost()
    {
        say::info(
            "Left a job running on the cluster.",
            &[("job", &held.job.id)],
        );
        return held.detach();
    }
    let ended = held.end(report);
    cluster.clean(dirs);
    ended
}

/// Pulls the image, writes the step and submits it. Returns its Slurm job's ID.
fn start(
    cluster: &Cluster,
    host: &Arc<Job>,
    plan: &Plan,
    placed: &RunInputJobSlurm,
    dirs: &Dirs,
    kept: &Kept,
) -> Result<String, Report> {
    let failed = |message: String| Report::Fail(FailInReason::Failed, message);
    let input = plan.input;
    copy_code(cluster, plan)?;
    let pulled = image::ensure(
        &cluster.settings.tools,
        &cluster.settings.image_cache,
        &placed.image,
        placed.image_policy.as_ref(),
    )
    .map_err(failed)?;
    let (program, args) = job::command_line(plan).map_err(failed)?;
    let (env, secrets) = step_environment(dirs, input);
    let step = step::Step {
        apptainer: cluster.settings.tools.apptainer.clone(),
        image: pulled.sif,
        dir: dirs.root.clone(),
        workdir: plan.plugin_dir.to_path_buf(),
        gpus: placed.gpus.unwrap_or_default() > 0,
        program: program.to_string_lossy().into_owned(),
        args,
        env,
    };
    let write = |name: &str, bytes: serde_json::Result<Vec<u8>>| {
        bytes
            .map_err(std::io::Error::other)
            .and_then(|bytes| write_private(&dirs.root.join(name), &bytes, 0o600))
            .map_err(|e| failed(format!("Couldn't write the job's {name}: {e}")))
    };
    write(step::STEP, serde_json::to_vec(&step))?;
    write(
        step::SECRETS,
        serde_json::to_vec(&step::Secrets { env: secrets }),
    )?;
    let script = dirs.root.join("job.sh");
    let quote = |p: &Path| format!("'{}'", p.to_string_lossy().replace('\'', r"'\''"));
    let body = format!(
        "#!/bin/sh\n# Iki job {job}: runs its command on this node (ADR 0133).\n\
         {bin} slurm-step {dir}\nstatus=$?\necho \"$status\" > {exit}\nexit \"$status\"\n",
        job = kept.job,
        bin = quote(&cluster.step_binary),
        dir = quote(&dirs.root),
        exit = quote(&dirs.root.join(EXIT)),
    );
    write_private(&script, body.as_bytes(), 0o700)
        .map_err(|e| failed(format!("Couldn't write the job's batch script: {e}")))?;
    keep(dirs, kept).map_err(failed)?;
    if host.is_lost() {
        return Err(failed(
            "Its lease went elsewhere before it was submitted.".into(),
        ));
    }
    let comment = format!("iki:{}", kept.job);
    let args = commands::sbatch_args(placed, &kept.name, &comment, &dirs.root);
    let id = cluster.settings.slurm.submit(&args, &script).map_err(|e| {
        failed(format!(
            "The cluster refused the job: {e} A Compute admin can check the `{}` \
             template's partition and account.",
            placed.template
        ))
    })?;
    keep(
        dirs,
        &Kept {
            slurm_id: Some(id.clone()),
            ..kept.clone()
        },
    )
    .map_err(failed)?;
    say::info(
        "Submitted a job to the cluster.",
        &[
            ("job", &kept.job),
            ("slurm_job", &id),
            ("partition", &placed.partition),
        ],
    );
    host.line(&format!(
        "iki-worker: Submitted Slurm job {id} to the {} partition, charged to {}.\n",
        placed.partition, placed.account
    ));
    host.state(HeartbeatInState::Pending);
    report(
        host,
        &serde_json::json!({
            "slurm_id": id,
            "state": "PENDING",
            "submitted_at": now(),
            "image_signer": pulled.signer,
        }),
    );
    Ok(id)
}

/// Copies its plugin's code, as the worker has it now, into the job's own directory: the
/// compute node runs it from there, and the worker fetching newer code doesn't change it.
fn copy_code(cluster: &Cluster, plan: &Plan) -> Result<(), Report> {
    let failed = |message: String| Report::Fail(FailInReason::Failed, message);
    let input = plan.input;
    let code = {
        let have = cluster.have.lock().unwrap_or_else(PoisonError::into_inner);
        let name = &input.run.plugin.name;
        match have.code.iter().find(|p| &p.name == name) {
            None => {
                return Err(failed(format!(
                    "This worker has no code for the plugin {name}."
                )));
            }
            Some(p) if p.sha.as_deref() != input.run.plugin.sha.as_deref() => {
                return Err(failed(format!(
                    "This worker has other code for {name} than the run was launched with."
                )));
            }
            Some(_) => have.dir(name).map_or_else(
                || {
                    cluster
                        .config
                        .plugins_dir
                        .as_deref()
                        .unwrap_or(&cluster.config.cache_dir)
                        .join(plugins::label(name))
                },
                Path::to_path_buf,
            ),
        }
    };
    copy_tree(&code, plan.plugin_dir)
        .map_err(|e| failed(format!("Couldn't copy the plugin's code for the job: {e}")))
}

/// Environment variables, by name.
type Variables = Vec<(String, String)>;

/// The command's environment, but for its secrets and proxy credentials, which go apart.
fn step_environment(dirs: &Dirs, input: &RunInput) -> (Variables, Variables) {
    let secret_names: BTreeSet<String> = input
        .job
        .secrets
        .iter()
        .flat_map(|s| s.keys().cloned())
        .collect();
    // The image has its own programs and certificates; the job its own secrets.
    let host_only = ["PATH", "MAMBA_ROOT_PREFIX", "SSL_CERT_FILE", "SSL_CERT_DIR"];
    let proxied = ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"];
    let env = job::environment(dirs, input, None)
        .into_iter()
        .filter(|(name, _)| {
            !host_only.contains(&name.as_str())
                && !proxied.contains(&name.as_str())
                && !secret_names.contains(name)
        })
        .collect();
    let mut secrets: Vec<(String, String)> = input
        .job
        .secrets
        .iter()
        .flat_map(|s| s.iter().map(|(n, v)| (n.clone(), v.clone())))
        .collect();
    if let Some(proxy) = &input.job.proxy {
        secrets.extend(job::proxy_environment(proxy));
    }
    (env, secrets)
}

/// Copies a plugin's code: its directories and regular files, nothing else.
fn copy_tree(from: &Path, to: &Path) -> std::io::Result<()> {
    if to.exists() {
        std::fs::remove_dir_all(to)?;
    }
    private_dir(to)?;
    for entry in std::fs::read_dir(from)? {
        let entry = entry?;
        let kind = entry.file_type()?;
        let target = to.join(entry.file_name());
        if kind.is_dir() {
            copy_tree(&entry.path(), &target)?;
        } else if kind.is_file() {
            std::fs::copy(entry.path(), &target)?;
        }
    }
    Ok(())
}

fn now() -> String {
    let seconds = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| i64::try_from(d.as_secs()).unwrap_or(0));
    commands::rfc3339(seconds)
}

/// Says what Slurm reports of the job (`POST /jobs/{id}/slurm`). One the server doesn't
/// take is noted, not fatal: the job goes on.
fn report(host: &Job, fields: &serde_json::Value) {
    let mut body = fields.clone();
    body["lease_token"] = host.lease_token.clone().into();
    let path = fill(paths::JOBS_SLURM, &[&host.id]);
    let deadline = Instant::now() + Duration::from_secs(60);
    match api::retrying(deadline, || {
        host.api().post::<_, serde_json::Value>(&path, &body)
    }) {
        Ok(_) => {}
        Err(error @ (Error::Stale(_) | Error::Unauthorized(_))) => host.lost(&error),
        Err(error) => say::warn(
            "Couldn't say what Slurm reports of a job.",
            &[("job", &host.id), ("detail", &error)],
        ),
    }
}

/// Reads a file another process is writing, as it grows, until `done`.
struct Tail {
    path: PathBuf,
    file: Option<std::fs::File>,
    done: Arc<AtomicBool>,
}

impl Read for Tail {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        loop {
            if self.file.is_none() {
                self.file = std::fs::File::open(&self.path).ok();
            }
            let finished = self.done.load(Ordering::SeqCst);
            if let Some(file) = &mut self.file {
                let read = file.read(buf)?;
                if read > 0 || finished {
                    return Ok(read);
                }
            } else if finished {
                return Ok(0);
            }
            thread::sleep(Duration::from_millis(250));
        }
    }
}

/// Follows the Slurm job until it ends: renews nothing itself (the job's heartbeats do),
/// but says what Slurm reports, streams its log, and cancels it when the run is
/// cancelled or its lease goes elsewhere. The worker stopping leaves it running.
#[allow(clippy::too_many_arguments)]
fn watch(
    cluster: &Cluster,
    host: &Arc<Job>,
    plan: &Plan,
    placed: &RunInputJobSlurm,
    dirs: &Dirs,
    kept: &Kept,
    said: &Arc<Mutex<Option<String>>>,
    mask: &logs::Mask,
) -> Ran {
    let id = kept.slurm_id.clone().unwrap_or_default();
    let done = Arc::new(AtomicBool::new(false));
    let reading = {
        let tail = Tail {
            path: dirs.root.join("log"),
            file: None,
            done: Arc::clone(&done),
        };
        let (host, said, mask) = (Arc::clone(host), Arc::clone(said), mask.clone());
        thread::spawn(move || job::read_lines(host.as_ref(), tail, &said, &mask))
    };
    cluster.poller.track(&id);
    let mut last: Option<Queued> = None;
    let mut cancelled = false;
    let watched = loop {
        if cluster.interrupted.load(Ordering::SeqCst)
            && !cluster.unauthorized.load(Ordering::SeqCst)
        {
            break Watched::Detached;
        }
        if host.is_lost() || cluster.unauthorized.load(Ordering::SeqCst) {
            let _ = cluster.settings.slurm.cancel(&id);
            break Watched::Lost;
        }
        if !cancelled && host.stop_for() == Some(Stop::Cancelled) {
            say::info(
                "Cancelling a job on the cluster.",
                &[("job", &kept.job), ("slurm_job", &id)],
            );
            match cluster.settings.slurm.cancel(&id) {
                Ok(()) => cancelled = true,
                Err(error) => say::warn("Couldn't cancel a Slurm job.", &[("detail", &error)]),
            }
        }
        match cluster.poller.seen(&id) {
            Some(Seen::Queued(queued)) => {
                if last.as_ref() != Some(&queued) {
                    seen(host, &queued, last.as_ref());
                    last = Some(queued);
                }
            }
            Some(Seen::Gone) => break Watched::Left,
            None => {}
        }
        thread::sleep(Duration::from_millis(500));
    };
    cluster.poller.forget(&id);
    let ran = match watched {
        Watched::Detached => Ran {
            status: None,
            stopped: Some(Stop::Detached),
            report: None,
        },
        Watched::Lost => Ran {
            status: None,
            stopped: Some(Stop::Lost),
            report: None,
        },
        Watched::Left => ended_as(cluster, host, plan, placed, dirs, &id, cancelled),
    };
    done.store(true, Ordering::SeqCst);
    let _ = reading.join();
    ran
}

/// Why the worker stopped following a Slurm job.
enum Watched {
    /// It left the queue: it ended.
    Left,
    /// Its lease went elsewhere, and it was cancelled.
    Lost,
    /// The worker is stopping, and leaves it running.
    Detached,
}

/// Says what changed of the job as `squeue` lists it.
fn seen(host: &Job, queued: &Queued, last: Option<&Queued>) {
    let mut fields = serde_json::json!({});
    if commands::STATES.contains(&queued.state.as_str()) {
        fields["state"] = queued.state.clone().into();
    }
    fields["reason"] = queued.reason.clone().into();
    if !queued.nodes.is_empty() {
        fields["nodes"] = queued.nodes.clone().into();
    }
    if let Some(started) = queued
        .started
        .filter(|_| commands::phase(&queued.state) == Phase::Running)
    {
        fields["started_at"] = commands::rfc3339(started).into();
    }
    let was = last.map(|q| commands::phase(&q.state));
    if commands::phase(&queued.state) == Phase::Running && was != Some(Phase::Running) {
        host.state(HeartbeatInState::Running);
        host.line(&format!(
            "iki-worker: Slurm job {} started on {}.\n",
            queued.id,
            if queued.nodes.is_empty() {
                "its node"
            } else {
                &queued.nodes
            }
        ));
    }
    report(host, &fields);
}

/// How the job ended, from `sacct` (or, on a cluster without accounting, the exit status
/// its batch script noted), said to the server with what it used.
fn ended_as(
    cluster: &Cluster,
    host: &Job,
    plan: &Plan,
    placed: &RunInputJobSlurm,
    dirs: &Dirs,
    id: &str,
    cancelled: bool,
) -> Ran {
    // sacct may take a moment to account for a job that just left the queue.
    let mut accounted = None;
    for wait in [0, 2, 5, 10, 20] {
        thread::sleep(Duration::from_secs(wait));
        match cluster.settings.slurm.accounted(id) {
            Ok(Some(found)) if commands::phase(&found.state) == Phase::Ended => {
                accounted = Some(found);
                break;
            }
            Ok(_) => {}
            Err(error) => say::warn("Couldn't ask sacct about a job.", &[("detail", &error)]),
        }
    }
    let noted = std::fs::read_to_string(dirs.root.join(EXIT))
        .ok()
        .and_then(|t| t.trim().parse::<i32>().ok());
    let Some(found) = accounted else {
        return match noted {
            Some(code) => Ran {
                status: Some(status(code)),
                stopped: None,
                report: None,
            },
            None => interrupted(format!(
                "Slurm job {id} left the queue without a word of how it ended: the cluster \
                 may have lost it."
            )),
        };
    };
    report(host, &accounting(&found));
    let code = found.exit().or(noted).unwrap_or(1);
    let timeout = plan.timeout.as_secs();
    match found.state.as_str() {
        "COMPLETED" | "FAILED" => Ran {
            status: Some(status(code)),
            stopped: None,
            report: None,
        },
        "OUT_OF_MEMORY" => Ran {
            status: Some(status(code)),
            stopped: None,
            report: Some(Report::Fail(
                FailInReason::Failed,
                format!(
                    "Slurm stopped it: it used more than the {} of memory it asked for. Its \
                     environment can ask for more.",
                    gibibytes(placed.memory.unwrap_or_default())
                ),
            )),
        },
        "TIMEOUT" | "DEADLINE" => Ran {
            status: None,
            stopped: Some(Stop::TimedOut),
            report: Some(Report::Fail(
                FailInReason::TimedOut,
                format!(
                    "Slurm stopped it at its time limit of {timeout} second{}.",
                    if timeout == 1 { "" } else { "s" }
                ),
            )),
        },
        "CANCELLED" if cancelled => Ran {
            status: None,
            stopped: Some(Stop::Cancelled),
            report: None,
        },
        "CANCELLED" => interrupted(format!(
            "Its Slurm job {id} was cancelled on the cluster{}.",
            found
                .cancelled_by
                .as_deref()
                .map(|by| format!(" by user {by}"))
                .unwrap_or_default()
        )),
        "PREEMPTED" => interrupted(format!(
            "The cluster preempted its Slurm job {id} for other work."
        )),
        "NODE_FAIL" | "BOOT_FAIL" => interrupted(format!(
            "Its node{} failed while running Slurm job {id}.",
            if found.nodes.is_empty() {
                String::new()
            } else {
                format!(" ({})", found.nodes)
            }
        )),
        other => interrupted(format!("Slurm ended its job {id} as {other}.")),
    }
}

fn interrupted(message: String) -> Ran {
    Ran {
        status: None,
        stopped: Some(Stop::Interrupted),
        report: Some(Report::Fail(FailInReason::Interrupted, message)),
    }
}

/// What `sacct` says, as the server takes it.
fn accounting(found: &Accounted) -> serde_json::Value {
    let mut fields = serde_json::json!({
        "usage": {
            "elapsed": found.elapsed,
            "cpu_seconds": found.cpu_seconds,
            "max_rss": found.max_rss,
            "alloc": {
                "cpu": found.alloc_cpu,
                "memory": found.alloc_memory,
                "gpu_count": found.alloc_gpus,
            },
        },
    });
    if commands::STATES.contains(&found.state.as_str()) {
        fields["state"] = found.state.clone().into();
    }
    if !found.nodes.is_empty() && found.nodes != "None assigned" {
        fields["nodes"] = found.nodes.clone().into();
    }
    for (key, at) in [
        ("submitted_at", found.submitted),
        ("started_at", found.started),
        ("ended_at", found.ended),
    ] {
        if let Some(at) = at {
            fields[key] = commands::rfc3339(at).into();
        }
    }
    // `code:signal`, when sacct knows it.
    if found.exit_code.contains(':') {
        fields["exit_code"] = found.exit_code.clone().into();
    }
    fields
}

fn status(code: i32) -> ExitStatus {
    #[cfg(unix)]
    {
        std::os::unix::process::ExitStatusExt::from_raw((code & 0xff) << 8)
    }
    #[cfg(not(unix))]
    {
        let _ = code;
        unreachable!("Slurm jobs run only on Unix")
    }
}

fn gibibytes(bytes: i64) -> String {
    #[allow(clippy::cast_precision_loss)]
    let gib = bytes as f64 / f64::from(1u32 << 30);
    if gib >= 1.0 {
        format!("{gib:.0} GiB")
    } else {
        format!("{} MiB", bytes >> 20)
    }
}

// --- Picking jobs up again after a restart -------------------------------------------------

/// Picks up the jobs the worker held when it stopped, from their directories: each whose
/// lease is still its own is followed again; the rest are cancelled and removed.
fn adopt(cluster: &Arc<Cluster>) -> Vec<thread::JoinHandle<()>> {
    let Ok(entries) = std::fs::read_dir(cluster.settings.work_dir.join("jobs")) else {
        return Vec::new();
    };
    let mut threads = Vec::new();
    for entry in entries.flatten() {
        let dir = entry.path();
        let read = |name: &str| std::fs::read(dir.join(name)).ok();
        // A job the worker stopped while staging has no note yet: its input says whose
        // lease it is, so its run hears it was interrupted rather than wait for the lease
        // to lapse.
        let found = read(KEPT)
            .and_then(|t| serde_json::from_slice::<Kept>(&t).ok())
            .or_else(|| {
                let input: RunInput = serde_json::from_slice(&read("input.json")?).ok()?;
                Some(Kept {
                    name: format!("iki-{}-{}", input.run.hrid, input.run.attempt),
                    job: input.job.id,
                    lease_token: input.job.lease_token,
                    slurm_id: None,
                    heartbeat_every: input.job.heartbeat_every,
                })
            });
        let Some(kept) = found else {
            // Nothing to follow, and no lease to give back: it lapses.
            if !cluster.config.keep_job_dirs {
                let _ = std::fs::remove_dir_all(&dir);
            }
            continue;
        };
        cluster.held.fetch_add(1, Ordering::SeqCst);
        let cluster = Arc::clone(cluster);
        threads.push(thread::spawn(move || {
            let ended = resume(&cluster, &kept);
            cluster.ended(ended);
        }));
    }
    threads
}

fn resume(cluster: &Arc<Cluster>, kept: &Kept) -> Ended {
    let dirs = cluster.dirs(&kept.job);
    let slurm = &cluster.settings.slurm;
    let found = kept
        .slurm_id
        .clone()
        .or_else(|| slurm.named(&kept.name).ok().flatten());
    // Its heartbeats say what it's doing from the first: waiting, or (running or ended)
    // on its nodes.
    let waiting = found
        .as_ref()
        .and_then(|id| slurm.queued(std::slice::from_ref(id)).ok()?.remove(id))
        .is_some_and(|queued| commands::phase(&queued.state) == Phase::Pending);
    let held = Held::start(
        &cluster.api,
        &kept.job,
        &kept.lease_token,
        if waiting {
            HeartbeatInState::Pending
        } else {
            HeartbeatInState::Running
        },
        kept.heartbeat_every,
        false,
    );
    if let Err(error) = held.job.renew() {
        // Its lease lapsed while the worker was away, and the run went on without it.
        say::info(
            "A job's lease lapsed while the worker was stopped; cancelling it on the cluster.",
            &[("job", &kept.job), ("detail", &error)],
        );
        if let Some(id) = &found {
            let _ = slurm.cancel(id);
        }
        let ended = held.detach();
        cluster.clean(&dirs);
        return ended;
    }
    let raw = std::fs::read(&dirs.input)
        .ok()
        .and_then(|t| serde_json::from_slice::<serde_json::Value>(&t).ok());
    let input = raw
        .as_ref()
        .and_then(|raw| serde_json::from_value::<RunInput>(raw.clone()).ok());
    let (Some(input), Some(id)) = (input, found) else {
        let ended = held.end(Some(Report::Fail(
            FailInReason::Interrupted,
            "iki-worker restarted before it could submit the job to the cluster.".into(),
        )));
        cluster.clean(&dirs);
        return ended;
    };
    let Some(placed) = input.job.slurm.clone() else {
        return held.end(None);
    };
    say::info(
        "Picked up a job on the cluster again.",
        &[("job", &kept.job), ("slurm_job", &id)],
    );
    let kept = Kept {
        slurm_id: Some(id),
        ..kept.clone()
    };
    let _ = keep(&dirs, &kept);
    let plugin_dir = dirs.root.join("plugin");
    let plan = Plan {
        input: &input,
        plugin_dir: &plugin_dir,
        built: None,
        timeout: Duration::from_secs(u64::try_from(input.job.timeout.max(1)).unwrap_or(3600)),
    };
    let host = Arc::clone(&held.job);
    let report = job::conclude(&host, &plan, &dirs, |host, said, mask| {
        Ok(watch(
            cluster, host, &plan, &placed, &dirs, &kept, said, mask,
        ))
    });
    see_through(cluster, held, &dirs, report)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settings(given: &[(&str, &str)]) -> Result<Option<Settings>, String> {
        let given: BTreeMap<String, String> = given
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect();
        Settings::from_env(&|name| given.get(name).cloned())
    }

    #[test]
    fn a_worker_submits_to_slurm_once_it_has_a_shared_work_directory() {
        assert_eq!(settings(&[]), Ok(None));
        let bin = std::env::temp_dir().join(format!("iki-slurm-bin-{}", std::process::id()));
        std::fs::create_dir_all(&bin).unwrap();
        std::fs::write(bin.join("apptainer"), "").unwrap();
        let path = bin.to_string_lossy().into_owned();
        let found = settings(&[("IKI_SLURM_WORK_DIR", "/scratch/iki"), ("PATH", &path)])
            .unwrap()
            .unwrap();
        assert_eq!(found.work_dir, PathBuf::from("/scratch/iki"));
        assert_eq!(found.image_cache, PathBuf::from("/scratch/iki/images"));
        assert_eq!(
            found.tools.apptainer,
            bin.join("apptainer").to_string_lossy()
        );
        assert_eq!(
            (found.max_jobs, found.poll_every),
            (100, Duration::from_secs(15))
        );
        let relative = settings(&[("IKI_SLURM_WORK_DIR", "scratch"), ("PATH", &path)]);
        assert!(relative.unwrap_err().contains("absolute"));
        let none = settings(&[("IKI_SLURM_WORK_DIR", "/scratch/iki"), ("PATH", "/nowhere")]);
        assert!(none.unwrap_err().contains("Couldn't find apptainer"));
        let zero = settings(&[
            ("IKI_SLURM_WORK_DIR", "/scratch/iki"),
            ("PATH", &path),
            ("IKI_SLURM_MAX_JOBS", "0"),
        ]);
        assert!(zero.unwrap_err().contains("IKI_SLURM_MAX_JOBS"));
    }

    #[test]
    fn what_sacct_says_goes_to_the_server_as_it_takes_it() {
        let found = Accounted {
            state: "COMPLETED".into(),
            cancelled_by: None,
            exit_code: "0:0".into(),
            elapsed: Some(42),
            cpu_seconds: Some(40.5),
            max_rss: Some(1 << 20),
            alloc_cpu: Some(2.0),
            alloc_memory: Some(1 << 30),
            alloc_gpus: None,
            nodes: "c1".into(),
            submitted: Some(1_700_000_000),
            started: Some(1_700_000_010),
            ended: Some(1_700_000_052),
        };
        let fields = accounting(&found);
        assert_eq!(fields["state"], "COMPLETED");
        assert_eq!(fields["exit_code"], "0:0");
        assert_eq!(fields["usage"]["elapsed"], 42);
        assert_eq!(fields["usage"]["alloc"]["cpu"], 2.0);
        assert_eq!(fields["nodes"], "c1");
        assert_eq!(fields["started_at"], "2023-11-14T22:13:30Z");
        // An exit code sacct didn't give isn't sent.
        let unknown = accounting(&Accounted {
            exit_code: String::new(),
            ..found
        });
        assert!(unknown.get("exit_code").is_none());
    }
}
