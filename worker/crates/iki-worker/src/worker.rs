//! A worker's life: register (or pick up its saved token), lease jobs one at a time and run
//! them, and deregister when it stops for good (ADR 0043).

use std::io::Write;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;

use iki_protocol::{PluginCode, RegisterIn, RegisteredOut, WorkerIn, WorkerOut, paths};
use serde::{Deserialize, Serialize};

use crate::api::{self, Api, Error};
use crate::config::Config;
use crate::job::{self, Ended};
use crate::plugins;
use crate::say;

/// Seconds a lease request waits for work. Short enough that a stopping worker isn't
/// held for long (give pods a termination grace period of at least a minute).
const LEASE_WAIT: u32 = 20;

/// What `IKI_WORKER_STATE` holds: the worker's own token, for the server it's for.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct Saved {
    url: String,
    id: String,
    token: String,
}

fn load(path: &Path, url: &str) -> Option<Saved> {
    let text = std::fs::read(path).ok()?;
    serde_json::from_slice::<Saved>(&text)
        .ok()
        .filter(|saved| saved.url == url)
}

/// Writes the token where only this user can read it, replacing the file in one step.
fn save(path: &Path, saved: &Saved) -> std::io::Result<()> {
    if let Some(dir) = path.parent().filter(|d| !d.as_os_str().is_empty()) {
        std::fs::create_dir_all(dir)?;
    }
    let partial = path.with_extension("partial");
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
    let mut file = options.open(&partial)?;
    file.write_all(&serde_json::to_vec(saved).map_err(std::io::Error::other)?)?;
    file.sync_all()?;
    std::fs::rename(partial, path)
}

/// The worker's API client, with its own token: the saved one if it still works, or a
/// new one from registering.
/// Signs in with the saved token, saying what the worker runs now, or registers.
fn sign_in(config: &Config, plugins: &[PluginCode]) -> Result<(Api, bool), String> {
    let anonymous = Api::new(&config.url, "");
    if let Some(path) = &config.state_file
        && let Some(saved) = load(path, &config.url)
    {
        let api = anonymous.with_token(&saved.token);
        let body = WorkerIn {
            capacity: config.capacity.clone(),
            plugins: plugins.to_vec(),
            version: env!("CARGO_PKG_VERSION").to_owned(),
        };
        match api::retrying(std::time::Instant::now() + Duration::from_secs(120), || {
            api.put::<_, WorkerOut>(paths::ME, &body)
        }) {
            Ok(_) => {
                say::info("Signed in with the saved token.", &[("worker", &saved.id)]);
                return Ok((api, false));
            }
            Err(Error::Unauthorized(_)) => {
                say::warn(
                    "The saved token was revoked; registering again.",
                    &[("worker", &saved.id)],
                );
            }
            Err(error) => return Err(format!("Couldn't reach {}: {error}", config.url)),
        }
    }
    let registration = config
        .registration_token
        .as_deref()
        .ok_or("The saved token no longer works. Set IKI_REGISTRATION_TOKEN to register again.")?;
    let body = RegisterIn {
        capacity: config.capacity.clone(),
        name: config.name.clone(),
        plugins: plugins.to_vec(),
        version: env!("CARGO_PKG_VERSION").to_owned(),
    };
    let registering = anonymous.with_token(registration);
    let registered = api::retrying(std::time::Instant::now() + Duration::from_secs(120), || {
        registering.post::<_, RegisteredOut>(paths::REGISTER, &body)
    });
    let registered = match registered {
        Ok(Some(registered)) => registered,
        Ok(None) => return Err("The server's answer to registering was empty.".to_owned()),
        Err(Error::Unauthorized(detail)) => {
            return Err(format!(
                "Couldn't register: {detail} Ask a Compute admin for a new registration token."
            ));
        }
        Err(error) => return Err(format!("Couldn't register with {}: {error}", config.url)),
    };
    say::info(
        "Registered.",
        &[
            ("worker", &registered.id),
            ("tier", &registered.tier),
            ("name", &config.name),
        ],
    );
    let api = anonymous.with_token(&registered.token);
    if let Some(path) = &config.state_file {
        let saved = Saved {
            url: config.url.clone(),
            id: registered.id,
            token: registered.token,
        };
        save(path, &saved)
            .map_err(|error| format!("Couldn't save the token to {}: {error}", path.display()))?;
        return Ok((api, false));
    }
    // Nowhere to keep the token: it lives as long as this process.
    Ok((api, true))
}

