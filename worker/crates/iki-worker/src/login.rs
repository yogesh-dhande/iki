//! Signing a worker in on someone's own computer (ADR 0058).
//!
//! `iki-worker login <server>` goes through OAuth's device flow (RFC 8628) as the
//! `iki-worker` client, for the `worker` scope alone: the person confirms a code in their
//! browser and chooses the projects it may run their actions in. The worker registers
//! with the access token that gives it, which registering spends, and saves its own token
//! where only they can read it. `iki-worker logout` deregisters it and forgets the token.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use iki_protocol::{Capacity, RegisterIn, RegisteredOut, paths};
use serde::Deserialize;

use crate::api::{self, Api, Error};
use crate::worker::{self, Saved};

/// The OAuth client and scope the server keeps for this (ADR 0058).
const CLIENT_ID: &str = "iki-worker";
const SCOPE: &str = "worker";
const DEVICE_CODE: &str = "urn:ietf:params:oauth:grant-type:device_code";

/// Where a signed-in worker keeps its token unless `IKI_WORKER_STATE` says: the user's
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

/// What `login` needs besides the server.
pub struct Options {
    pub url: String,
    pub state_file: PathBuf,
    pub name: String,
    pub capacity: Capacity,
    pub open_browser: bool,
}

#[derive(Deserialize)]
struct Started {
    device_code: String,
    user_code: String,
    verification_uri: String,
    verification_uri_complete: Option<String>,
    interval: Option<u64>,
    expires_in: Option<u64>,
}

#[derive(Deserialize)]
struct Tokens {
    access_token: String,
}

#[derive(Deserialize)]
struct OAuthError {
    error: Option<String>,
}

fn tell(text: &str) {
    let _ = writeln!(std::io::stderr().lock(), "{text}");
}

/// Text from the server, made safe to print: nothing that moves the cursor or recolours
/// the terminal.
fn printable(text: &str) -> String {
    text.chars().filter(char::is_ascii_graphic).collect()
}

fn form(
    agent: &ureq::Agent,
    url: &str,
    fields: &[(&str, &str)],
) -> Result<(u16, serde_json::Value), String> {
    let mut reply = agent
        .post(url)
        .header("Accept", "application/json")
        .send_form(fields.iter().copied())
        .map_err(|error| format!("Couldn't reach {url}: {error}"))?;
    let status = reply.status().as_u16();
    let body = reply
        .body_mut()
        .with_config()
        .limit(64 * 1024)
        .read_json()
        .unwrap_or(serde_json::Value::Null);
    Ok((status, body))
}

/// Opens `page` in the person's browser, if it's on `server` and there's a browser to
/// open it in. Failing to is fine: the page and code are printed too.
fn open(page: &str, server: &str) {
    if !page.starts_with(&format!("{server}/")) {
        return;
    }
    let launched = if cfg!(target_os = "macos") {
        std::process::Command::new("open").arg(page).spawn()
    } else if cfg!(windows) {
        std::process::Command::new("rundll32")
            .args(["url.dll,FileProtocolHandler", page])
            .spawn()
    } else if std::env::var_os("DISPLAY").is_some() || std::env::var_os("WAYLAND_DISPLAY").is_some()
    {
        std::process::Command::new("xdg-open").arg(page).spawn()
    } else {
        return;
    };
    if let Ok(mut child) = launched {
        // Reap it in the background; whether it worked shows in the browser.
        std::thread::spawn(move || child.wait());
    }
}

/// Polls until the person confirms the code (or declines, or it expires), as RFC 8628 says:
/// every `interval` seconds, five more each time the server says to slow down.
fn wait_for_token(agent: &ureq::Agent, server: &str, started: &Started) -> Result<String, String> {
    let mut interval = Duration::from_secs(started.interval.unwrap_or(5).clamp(1, 60));
    let deadline = Instant::now() + Duration::from_secs(started.expires_in.unwrap_or(900));
    loop {
        if Instant::now() + interval > deadline {
            return Err(
                "The code expired before it was confirmed. Run iki-worker login again.".to_owned(),
            );
        }
        std::thread::sleep(interval);
        let (status, body) = form(
            agent,
            &format!("{server}/oauth/token/"),
            &[
                ("grant_type", DEVICE_CODE),
                ("device_code", &started.device_code),
                ("client_id", CLIENT_ID),
            ],
        )?;
        if status == 200 {
            let tokens: Tokens = serde_json::from_value(body)
                .map_err(|error| format!("{server}'s answer didn't parse: {error}"))?;
            return Ok(tokens.access_token);
        }
        let error = serde_json::from_value::<OAuthError>(body)
            .ok()
            .and_then(|e| e.error);
        match error.as_deref() {
            Some("authorization_pending") => {}
            Some("slow_down") => interval += Duration::from_secs(5),
            Some("access_denied") => {
                return Err("The sign-in was declined in the browser.".to_owned());
            }
            Some("expired_token") => {
                return Err(
                    "The code expired before it was confirmed. Run iki-worker login again."
                        .to_owned(),
                );
            }
            other => {
                return Err(format!(
                    "Signing in failed: {}.",
                    other.map_or_else(|| status.to_string(), printable)
                ));
            }
        }
    }
}

