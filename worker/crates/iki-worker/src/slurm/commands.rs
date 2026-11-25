//! Slurm's commands, as a cluster's worker runs them on its login node: `sbatch` to
//! submit a job, `squeue` while it waits and runs, `sacct` once it's left the queue, and
//! `scancel` to stop it. Each is run with Slurm's times as seconds since the epoch, and
//! read in its parsable form.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use iki_protocol::RunInputJobSlurm;

/// The longest a Slurm command may take: slurmctld can be slow when it's busy, but a
/// command that hangs mustn't hold up the worker.
const TIMEOUT: Duration = Duration::from_secs(120);

/// Where the worker finds Slurm's commands: on `PATH`, or in `IKI_SLURM_BIN`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Slurm {
    pub bin: Option<PathBuf>,
}

/// A job as `squeue` lists it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Queued {
    pub id: String,
    pub state: String,
    pub reason: String,
    pub nodes: String,
    /// When it started on its nodes, in seconds since the epoch.
    pub started: Option<i64>,
}

/// A job as `sacct` accounts for it, once it's ended.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Accounted {
    pub state: String,
    /// Who cancelled it, if it was: Slurm's `CANCELLED by <uid>`.
    pub cancelled_by: Option<String>,
    /// `code:signal`.
    pub exit_code: String,
    pub elapsed: Option<i64>,
    pub cpu_seconds: Option<f64>,
    pub max_rss: Option<i64>,
    pub alloc_cpu: Option<f64>,
    pub alloc_memory: Option<i64>,
    pub alloc_gpus: Option<i64>,
    pub nodes: String,
    pub submitted: Option<i64>,
    pub started: Option<i64>,
    pub ended: Option<i64>,
}

impl Accounted {
    /// The command's exit status: its code, or 128 plus the signal that ended it.
    pub fn exit(&self) -> Option<i32> {
        let (code, signal) = self.exit_code.split_once(':')?;
        let (code, signal) = (code.parse::<i32>().ok()?, signal.parse::<i32>().ok()?);
        Some(if signal > 0 { 128 + signal } else { code })
    }
}

impl Slurm {
    fn command(&self, name: &str) -> Command {
        let program = self
            .bin
            .as_ref()
            .map_or_else(|| PathBuf::from(name), |bin| bin.join(name));
        let mut command = Command::new(program);
        // Times as seconds since the epoch, whatever the cluster's locale.
        command
            .env("SLURM_TIME_FORMAT", "%s")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        command
    }

    /// Runs `name` with `args`, and returns what it printed; errs with what it said if it
    /// failed.
    fn run(&self, name: &str, args: &[String]) -> Result<String, String> {
        let mut child = self
            .command(name)
            .args(args)
            .spawn()
            .map_err(|e| format!("Couldn't run {name}: {e}."))?;
        let deadline = Instant::now() + TIMEOUT;
        loop {
            match child.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(50));
                }
                Ok(None) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(format!("{name} took longer than {}s.", TIMEOUT.as_secs()));
                }
                Err(e) => return Err(format!("Couldn't wait for {name}: {e}.")),
            }
        }
        let output = child
            .wait_with_output()
            .map_err(|e| format!("Couldn't read what {name} printed: {e}."))?;
        let text = String::from_utf8_lossy(&output.stdout).into_owned();
        if output.status.success() {
            Ok(text)
        } else {
            let said = String::from_utf8_lossy(&output.stderr);
            let said = said.trim();
            Err(if said.is_empty() {
                format!("{name} failed ({}).", output.status)
            } else {
                format!("{name} failed: {said}")
            })
        }
    }

    /// Submits `script` with `args` and returns the job's ID.
    pub fn submit(&self, args: &[String], script: &Path) -> Result<String, String> {
        let mut all = args.to_vec();
        all.push(script.to_string_lossy().into_owned());
        let printed = self.run("sbatch", &all)?;
        parse_submitted(&printed)
            .ok_or_else(|| format!("sbatch printed no job ID: {:?}", printed.trim()))
    }

    /// The jobs `squeue` lists of `ids`, by ID: those that have left the queue aren't.
    pub fn queued(&self, ids: &[String]) -> Result<BTreeMap<String, Queued>, String> {
        if ids.is_empty() {
            return Ok(BTreeMap::new());
        }
        let args = [
            "--noheader".to_owned(),
            format!("--jobs={}", ids.join(",")),
            "--format=%i|%T|%r|%N|%S".to_owned(),
        ];
        match self.run("squeue", &args) {
            Ok(text) => Ok(parse_queued(&text)),
            // squeue errs on a job ID it no longer knows, when that's the only one asked.
            Err(error) if error.contains("Invalid job id") => Ok(BTreeMap::new()),
            Err(error) => Err(error),
        }
    }

    /// The job named `name` that `squeue` lists, if any: one this worker submitted before
    /// it could note its ID.
    pub fn named(&self, name: &str) -> Result<Option<String>, String> {
        let args = [
            "--noheader".to_owned(),
            format!("--name={name}"),
            "--format=%i".to_owned(),
        ];
        let text = self.run("squeue", &args)?;
        Ok(text
            .lines()
            .map(str::trim)
            .find(|l| !l.is_empty())
            .map(str::to_owned))
    }

    /// What `sacct` says of the job; None until it has accounted for it.
    pub fn accounted(&self, id: &str) -> Result<Option<Accounted>, String> {
        let args = [
            "--noheader".to_owned(),
            "--parsable2".to_owned(),
            format!("--jobs={id}"),
            format!("--format={SACCT_FIELDS}"),
        ];
        Ok(parse_accounted(id, &self.run("sacct", &args)?))
    }

    /// Cancels the job: Slurm signals it, then kills it after its `KillWait`.
    pub fn cancel(&self, id: &str) -> Result<(), String> {
        self.run("scancel", &[id.to_owned()]).map(|_| ())
    }
}

