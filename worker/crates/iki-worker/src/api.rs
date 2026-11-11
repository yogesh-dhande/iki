//! Calls to the server's `/worker/v1` API (ADR 0043).
//!
//! HTTPS through rustls, checked against the operating system's certificates (with
//! `SSL_CERT_FILE` honoured on Linux), through `HTTPS_PROXY` when it's set. No redirects
//! are followed, so the token is only ever sent to the configured server.

use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use serde::de::DeserializeOwned;
use ureq::Agent;
use ureq::tls::{RootCerts, TlsConfig, TlsProvider};

/// How a call failed, by what the worker should do about it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// 401: the token is missing, wrong or revoked. Nothing to do but stop.
    Unauthorized(String),
    /// 409 `stale_lease`: the worker doesn't hold this job any more. Drop it.
    Stale(String),
    /// Any other answer in the 400s: the request itself is wrong, so sending it again
    /// won't help.
    Refused {
        status: u16,
        code: String,
        detail: String,
        /// The whole problem body, for members such as `next_offset`.
        body: serde_json::Value,
    },
    /// The network, a timeout, 429 or the 500s: worth trying again shortly.
    Transient(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unauthorized(detail) | Self::Stale(detail) | Self::Transient(detail) => {
                f.write_str(detail)
            }
            Self::Refused {
                status,
                code,
                detail,
                ..
            } => {
                write!(f, "{detail} ({status} {code})")
            }
        }
    }
}

/// A client for one server, with one token.
#[derive(Clone)]
pub struct Api {
    agent: Agent,
    base: String,
    token: String,
}

/// HTTPS as the worker makes it: rustls with the OS's certificates, `HTTPS_PROXY`, no
/// redirects, and every status answered rather than raised.
pub fn agent_config() -> ureq::config::ConfigBuilder<ureq::typestate::AgentScope> {
    let tls = TlsConfig::builder()
        .provider(TlsProvider::Rustls)
        .root_certs(RootCerts::PlatformVerifier)
        .unversioned_rustls_crypto_provider(Arc::new(rustls::crypto::ring::default_provider()))
        .build();
    Agent::config_builder()
        .tls_config(tls)
        .proxy(ureq::Proxy::try_from_env())
        .http_status_as_error(false)
        .max_redirects(0)
        .user_agent(concat!("iki-worker/", env!("CARGO_PKG_VERSION")))
        .timeout_connect(Some(Duration::from_secs(15)))
}

fn agent() -> Agent {
    agent_config()
        // The lease waits up to 30 seconds for work; anything slower than this is stuck.
        .timeout_global(Some(Duration::from_secs(60)))
        .build()
        .new_agent()
}

impl Api {
    pub fn new(base: &str, token: &str) -> Self {
        Self {
            agent: agent(),
            base: base.to_owned(),
            token: token.to_owned(),
        }
    }

    /// The same server, with another token.
    pub fn with_token(&self, token: &str) -> Self {
        Self {
            agent: self.agent.clone(),
            base: self.base.clone(),
            token: token.to_owned(),
        }
    }

    /// POSTs `body` as JSON to `path` (from `iki_protocol::paths`, filled). `None` is a
    /// 204: no content.
    pub fn post<B: Serialize, R: DeserializeOwned>(
        &self,
        path: &str,
        body: &B,
    ) -> Result<Option<R>, Error> {
        let reply = self
            .agent
            .post(format!("{}{path}", self.base))
            .header("Authorization", format!("Bearer {}", self.token))
            .header("Accept", "application/json")
            .send_json(body);
        read(reply)
    }

    pub fn get<R: DeserializeOwned>(&self, path: &str) -> Result<Option<R>, Error> {
        let reply = self
            .agent
            .get(format!("{}{path}", self.base))
            .header("Authorization", format!("Bearer {}", self.token))
            .header("Accept", "application/json")
            .call();
        read(reply)
    }
}

fn read<R: DeserializeOwned>(
    reply: Result<ureq::http::Response<ureq::Body>, ureq::Error>,
) -> Result<Option<R>, Error> {
    let mut reply = reply.map_err(|error| Error::Transient(error.to_string()))?;
    let status = reply.status().as_u16();
    if status == 204 {
        return Ok(None);
    }
    if (200..300).contains(&status) {
        return reply
            .body_mut()
            .read_json::<R>()
            .map(Some)
            .map_err(|error| {
                Error::Transient(format!("The server's answer didn't parse: {error}"))
            });
    }
    let body: serde_json::Value = reply
        .body_mut()
        .with_config()
        .limit(64 * 1024)
        .read_json()
        .unwrap_or(serde_json::Value::Null);
    let text = |key: &str| {
        body.get(key)
            .and_then(serde_json::Value::as_str)
            .unwrap_or_default()
    };
    let detail = match text("detail") {
        "" => format!("The server answered {status}."),
        detail => detail.to_owned(),
    };
    Err(match (status, text("code")) {
        (401, _) => Error::Unauthorized(detail),
        (409, "stale_lease") => Error::Stale(detail),
        (429 | 500.., _) => Error::Transient(detail),
        (_, code) => Error::Refused {
            status,
            code: code.to_owned(),
            detail,
            body,
        },
    })
}

/// How long to wait before try number `attempt` (from 1): 1, 2, 4… seconds up to 30, give
/// or take a fifth so a fleet that lost the server together doesn't come back together.
pub fn backoff(attempt: u32) -> Duration {
    let base = 1u64 << attempt.saturating_sub(1).min(5);
    let millis = base.min(30) * 1000;
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| u64::from(d.subsec_nanos()));
    let spread = millis / 5;
    Duration::from_millis(millis - spread + nanos % (2 * spread + 1))
}

/// Calls `call` until it succeeds, fails for a reason trying again won't fix, or
/// `deadline` passes, waiting a little longer each time.
pub fn retrying<T>(
    deadline: Instant,
    mut call: impl FnMut() -> Result<T, Error>,
) -> Result<T, Error> {
    let mut attempt = 0;
    loop {
        attempt += 1;
        match call() {
            Err(Error::Transient(detail)) => {
                let wait = backoff(attempt);
                if Instant::now() + wait > deadline {
                    return Err(Error::Transient(detail));
                }
                crate::say::warn(
                    "A call to the server failed; trying again.",
                    &[("detail", &detail), ("in_seconds", &wait.as_secs())],
                );
                thread::sleep(wait);
            }
            done => return done,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_doubles_up_to_thirty_seconds_with_some_spread() {
        for (attempt, base) in [(1, 1000), (2, 2000), (3, 4000), (6, 30_000), (40, 30_000)] {
            let wait = backoff(attempt).as_millis();
            assert!(
                wait >= base * 4 / 5 && wait <= base * 6 / 5,
                "{attempt}: {wait}"
            );
        }
    }

    #[test]
    fn retrying_stops_on_errors_that_wont_go_away() {
        let mut calls = 0;
        let result: Result<(), Error> = retrying(Instant::now() + Duration::from_secs(60), || {
            calls += 1;
            Err(Error::Stale("gone".into()))
        });
        assert_eq!(result, Err(Error::Stale("gone".into())));
        assert_eq!(calls, 1);
    }

    #[test]
    fn retrying_gives_up_at_the_deadline() {
        let mut calls = 0;
        let result: Result<(), Error> = retrying(Instant::now(), || {
            calls += 1;
            Err(Error::Transient("down".into()))
        });
        assert_eq!(result, Err(Error::Transient("down".into())));
        assert_eq!(calls, 1);
    }
}
