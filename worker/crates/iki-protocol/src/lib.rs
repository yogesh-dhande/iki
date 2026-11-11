//! Wire types for the Iki worker protocol (ADR 0010, ADR 0043).
//!
//! - [`RunInput`] and [`RunOutput`]: what an action's command reads (`input.json`) and
//!   writes (`output.json`), from the `run-input/v1` and `run-output/v1` schemas in
//!   `spec/`.
//! - The request and response bodies of the `/worker/v1` API, from its description
//!   (`worker-openapi.json`, beside this crate), and its [`paths`].
//!
//! The types are generated (`make protocol`), and CI fails if they drift from the
//! server's. Unknown fields are ignored and unknown enum values read as `Unknown`, so a
//! worker keeps working against a newer server.

#[rustfmt::skip]
#[allow(missing_docs, clippy::all, clippy::pedantic)]
mod generated;

pub use generated::*;

/// The worker API version this crate speaks, matching the server's `/worker/v1` prefix.
pub const PROTOCOL_VERSION: u32 = 1;

/// Fills a path's `{placeholders}` from [`paths`], in order.
///
/// ```
/// use iki_protocol::{fill, paths};
/// assert_eq!(
///     fill(paths::JOBS_UPLOADS_PARTS, &["j1", "u2"]),
///     "/worker/v1/jobs/j1/uploads/u2/parts",
/// );
/// ```
#[must_use]
pub fn fill(path: &str, values: &[&str]) -> String {
    let mut out = String::with_capacity(path.len());
    let mut values = values.iter();
    let mut rest = path;
    while let Some(start) = rest.find('{') {
        out.push_str(&rest[..start]);
        let Some(end) = rest[start..].find('}') else {
            break;
        };
        out.push_str(values.next().copied().unwrap_or_default());
        rest = &rest[start + end + 1..];
    }
    out.push_str(rest);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fill_replaces_placeholders_in_order() {
        assert_eq!(
            fill(paths::JOBS_HEARTBEAT, &["abc"]),
            "/worker/v1/jobs/abc/heartbeat"
        );
        assert_eq!(fill(paths::LEASE, &[]), "/worker/v1/lease");
    }

    #[test]
    fn paths_carry_the_protocol_version() {
        assert!(paths::REGISTER.starts_with(&format!("/worker/v{PROTOCOL_VERSION}/")));
    }

    #[test]
    fn run_input_reads_the_server_payload() {
        let input: RunInput = serde_json::from_value(serde_json::json!({
            "apiVersion": "iki/v1",
            "kind": "RunInput",
            "run": {
                "id": "r", "hrid": "RUN-1", "attempt": 1, "action": "protein.fold",
                "plugin": {"name": "protein", "version": "1.0.0", "sha": null},
                "project": "p",
            },
            "job": {
                "id": "j", "lease_token": "t", "lease_expires_at": "2026-01-01T00:00:00Z",
                "heartbeat_every": 20, "timeout": 3600, "resources": {"cpu": 0.5},
                "added_later": true,
            },
            "command": ["python", "fold.py"],
            "environment": "default",
            "params": {"seed": 1},
            "inputs": {"protein": [{
                "id": "e", "hrid": "PRO-1", "type": "protein.Protein",
                "schema_version": 1, "revision": 2, "values": {"name": "abl"},
            }]},
        }))
        .unwrap();
        assert_eq!(input.api_version, RunInputApiVersion::IkiV1);
        assert_eq!(input.run.plugin.sha, None);
        assert_eq!(input.job.resources.cpu, Some(0.5));
        assert_eq!(input.inputs["protein"][0].r#type, "protein.Protein");
    }

    #[test]
    fn run_output_writes_camel_case_and_skips_what_is_unset() {
        let output = RunOutput {
            api_version: RunOutputApiVersion::IkiV1,
            kind: RunOutputKind::RunOutput,
            updates: None,
            creates: None,
            metrics: Some([("score".to_owned(), 0.9)].into()),
            messages: None,
        };
        assert_eq!(
            serde_json::to_value(&output).unwrap(),
            serde_json::json!({"apiVersion": "iki/v1", "kind": "RunOutput", "metrics": {"score": 0.9}}),
        );
    }

    #[test]
    fn unknown_enum_values_read_as_unknown() {
        let state: UploadOutState = serde_json::from_str("\"quarantined\"").unwrap();
        assert_eq!(state, UploadOutState::Unknown);
        assert!(serde_json::from_str::<RunOutputKind>("\"Other\"").is_err());
    }
}