/// What `sacct` is asked for, in this order.
const SACCT_FIELDS: &str =
    "JobID,State,ExitCode,ElapsedRaw,TotalCPU,MaxRSS,AllocTRES,NodeList,Submit,Start,End";

/// `sbatch --parsable` prints `id` or `id;cluster`.
fn parse_submitted(printed: &str) -> Option<String> {
    let line = printed.lines().map(str::trim).find(|l| !l.is_empty())?;
    let id = line.split(';').next()?.trim();
    (!id.is_empty() && id.bytes().all(|b| b.is_ascii_digit() || b == b'_')).then(|| id.to_owned())
}

fn parse_queued(text: &str) -> BTreeMap<String, Queued> {
    text.lines()
        .filter_map(|line| {
            let mut parts = line.trim().splitn(5, '|');
            let id = parts.next()?.trim().to_owned();
            if id.is_empty() {
                return None;
            }
            let state = parts.next()?.trim().to_owned();
            let reason = parts.next().unwrap_or("").trim();
            let nodes = parts.next().unwrap_or("").trim().to_owned();
            let started = parts.next().and_then(epoch);
            let queued = Queued {
                id: id.clone(),
                state,
                reason: if reason == "None" {
                    String::new()
                } else {
                    reason.to_owned()
                },
                nodes,
                started,
            };
            Some((id, queued))
        })
        .collect()
}

/// A time Slurm printed as seconds since the epoch; `Unknown`, `None` or 0 is none.
fn epoch(raw: &str) -> Option<i64> {
    raw.trim().parse::<i64>().ok().filter(|t| *t > 0)
}

/// `sacct`'s rows for `id`: its own, with its state and allocation, and its steps', the
/// batch step's with how much memory it used.
fn parse_accounted(id: &str, text: &str) -> Option<Accounted> {
    let mut found: Option<Accounted> = None;
    let mut max_rss: Option<i64> = None;
    let mut cpu_seconds: Option<f64> = None;
    for line in text.lines() {
        let fields: Vec<&str> = line.trim().split('|').collect();
        if fields.len() < 11 {
            continue;
        }
        let rss = bytes(fields[5]);
        if rss.is_some() {
            max_rss = max_rss.max(rss);
        }
        if fields[0] == id {
            let (state, cancelled_by) = match fields[1].split_once(" by ") {
                Some((state, by)) => (state.trim().to_owned(), Some(by.trim().to_owned())),
                None => (fields[1].trim().to_owned(), None),
            };
            let tres = tres(fields[6]);
            cpu_seconds = cpu_time(fields[4]);
            found = Some(Accounted {
                state,
                cancelled_by,
                exit_code: fields[2].trim().to_owned(),
                elapsed: fields[3].trim().parse().ok(),
                cpu_seconds: None,
                max_rss: None,
                alloc_cpu: tres.get("cpu").and_then(|v| v.parse().ok()),
                alloc_memory: tres.get("mem").and_then(|v| bytes(v)),
                alloc_gpus: tres
                    .iter()
                    .find(|(k, _)| **k == "gres/gpu")
                    .and_then(|(_, v)| v.parse().ok()),
                nodes: fields[7].trim().to_owned(),
                submitted: epoch(fields[8]),
                started: epoch(fields[9]),
                ended: epoch(fields[10]),
            });
        }
    }
    found.map(|mut job| {
        job.max_rss = max_rss;
        job.cpu_seconds = cpu_seconds;
        job
    })
}