/// Sleeps for `wait`, waking early if the worker is stopping.
fn pause(wait: Duration, interrupted: &AtomicBool) {
    let end = std::time::Instant::now() + wait;
    while std::time::Instant::now() < end && !interrupted.load(Ordering::SeqCst) {
        thread::sleep(Duration::from_millis(100));
    }
}

/// Runs until SIGTERM or SIGINT, or after one job with `once`. Stopping stops the job's
/// command too (SIGTERM, then SIGKILL after 10 seconds) and reports it `interrupted`, so
/// it can be retried elsewhere.
pub fn start(config: &Config, once: bool) -> Result<(), String> {
    let interrupted = Arc::new(AtomicBool::new(false));
    for signal in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        signal_hook::flag::register(signal, Arc::clone(&interrupted))
            .map_err(|error| format!("Couldn't watch for signals: {error}"))?;
    }
    let have = plugins::discover(&config.plugins_dir)?;
    if have.code.is_empty() {
        say::warn(
            "IKI_PLUGINS_DIR holds no plugins, so this worker can run nothing.",
            &[("path", &config.plugins_dir.display())],
        );
    }
    let (api, ephemeral) = sign_in(config, &have.code)?;
    let lease = format!("{}?wait={LEASE_WAIT}", paths::LEASE);
    let mut failures = 0;
    let mut outcome = Ok(());
    say::info("Waiting for jobs.", &[("server", &config.url)]);
    while !interrupted.load(Ordering::SeqCst) {
        match api.post::<_, serde_json::Value>(&lease, &serde_json::json!({})) {
            Ok(Some(raw)) => {
                failures = 0;
                if job::run(&api, config, &have, &interrupted, &raw) == Ended::Unauthorized {
                    outcome = Err(revoked());
                    break;
                }
                if once {
                    break;
                }
            }
            Ok(None) => failures = 0,
            Err(Error::Unauthorized(_)) => {
                outcome = Err(revoked());
                break;
            }
            Err(error) => {
                failures += 1;
                let wait = api::backoff(failures);
                say::warn(
                    "Couldn't lease; trying again.",
                    &[("detail", &error), ("in_seconds", &wait.as_secs())],
                );
                pause(wait, &interrupted);
            }
        }
    }
    if ephemeral && outcome.is_ok() {
        match api.post::<_, serde_json::Value>(paths::DEREGISTER, &serde_json::json!({})) {
            Ok(_) => say::info("Deregistered.", &[]),
            Err(error) => say::warn("Couldn't deregister.", &[("detail", &error)]),
        }
    }
    say::info("Stopped.", &[]);
    outcome
}

fn revoked() -> String {
    "The server no longer accepts this worker's token: a Compute admin revoked it. \
     Register again with a registration token."
        .to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn saved_tokens_are_private_and_for_one_server() {
        let dir = std::env::temp_dir().join(format!("iki-worker-state-{}", std::process::id()));
        let path = dir.join("state").join("worker.json");
        let saved = Saved {
            url: "https://a.example".into(),
            id: "w".into(),
            token: "iki_wkr_x".into(),
        };
        save(&path, &saved).unwrap();
        assert_eq!(load(&path, "https://a.example"), Some(saved));
        assert_eq!(load(&path, "https://b.example"), None);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&path).unwrap().permissions().mode();
            assert_eq!(mode & 0o777, 0o600);
        }
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
