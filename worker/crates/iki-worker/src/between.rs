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
//!
//! And while any job runs, the registration token isn't in the worker's environment: a
//! worker started with `IKI_REGISTRATION_TOKEN` starts itself again without it, handing
//! the token over on stdin ([`restart`], [`handed_token`]).

use std::path::Path;
use std::time::{Duration, Instant};

use crate::say;

/// How long processes a job left may take to die before the worker gives up on this
/// machine.
const STRAYS_WITHIN: Duration = Duration::from_secs(10);

/// Keeps other processes running as this user, such as a job's command, from reading
/// the worker's memory and environment through `/proc` or a debugger. Its token is in
/// there. On Linux only: elsewhere it's a self-hosted machine its team looks after.
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

/// Says the registration token is on stdin: the worker started itself again without it in
/// its environment.
pub const TOKEN_ON_STDIN: &str = "IKI_REGISTRATION_TOKEN_ON_STDIN";

/// A token is one line of a few dozen characters; more on stdin isn't one.
const MOST_TOKEN: u64 = 4096;

/// The registration token the worker handed itself on stdin when it started again, if it
/// did ([`restart`]).
pub fn handed_token() -> Result<Option<String>, String> {
    use std::io::Read;
    if std::env::var_os(TOKEN_ON_STDIN).is_none_or(|v| v != "1") {
        return Ok(None);
    }
    let mut text = String::new();
    std::io::stdin()
        .take(MOST_TOKEN)
        .read_to_string(&mut text)
        .map_err(|e| format!("Couldn't read the registration token from stdin: {e}"))?;
    let token = text.trim();
    if token.is_empty() {
        return Err(format!("{TOKEN_ON_STDIN} is set, but stdin held no token."));
    }
    Ok(Some(token.to_owned()))
}

/// The worker, started again in this process's place with the same arguments and
/// environment, except that `IKI_REGISTRATION_TOKEN` comes on stdin ([`hand_over`]).
pub fn restart_command(exe: &Path, token: Option<&str>) -> std::io::Result<std::process::Command> {
    let mut command = std::process::Command::new(exe);
    command.args(std::env::args_os().skip(1));
    hand_over(&mut command, token)?;
    Ok(command)
}

/// Gives `command` the registration token on stdin, never in its environment. A process's
/// environment stays readable by the process itself for as long as it runs
/// (`/proc/self/environ`), whatever it changes later; a new program image starts with
/// only what it's given.
fn hand_over(command: &mut std::process::Command, token: Option<&str>) -> std::io::Result<()> {
    command.env_remove("IKI_REGISTRATION_TOKEN");
    match token {
        Some(token) => {
            let (reader, mut writer) = std::io::pipe()?;
            // Far less than a pipe holds, so this doesn't wait for a reader.
            std::io::Write::write_all(&mut writer, token.as_bytes())?;
            drop(writer);
            command.env(TOKEN_ON_STDIN, "1").stdin(reader);
        }
        None => {
            command.env_remove(TOKEN_ON_STDIN);
        }
    }
    Ok(())
}

/// Starts the worker again in this process's place, as [`restart_command`] says. Returns
/// only if it couldn't, with why.
pub fn restart(exe: &Path, token: Option<&str>) -> std::io::Error {
    use std::os::unix::process::CommandExt;
    match restart_command(exe, token) {
        Ok(mut command) => command.exec(),
        Err(error) => error,
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
    fn the_registration_token_goes_on_stdin_never_in_the_environment() {
        use std::ffi::OsStr;
        use std::io::Read;
        let mut command = std::process::Command::new("cat");
        hand_over(&mut command, Some("iki_wreg_x")).unwrap();
        let env: Vec<_> = command.get_envs().collect();
        assert!(env.contains(&(OsStr::new("IKI_REGISTRATION_TOKEN"), None)));
        assert!(env.contains(&(OsStr::new(TOKEN_ON_STDIN), Some(OsStr::new("1")))));
        // What the new program reads on stdin is the token, and nothing more.
        let mut child = command
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        let mut read = String::new();
        child
            .stdout
            .take()
            .unwrap()
            .read_to_string(&mut read)
            .unwrap();
        assert!(child.wait().unwrap().success());
        assert_eq!(read, "iki_wreg_x");

        let command = restart_command(Path::new("/bin/iki-worker"), None).unwrap();
        let env: Vec<_> = command.get_envs().collect();
        assert!(env.contains(&(OsStr::new("IKI_REGISTRATION_TOKEN"), None)));
        assert!(env.contains(&(OsStr::new(TOKEN_ON_STDIN), None)));
    }

    #[test]
    fn a_worker_on_someones_computer_leaves_other_processes_alone() {
        // The test runner isn't its container's first process.
        assert!(!owns_processes());
        assert!(end_strays().is_ok());
    }
}