/// `AllocTRES`: `cpu=2,mem=8G,node=1,billing=2,gres/gpu=1`.
fn tres(raw: &str) -> BTreeMap<&str, &str> {
    raw.split(',')
        .filter_map(|pair| pair.split_once('='))
        .map(|(k, v)| (k.trim(), v.trim()))
        .collect()
}

/// Memory as Slurm prints it: `123456K`, `8G`, or bytes. Fractions (`1.50G`) too.
fn bytes(raw: &str) -> Option<i64> {
    let raw = raw.trim();
    if raw.is_empty() {
        return None;
    }
    let (number, unit) = match raw.char_indices().find(|(_, c)| c.is_ascii_alphabetic()) {
        Some((at, _)) => (&raw[..at], &raw[at..]),
        None => (raw, ""),
    };
    let scale: f64 = match unit.to_ascii_uppercase().as_str() {
        "" => 1.0,
        "K" => 1024.0,
        "M" => 1024.0 * 1024.0,
        "G" => 1024.0 * 1024.0 * 1024.0,
        "T" => 1024.0 * 1024.0 * 1024.0 * 1024.0,
        _ => return None,
    };
    let value = number.parse::<f64>().ok()? * scale;
    #[allow(clippy::cast_possible_truncation)]
    Some(value.round() as i64)
}

/// `TotalCPU`: `[D-]HH:MM:SS[.mmm]` or `MM:SS.mmm`.
fn cpu_time(raw: &str) -> Option<f64> {
    let raw = raw.trim();
    if raw.is_empty() {
        return None;
    }
    let (days, rest) = match raw.split_once('-') {
        Some((d, rest)) => (d.parse::<f64>().ok()?, rest),
        None => (0.0, raw),
    };
    let parts: Vec<f64> = rest
        .split(':')
        .map(str::parse::<f64>)
        .collect::<Result<_, _>>()
        .ok()?;
    let seconds = match parts.as_slice() {
        [h, m, s] => h * 3600.0 + m * 60.0 + s,
        [m, s] => m * 60.0 + s,
        [s] => *s,
        _ => return None,
    };
    Some(days * 86400.0 + seconds)
}

/// What `sbatch` is given for a job the server placed: sized and placed as `slurm` says,
/// never requeued (an attempt that ends is the server's to retry), with only the
/// environment the batch script sets, and its output in the job's directory.
pub fn sbatch_args(slurm: &RunInputJobSlurm, name: &str, comment: &str, dir: &Path) -> Vec<String> {
    let path = |p: &Path| p.to_string_lossy().into_owned();
    let mut args = vec![
        "--parsable".to_owned(),
        "--export=NONE".to_owned(),
        "--no-requeue".to_owned(),
        "--nodes=1".to_owned(),
        "--ntasks=1".to_owned(),
        format!("--job-name={name}"),
        format!("--comment={comment}"),
        format!("--partition={}", slurm.partition),
        format!("--account={}", slurm.account),
        format!("--cpus-per-task={}", slurm.cpus.max(1)),
        format!("--time={}", walltime(slurm.walltime)),
        format!("--output={}", path(&dir.join("log"))),
        format!("--error={}", path(&dir.join("log"))),
        "--open-mode=append".to_owned(),
        format!("--chdir={}", path(&dir.join("command"))),
    ];
    if let Some(qos) = slurm.qos.as_deref().filter(|q| !q.is_empty()) {
        args.push(format!("--qos={qos}"));
    }
    if let Some(memory) = slurm.memory.filter(|m| *m > 0) {
        // In MiB, rounded up, as Slurm takes it without a unit.
        args.push(format!("--mem={}", (memory + (1 << 20) - 1) >> 20));
    }
    if let Some(gpus) = slurm.gpus.filter(|g| *g > 0) {
        args.push(match slurm.gpu_type.as_deref().filter(|t| !t.is_empty()) {
            Some(kind) => format!("--gpus={kind}:{gpus}"),
            None => format!("--gpus={gpus}"),
        });
    }
    if let Some(disk) = slurm.scratch_disk.filter(|d| *d > 0) {
        args.push(format!("--tmp={}", (disk + (1 << 20) - 1) >> 20));
    }
    if let Some(constraint) = slurm.constraint.as_deref().filter(|c| !c.is_empty()) {
        args.push(format!("--constraint={constraint}"));
    }
    args
}