/// Signs the worker in as whoever confirms the code, and saves its token.
pub fn login(options: &Options) -> Result<(), String> {
    let agent = api::agent_config()
        .timeout_global(Some(Duration::from_secs(60)))
        .build()
        .new_agent();
    let server = &options.url;
    let (status, body) = form(
        &agent,
        &format!("{server}/oauth/device-authorization/"),
        &[("client_id", CLIENT_ID), ("scope", SCOPE)],
    )?;
    if status != 200 {
        let error = serde_json::from_value::<OAuthError>(body)
            .ok()
            .and_then(|e| e.error);
        return Err(match error {
            Some(error) => format!(
                "{server} won't sign in a worker ({}). Ask your Iki administrator.",
                printable(&error)
            ),
            None => format!("{server} didn't start a sign-in ({status}). Is it an Iki deployment?"),
        });
    }
    let started: Started = serde_json::from_value(body)
        .map_err(|error| format!("{server}'s answer didn't parse: {error}"))?;
    let page = printable(&started.verification_uri);
    let direct = started
        .verification_uri_complete
        .as_deref()
        .map_or_else(|| page.clone(), printable);
    tell(&format!(
        "To sign this worker in, open {page} and enter the code {}",
        printable(&started.user_code)
    ));
    if options.open_browser {
        open(&direct, server);
    }
    let token = wait_for_token(&agent, server, &started)?;
    // The worker says what it runs each time it starts; until then, nothing.
    let body = RegisterIn {
        capacity: options.capacity.clone(),
        name: options.name.clone(),
        plugins: Vec::new(),
        version: env!("CARGO_PKG_VERSION").to_owned(),
    };
    let registering = Api::new(server, &token);
    let registered = match api::retrying(Instant::now() + Duration::from_secs(60), || {
        registering.post::<_, RegisteredOut>(paths::REGISTER, &body)
    }) {
        Ok(Some(registered)) => registered,
        Ok(None) => return Err("The server's answer to registering was empty.".to_owned()),
        Err(Error::Unauthorized(detail)) => return Err(format!("Couldn't register: {detail}")),
        Err(error) => return Err(format!("Couldn't register with {server}: {error}")),
    };
    // Signed in again on this computer: the worker it replaces stops working.
    if let Some(old) = worker::load_any(&options.state_file) {
        deregister(&old);
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
    tell(&format!(
        "Signed in to {server} as worker {}. Run iki-worker start to take your runs.",
        options.name
    ));
    Ok(())
}

fn deregister(saved: &Saved) -> bool {
    let api = Api::new(&saved.url, &saved.token);
    match api.post::<_, serde_json::Value>(paths::DEREGISTER, &serde_json::json!({})) {
        Ok(_) | Err(Error::Unauthorized(_)) => true,
        Err(error) => {
            tell(&format!(
                "Couldn't tell {} this worker is signing out ({error}); \
                 revoke it under Your workers.",
                saved.url
            ));
            false
        }
    }
}

/// Deregisters the signed-in worker and forgets its token.
pub fn logout(state_file: &Path) -> Result<(), String> {
    let Some(saved) = worker::load_any(state_file) else {
        tell("This computer has no worker signed in.");
        return Ok(());
    };
    deregister(&saved);
    std::fs::remove_file(state_file).map_err(|error| {
        format!(
            "Couldn't remove the token at {}: {error}",
            state_file.display()
        )
    })?;
    tell(&format!("Signed out of {}.", saved.url));
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
    fn prints_only_plain_text_from_the_server() {
        assert_eq!(printable("ABCD-EFGH\u{1b}[2J\n"), "ABCD-EFGH[2J");
    }
}
