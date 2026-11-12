//! A worker's life: register (or pick up its saved token), lease jobs one at a time and run
//! them, and deregister when it stops for good (ADR 0043).

use std::io::Write;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;

use iki_protocol::{PluginCode, RegisterIn, RegisteredOut, UpdateOut, WorkerIn, WorkerOut, paths};
use serde::{Deserialize, Serialize};

use crate::api::{self, Api, Error};
use crate::config::Config;
use crate::job::{self, Ended};
use crate::say;
use crate::{between, bundles, plugins, update};

/// Seconds a lease request waits for work. Short enough that a stopping worker isn't
/// held for long (give pods a termination grace period of at least a minute).
const LEASE_WAIT: u32 = 20;

/// What `IKI_WORKER_STATE` holds: the worker's own token, for the server it's for.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Saved {
    pub url: String,
    pub id: String,
    pub token: String,
}

/// The saved token, whichever server it's for.
pub fn load_any(path: &Path) -> Option<Saved> {
    let text = std::fs::read(path).ok()?;
    serde_json::from_slice::<Saved>(&text).ok()
}

fn load(path: &Path, url: &str) -> Option<Saved> {
    load_any(path).filter(|saved| saved.url == url)
}

/// Writes the token where only this user can read it, replacing the file in one step.
pub fn save(path: &Path, saved: &Saved) -> std::io::Result<()> {
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
/// Signs in with the saved token, saying what the worker runs now, or registers. With no
/// `plugins` yet (it fetches them once signed in), it says nothing of what it runs.
fn sign_in(config: &Config, plugins: Option<&[PluginCode]>) -> Result<(Api, bool), String> {
    let anonymous = Api::new(&config.url, "");
    if let Some(path) = &config.state_file
        && let Some(saved) = load(path, &config.url)
    {
        let api = anonymous.with_token(&saved.token);
        match api::retrying(
            std::time::Instant::now() + Duration::from_secs(120),
            || match plugins {
                Some(plugins) => tell(&api, config, plugins),
                None => api.get::<WorkerOut>(paths::ME).map(|_| ()),
            },
        ) {
            Ok(()) => {
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
    let registration = config.registration_token.as_deref().ok_or_else(|| {
        format!(
            "This worker isn't signed in to {0}. Run iki-worker login {0}, or set \
             IKI_REGISTRATION_TOKEN to register with a registration token.",
            config.url
        )
    })?;
    let body = RegisterIn {
        capacity: config.capacity.clone(),
        name: config.name.clone(),
        plugins: plugins.unwrap_or_default().to_vec(),
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

/// Says what the worker runs now (`PUT /me`).
fn tell(api: &Api, config: &Config, plugins: &[PluginCode]) -> Result<(), Error> {
    let body = WorkerIn {
        capacity: config.capacity.clone(),
        plugins: plugins.to_vec(),
        version: env!("CARGO_PKG_VERSION").to_owned(),
    };
    api.put::<_, WorkerOut>(paths::ME, &body).map(|_| ())
}

/// How often an idle worker that fetches its plugins asks whether they changed: an
/// upgrade reaches it without a restart.
const RESYNC_EVERY: Duration = Duration::from_secs(600);

/// What the server installs now, fetched and built, told to the server.
fn fetch_plugins(
    api: &Api,
    config: &Config,
    listed: &[iki_protocol::AvailableOut],
) -> Result<plugins::Have, Error> {
    let have = bundles::sync(api, &config.cache_dir, listed);
    if have.code.is_empty() && !listed.is_empty() {
        say::warn("Fetched no plugin this worker can run.", &[]);
    }
    api::retrying(std::time::Instant::now() + Duration::from_secs(120), || {
        tell(api, config, &have.code)
    })?;
    Ok(have)
}

/// Fetches the server's plugins again if they changed since `listed`.
fn resync(
    api: &Api,
    config: &Config,
    listed: &mut Vec<iki_protocol::AvailableOut>,
    have: &mut plugins::Have,
) {
    match bundles::available(api) {
        Ok(now) if now != *listed => {
            say::info("The server's plugins changed; fetching them.", &[]);
            match fetch_plugins(api, config, &now) {
                Ok(fetched) => {
                    *have = fetched;
                    *listed = now;
                }
                Err(error) => say::warn(
                    "Couldn't say what this worker runs now.",
                    &[("detail", &error)],
                ),
            }
        }
        Ok(_) => {}
        Err(error) => say::warn(
            "Couldn't ask which plugins the server installs.",
            &[("detail", &error)],
        ),
    }
}

/// Updates the worker to `update` (ADR 0060), once per version, and starts it in this
/// process's place. Returns why it couldn't start the new one, if it updated but that
/// failed; otherwise the worker goes on as it is.
fn self_update(
    config: &Config,
    update: Option<&UpdateOut>,
    tried: &mut Option<String>,
) -> Option<String> {
    let update = update?;
    if tried.as_deref() == Some(update.version.as_str()) {
        return None;
    }
    *tried = Some(update.version.clone());
    if !config.auto_update {
        say::warn(
            "The server wants a newer iki-worker; IKI_WORKER_AUTO_UPDATE=0 leaves that to you.",
            &[("version", &update.version)],
        );
        return None;
    }
    say::info("Updating iki-worker.", &[("version", &update.version)]);
    let exe = match std::env::current_exe() {
        Ok(exe) => exe,
        Err(error) => {
            say::warn(
                "Couldn't update: couldn't tell where iki-worker is.",
                &[("detail", &error)],
            );
            return None;
        }
    };
    match update::install(update, &exe, config.allow_http) {
        Ok(exe) => Some(update::restart(&exe)),
        Err(error) => {
            say::warn(
                "Couldn't update iki-worker; carrying on with this one.",
                &[("version", &update.version), ("detail", &error)],
            );
            None
        }
    }
}

/// What the server says of this worker now, including any update.
fn told(api: &Api) -> Option<UpdateOut> {
    match api.get::<WorkerOut>(paths::ME) {
        Ok(Some(me)) => me.update,
        Ok(None) => None,
        Err(error) => {
            say::warn(
                "Couldn't ask the server about this worker.",
                &[("detail", &error)],
            );
            None
        }
    }
}

/// Sleeps for `wait`, waking early if the worker is stopping.
fn pause(wait: Duration, interrupted: &AtomicBool) {
    let end = std::time::Instant::now() + wait;
    while std::time::Instant::now() < end && !interrupted.load(Ordering::SeqCst) {
        thread::sleep(Duration::from_millis(100));
    }
}

/// What the worker runs: its local plugins, the one plugin whose image it runs in, or
/// the server's plugins, fetched and built. Returns what the server listed, too.
fn plugins_for(
    api: &Api,
    config: &Config,
    local: Option<plugins::Have>,
) -> Result<(Vec<iki_protocol::AvailableOut>, plugins::Have), String> {
    if let Some(have) = local {
        return Ok((Vec::new(), have));
    }
    let listed = bundles::available(api).map_err(|e| unreachable_or_revoked(config, &e))?;
    let have = match &config.image {
        Some(image) => {
            let have = bundles::sync_image(api, &config.cache_dir, &listed, image)?;
            api::retrying(std::time::Instant::now() + Duration::from_secs(120), || {
                tell(api, config, &have.code)
            })
            .map_err(|e| unreachable_or_revoked(config, &e))?;
            have
        }
        None => {
            fetch_plugins(api, config, &listed).map_err(|e| unreachable_or_revoked(config, &e))?
        }
    };
    Ok((listed, have))
}

/// Makes the machine clean for the next job (ADR 0062): stops what the last one left
/// running, empties `IKI_WORKER_CLEAN_DIR`, and fetches the plugin's code again if it was
/// in there.
fn clean_up(
    api: &Api,
    config: &Config,
    listed: &[iki_protocol::AvailableOut],
    have: &mut plugins::Have,
) -> Result<(), String> {
    between::end_strays()?;
    if between::owns_processes() {
        between::empty(Path::new("/dev/shm"))?;
    }
    let Some(dir) = &config.clean_dir else {
        return Ok(());
    };
    between::empty(dir)?;
    if let Some(image) = &config.image
        && config.cache_dir.starts_with(dir)
    {
        *have = bundles::sync_image(api, &config.cache_dir, listed, image)?;
    }
    Ok(())
}

/// Warns about settings that work, but shouldn't be left that way.
fn warn_about(config: &Config, local: Option<&plugins::Have>) {
    if config.allow_http && config.url.starts_with("http://") {
        say::warn(
            "IKI_ALLOW_HTTP is set: this worker's token and every result travel unencrypted. \
             Only for a development cluster.",
            &[("server", &config.url)],
        );
    }
    if let (Some(have), Some(dir)) = (local, &config.plugins_dir)
        && have.code.is_empty()
    {
        say::warn(
            "IKI_PLUGINS_DIR holds no plugins, so this worker can run nothing.",
            &[("path", &dir.display())],
        );
    }
}

/// Runs until SIGTERM or SIGINT, or after one job with `once`. Stopping stops the job's
/// command too (SIGTERM, then SIGKILL after 10 seconds) and reports it `interrupted`, so
/// it can be retried elsewhere.
pub fn start(config: &Config, once: bool) -> Result<(), String> {
    between::guard_memory();
    let interrupted = Arc::new(AtomicBool::new(false));
    for signal in [signal_hook::consts::SIGTERM, signal_hook::consts::SIGINT] {
        signal_hook::flag::register(signal, Arc::clone(&interrupted))
            .map_err(|error| format!("Couldn't watch for signals: {error}"))?;
    }
    let local = config
        .plugins_dir
        .as_deref()
        .map(plugins::discover)
        .transpose()?;
    warn_about(config, local.as_ref());
    let (api, ephemeral) = sign_in(config, local.as_ref().map(|h| h.code.as_slice()))?;
    let (mut listed, mut have) = plugins_for(&api, config, local)?;
    let mut synced = std::time::Instant::now();
    let mut tried = None;
    if let Some(failed) = self_update(config, told(&api).as_ref(), &mut tried) {
        return Err(failed);
    }
    let lease = format!("{}?wait={LEASE_WAIT}", paths::LEASE);
    let mut failures = 0;
    let mut outcome = Ok(());
    let mut idle_since = std::time::Instant::now();
    say::info("Waiting for jobs.", &[("server", &config.url)]);
    while !interrupted.load(Ordering::SeqCst) {
        match api.post::<_, serde_json::Value>(&lease, &serde_json::json!({})) {
            Ok(Some(raw)) => {
                failures = 0;
                if job::run(&api, config, &have, &interrupted, &raw) == Ended::Unauthorized {
                    outcome = Err(revoked(config));
                    break;
                }
                if once {
                    break;
                }
                if let Err(error) = clean_up(&api, config, &listed, &mut have) {
                    // A machine that can't be made clean takes no more jobs.
                    outcome = Err(error);
                    break;
                }
                idle_since = std::time::Instant::now();
            }
            Ok(None) => {
                failures = 0;
                if config
                    .idle_exit
                    .is_some_and(|wait| idle_since.elapsed() >= wait)
                {
                    say::info("No job came for a while; stopping.", &[]);
                    break;
                }
                if synced.elapsed() >= RESYNC_EVERY {
                    synced = std::time::Instant::now();
                    if config.plugins_dir.is_none() && config.image.is_none() {
                        resync(&api, config, &mut listed, &mut have);
                    }
                    if let Some(failed) = self_update(config, told(&api).as_ref(), &mut tried) {
                        outcome = Err(failed);
                        break;
                    }
                }
            }
            Err(Error::Refused {
                code, body, detail, ..
            }) if code == "worker_outdated" => {
                let update = body
                    .get("update")
                    .and_then(|u| serde_json::from_value::<UpdateOut>(u.clone()).ok());
                tried = None;
                outcome = Err(self_update(config, update.as_ref(), &mut tried).unwrap_or(detail));
                break;
            }
            Err(Error::Unauthorized(_)) => {
                outcome = Err(revoked(config));
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

fn unreachable_or_revoked(config: &Config, error: &Error) -> String {
    match error {
        Error::Unauthorized(_) => revoked(config),
        error => format!("Couldn't fetch plugins from {}: {error}", config.url),
    }
}

fn revoked(config: &Config) -> String {
    if config.registration_token.is_some() {
        "The server no longer accepts this worker's token: a Compute admin revoked it. \
         Register again with a registration token."
            .to_owned()
    } else {
        format!(
            "The server no longer accepts this worker's token: it was revoked, or you \
             revoked the Iki worker app. Run iki-worker login {} to sign in again.",
            config.url
        )
    }
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