/// Seconds as Slurm's `--time` takes them: `D-HH:MM:SS`, rounded up to the minute Slurm
/// enforces it by.
fn walltime(seconds: i64) -> String {
    let minutes = (seconds.max(1) + 59) / 60;
    let (days, hours, minutes) = (minutes / 1440, (minutes / 60) % 24, minutes % 60);
    format!("{days}-{hours:02}:{minutes:02}:00")
}

/// What a Slurm state means for the attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    /// Waiting in the cluster's queue: `PENDING`, `CONFIGURING`, a requeue or a hold.
    Pending,
    /// On its nodes, or being stopped there.
    Running,
    /// It's left: what became of it is `sacct`'s to say.
    Ended,
}

pub fn phase(state: &str) -> Phase {
    match state {
        "PENDING" | "CONFIGURING" | "REQUEUED" | "REQUEUE_HOLD" | "REQUEUE_FED"
        | "RESV_DEL_HOLD" => Phase::Pending,
        "RUNNING" | "COMPLETING" | "SUSPENDED" | "STOPPED" | "STAGE_OUT" | "SIGNALING"
        | "RESIZING" => Phase::Running,
        _ => Phase::Ended,
    }
}

/// Slurm's states as the server takes them (`squeue`'s and `sacct`'s base states).
pub const STATES: &[&str] = &[
    "PENDING",
    "CONFIGURING",
    "REQUEUED",
    "REQUEUE_HOLD",
    "REQUEUE_FED",
    "RESV_DEL_HOLD",
    "SUSPENDED",
    "STOPPED",
    "RUNNING",
    "COMPLETING",
    "STAGE_OUT",
    "SIGNALING",
    "RESIZING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "DEADLINE",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "BOOT_FAIL",
    "PREEMPTED",
    "SPECIAL_EXIT",
    "REVOKED",
];

