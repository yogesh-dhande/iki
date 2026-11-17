//! Registering a self-hosted worker (ADR 0112).
//!
//! A Compute admin makes a registration token for some teams' machines, with the labels
//! they get. `iki-worker register --url <server> --token <iki_wreg_…>` registers this
//! machine with it once, as GitHub's `config.sh --url --token` does, and saves the
//! worker's own token where only this user can read it. `iki-worker start` then needs
//! nothing else. `iki-worker deregister` revokes it and forgets the token.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use iki_protocol::{Capacity, RegisterIn, RegisteredOut, WorkerOut, paths};

use crate::api::{self, Api, Error};
use crate::worker::{self, Saved};

/// What to say to someone who ran a command self-hosted workers replaced.
pub const NO_SIGN_IN: &str = "iki-worker no longer signs in as you: a Compute admin \
    registers each machine that runs runs for a team. Ask them for a registration token \
    (Compute → Workers), then run iki-worker register --url https://iki.example.com \
    --token iki_wreg_… on this machine.";

/// Where a registered worker keeps its token unless `IKI_WORKER_STATE` says: the user's
/// configuration directory, as the Iki CLI and `gh` do.
pub fn default_state_file(var: &dyn Fn(&str) -> Option<String>) -> Option<PathBuf> {
    let set = |name: &str| {
        var(name)
            .filter(|v| !v.trim().is_empty())
            .map(PathBuf::from)
    };
    let dir = if cfg!(windows) {
        set("APPDATA")?
    } else {
        set("XDG_CONFIG_HOME").or_else(|| set("HOME").map(|home| home.join(".config")))?
    };
    Some(dir.join("iki").join("worker.json"))
}

/// What `register` needs.
pub struct Options {
    pub url: String,
    pub token: String,
    pub state_file: PathBuf,
    pub name: String,
    pub capacity: Capacity,
}

fn tell(text: &str) {
    let _ = writeln!(std::io::stderr().lock(), "{text}");
}

/// Registers this machine with the token, and saves its own.
pub fn register(options: &Options) -> Result<(), String> {
    let server = &options.url;
    // The worker says what it runs each time it starts; until then, nothing.
    let body = RegisterIn {
        capacity: options.capacity.clone(),
        name: options.name.clone(),
        plugins: Vec::new(),
        version: env!("CARGO_PKG_VERSION").to_owned(),
    };
    let registering = Api::new(server, &options.token);
    let registered = match api::retrying(Instant::now() + Duration::from_secs(60), || {
        registering.post::<_, RegisteredOut>(paths::REGISTER, &body)
    }) {
        Ok(Some(registered)) => registered,
        Ok(None) => return Err("The server's answer to registering was empty.".to_owned()),
        Err(Error::Unauthorized(_)) => {
            return Err(format!(
                "{server} didn't take the registration token: it's wrong, expired or \
                 revoked. Ask a Compute admin for a new one (Compute → Workers)."
            ));
        }
        Err(error) => return Err(format!("Couldn't register with {server}: {error}")),
    };
    // Registered again on this machine: the worker it replaces stops working.
    if let Some(old) = worker::load_any(&options.state_file) {
        deregister_saved(&old);
    }
    let saved = Saved {
        url: server.clone(),
        id: registered.id,
        token: registered.token,
    };
    worker::save(&options.state_file, &saved).map_err(|error| {
        format!(
            "Couldn't save the token to {}: {error}",
            options.state_file.display()
        )
    })?;
    let labels = Api::new(server, &saved.token)
        .get::<WorkerOut>(paths::ME)
        .ok()
        .flatten()
        .map(|me| me.labels)
        .unwrap_or_default();
    let tier = registered.tier.replace('_', "-");
    tell(&format!(
        "Registered {} with {server} as a {tier} worker{}. Run iki-worker start to take runs.",
        options.name,
        if labels.is_empty() {
            String::new()
        } else {
            format!(", labelled {}", labels.join(", "))
        }
    ));
    Ok(())
}

fn deregister_saved(saved: &Saved) -> bool {
    let api = Api::new(&saved.url, &saved.token);
    match api.post::<_, serde_json::Value>(paths::DEREGISTER, &serde_json::json!({})) {
        Ok(_) | Err(Error::Unauthorized(_)) => true,
        Err(error) => {
            tell(&format!(
                "Couldn't tell {} this worker is going ({error}); a Compute admin can revoke \
                 it under Compute → Workers.",
                saved.url
            ));
            false
        }
    }
}

/// Deregisters this machine's worker and forgets its token.
pub fn deregister(state_file: &Path) -> Result<(), String> {
    let Some(saved) = worker::load_any(state_file) else {
        tell("This machine has no worker registered.");
        return Ok(());
    };
    deregister_saved(&saved);
    std::fs::remove_file(state_file).map_err(|error| {
        format!(
            "Couldn't remove the token at {}: {error}",
            state_file.display()
        )
    })?;
    tell(&format!("Deregistered from {}.", saved.url));
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::*;

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect();
        move |name| map.get(name).cloned()
    }

    #[test]
    #[cfg(unix)]
    fn keeps_its_token_in_the_users_configuration() {
        assert_eq!(
            default_state_file(&env(&[("HOME", "/home/ada")])),
            Some(PathBuf::from("/home/ada/.config/iki/worker.json"))
        );
        assert_eq!(
            default_state_file(&env(&[("HOME", "/home/ada"), ("XDG_CONFIG_HOME", "/cfg")])),
            Some(PathBuf::from("/cfg/iki/worker.json"))
        );
        assert_eq!(default_state_file(&env(&[])), None);
    }

    #[test]
    fn says_where_signing_in_went() {
        assert!(NO_SIGN_IN.contains("iki-worker register --url"));
        assert!(NO_SIGN_IN.contains("Compute admin"));
    }
}
