//! Between one job and the next, on a worker that runs many (ADR 0062).
//!
//! Runs from different projects may follow one another on the same worker, so what one
//! leaves must not reach the next:
//!
//! - The worker's memory, where its token is, is closed to other processes, which run as
//!   the same user ([`guard_memory`]).
//! - In a container whose first process is the worker, every other process is a job's:
//!   after each job, any still running are stopped and reaped ([`end_strays`]).
//! - `IKI_WORKER_CLEAN_DIR`, such as a pod's scratch volume, is emptied ([`empty`]).

use std::path::Path;
use std::time::{Duration, Instant};

use crate::say;

/// How long processes a job left may take to die before the worker gives up on this
/// machine.
const STRAYS_WITHIN: Duration = Duration::from_secs(10);

/// Keeps other processes running as this user, such as a job's command, from reading
/// the worker's memory and environment through `/proc` or a debugger. Its token is in
/// there. On Linux only; elsewhere the worker is on someone's own computer.
pub fn guard_memory() {
    #[cfg(target_os = "linux")]
    {
        use rustix::process::{DumpableBehavior, set_dumpable_behavior};
        if let Err(error) = set_dumpable_behavior(DumpableBehavior::NotDumpable) {
            say::warn(
                "Couldn't keep other processes from reading this worker's memory.",
                &[("detail", &error)],
            );
        }
    }
}

/// Whether the worker is the first process of its container, so that every other process
/// it can see is one of its jobs'.
pub fn owns_processes() -> bool {
    cfg!(target_os = "linux") && rustix::process::getpid().is_init()
}

/// Stops every process a job left behind, if the worker owns this container's
/// processes, and reaps them. Errors when some won't die: the machine isn't clean.
pub fn end_strays() -> Result<(), String> {
    if !owns_processes() {
        return Ok(());
    }
    let deadline = Instant::now() + STRAYS_WITHIN;
    let mut ended = 0;
    loop {
        reap();
        let others = others();
        if others.is_empty() {
            if ended > 0 {
                say::info(
                    "Stopped what the last job left running.",
                    &[("processes", &ended)],
                );
            }
            return Ok(());
        }
        if Instant::now() > deadline {
            return Err(format!(
                "{} processes the last job started wouldn't stop.",
                others.len()
            ));
        }
        // Stop them all, then kill them, so none starts another in between.
        for pid in &others {
            let _ = rustix::process::kill_process(*pid, rustix::process::Signal::STOP);
        }
        for pid in &others {
            let _ = rustix::process::kill_process(*pid, rustix::process::Signal::KILL);
        }
        ended = ended.max(others.len());
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// Every process but the worker: `/proc` lists one entry per process (not thread).
fn others() -> Vec<rustix::process::Pid> {
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return Vec::new();
    };
    let me = rustix::process::getpid();
    entries
        .filter_map(Result::ok)
        .filter_map(|e| e.file_name().to_str()?.parse::<i32>().ok())
        .filter_map(rustix::process::Pid::from_raw)
        .filter(|pid| *pid != me)
        .collect()
}

/// Collects every child that has ended: as the container's first process, the worker
/// inherits what its jobs left.
fn reap() {
    while let Ok(Some(_)) = rustix::process::waitpid(None, rustix::process::WaitOptions::NOHANG) {}
}

/// Removes everything in `dir`, but not `dir`: whatever a job left there, however it
/// set its permissions. Symlinks are removed, never followed.
pub fn empty(dir: &Path) -> Result<(), String> {
    let entries = match std::fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(format!("Couldn't empty {}: {error}", dir.display())),
    };
    for entry in entries {
        let path = entry
            .map_err(|error| format!("Couldn't empty {}: {error}", dir.display()))?
            .path();
        remove(&path).map_err(|error| format!("Couldn't remove {}: {error}", path.display()))?;
    }
    Ok(())
}

/// Removes `root` and everything under it, without recursing (a job may have nested
/// directories deeper than a stack).
fn remove(root: &Path) -> std::io::Result<()> {
    let mut stack = vec![(root.to_path_buf(), false)];
    while let Some((path, listed)) = stack.pop() {
        let meta = match std::fs::symlink_metadata(&path) {
            Ok(meta) => meta,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        if !meta.is_dir() {
            std::fs::remove_file(&path)?;
            continue;
        }
        if listed {
            std::fs::remove_dir(&path)?;
            continue;
        }
        // A job may have taken away its own permission to list or change a directory.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if meta.permissions().mode() & 0o700 != 0o700 {
                std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700))?;
            }
        }
        stack.push((path.clone(), true));
        for entry in std::fs::read_dir(&path)? {
            stack.push((entry?.path(), false));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("iki-between-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn empties_a_directory_whatever_a_job_left_in_it() {
        let dir = scratch("empty");
        let outside = scratch("outside");
        std::fs::write(outside.join("keep"), b"not the job's").unwrap();
        let deep = (0..200).fold(dir.join("nested"), |path, i| path.join(i.to_string()));
        std::fs::create_dir_all(&deep).unwrap();
        std::fs::write(deep.join("data.csv"), b"patient,result").unwrap();
        std::fs::write(dir.join(".hidden"), b"x").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::os::unix::fs::symlink(&outside, dir.join("link")).unwrap();
            let locked = dir.join("locked");
            std::fs::create_dir(&locked).unwrap();
            std::fs::write(locked.join("secret"), b"x").unwrap();
            std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o000)).unwrap();
        }
        empty(&dir).unwrap();
        assert!(dir.is_dir());
        assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 0);
        // A link out of the directory is removed, not followed.
        assert!(outside.join("keep").is_file());
        std::fs::remove_dir_all(&dir).unwrap();
        std::fs::remove_dir_all(&outside).unwrap();
        empty(&dir).unwrap();
    }

    #[test]
    fn a_worker_on_someones_computer_leaves_other_processes_alone() {
        // The test runner isn't its container's first process.
        assert!(!owns_processes());
        assert!(end_strays().is_ok());
    }
}