/// Seconds since the epoch as RFC 3339, in UTC.
pub fn rfc3339(epoch: i64) -> String {
    let days = epoch.div_euclid(86400);
    let seconds = epoch.rem_euclid(86400);
    // Howard Hinnant's civil_from_days.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        seconds / 3600,
        (seconds / 60) % 60,
        seconds % 60
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn placed() -> RunInputJobSlurm {
        RunInputJobSlurm {
            cluster: "hpc1".into(),
            template: "hpc-gpu".into(),
            partition: "gpu".into(),
            account: "rd-shared".into(),
            qos: Some("normal".into()),
            constraint: Some("a100&ib".into()),
            gpu_type: Some("a100".into()),
            cpus: 2,
            memory: Some(8 << 30),
            gpus: Some(1),
            scratch_disk: Some(100 << 30),
            walltime: 3601,
            image: format!("ghcr.io/acme/fold@sha256:{}", "a".repeat(64)),
            image_policy: None,
        }
    }

    #[test]
    fn a_job_asks_for_what_its_action_needs_where_its_template_places_it() {
        let args = sbatch_args(
            &placed(),
            "iki-RUN-000042-1",
            "iki:0190",
            Path::new("/scratch/iki/jobs/0190"),
        );
        assert_eq!(
            args,
            [
                "--parsable",
                "--export=NONE",
                "--no-requeue",
                "--nodes=1",
                "--ntasks=1",
                "--job-name=iki-RUN-000042-1",
                "--comment=iki:0190",
                "--partition=gpu",
                "--account=rd-shared",
                "--cpus-per-task=2",
                "--time=0-01:01:00",
                "--output=/scratch/iki/jobs/0190/log",
                "--error=/scratch/iki/jobs/0190/log",
                "--open-mode=append",
                "--chdir=/scratch/iki/jobs/0190/command",
                "--qos=normal",
                "--mem=8192",
                "--gpus=a100:1",
                "--tmp=102400",
                "--constraint=a100&ib",
            ]
        );
        let bare = RunInputJobSlurm {
            qos: None,
            constraint: None,
            gpu_type: None,
            memory: None,
            gpus: Some(0),
            scratch_disk: None,
            walltime: 2 * 86400,
            ..placed()
        };
        let args = sbatch_args(&bare, "n", "c", Path::new("/d"));
        assert!(args.contains(&"--time=2-00:00:00".to_owned()));
        assert!(
            !args
                .iter()
                .any(|a| a.starts_with("--gpus") || a.starts_with("--mem"))
        );
    }

    #[test]
    fn reads_what_slurm_prints() {
        assert_eq!(parse_submitted("4242\n"), Some("4242".into()));
        assert_eq!(parse_submitted("4242;hpc1\n"), Some("4242".into()));
        assert_eq!(parse_submitted("Submitted batch job 1\n"), None);
        let queued =
            parse_queued("4242|PENDING|Priority||0\n4243|RUNNING|None|gpu-007|1763492400\n");
        assert_eq!(queued["4242"].reason, "Priority");
        assert_eq!(queued["4242"].started, None);
        assert_eq!(queued["4243"].nodes, "gpu-007");
        assert_eq!(queued["4243"].reason, "");
        assert_eq!(queued["4243"].started, Some(1_763_492_400));
        let text = "\
4243|CANCELLED by 1000|0:15|1800|50:00.500||cpu=4,mem=8G,node=1,billing=4,gres/gpu=1|gpu-007|1763485200|1763492400|1763494200
4243.batch|CANCELLED|0:15|1800|50:00.500|6291456K|cpu=4,mem=8G,node=1|gpu-007|1763492400|1763492400|1763494200
4243.extern|COMPLETED|0:0|1800|00:00.001|1024K|cpu=4,mem=8G,node=1|gpu-007|1763492400|1763492400|1763494200
";
        let got = parse_accounted("4243", text).unwrap();
        assert_eq!(got.state, "CANCELLED");
        assert_eq!(got.cancelled_by.as_deref(), Some("1000"));
        assert_eq!(got.exit(), Some(143));
        assert_eq!(got.elapsed, Some(1800));
        assert_eq!(got.cpu_seconds, Some(3000.5));
        assert_eq!(got.max_rss, Some(6 << 30));
        assert_eq!(got.alloc_cpu, Some(4.0));
        assert_eq!(got.alloc_memory, Some(8 << 30));
        assert_eq!(got.alloc_gpus, Some(1));
        assert_eq!(got.started, Some(1_763_492_400));
        assert_eq!(parse_accounted("4244", text), None);
        assert_eq!(cpu_time("1-02:00:00"), Some(93600.0));
        assert_eq!(bytes("1.5G"), Some(3 << 29));
    }

    #[test]
    fn slurms_states_mean_waiting_running_or_ended() {
        assert_eq!(phase("PENDING"), Phase::Pending);
        assert_eq!(phase("REQUEUE_HOLD"), Phase::Pending);
        assert_eq!(phase("COMPLETING"), Phase::Running);
        assert_eq!(phase("SUSPENDED"), Phase::Running);
        for ended in [
            "COMPLETED",
            "FAILED",
            "TIMEOUT",
            "OUT_OF_MEMORY",
            "PREEMPTED",
        ] {
            assert_eq!(phase(ended), Phase::Ended);
        }
    }

    #[test]
    fn times_go_to_the_server_in_utc() {
        assert_eq!(rfc3339(0), "1970-01-01T00:00:00Z");
        assert_eq!(rfc3339(1_763_492_400), "2025-11-18T19:00:00Z");
        assert_eq!(rfc3339(951_825_600), "2000-02-29T12:00:00Z");
    }
}
