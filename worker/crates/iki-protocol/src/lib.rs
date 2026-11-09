//! Wire types for the Iki worker protocol.
//!
//! Types are generated from the JSON Schemas in `spec/` once the run protocol
//! is defined. For now the crate pins the protocol version that workers and
//! the server negotiate on.

/// The worker API version this crate speaks, matching the server's `/worker/v1` prefix.
pub const PROTOCOL_VERSION: u32 = 1;

/// Returns the path of a worker API endpoint, for example `"/worker/v1/lease"`.
///
/// Leading and trailing slashes in `endpoint` are ignored.
#[must_use]
pub fn worker_path(endpoint: &str) -> String {
    format!("/worker/v{PROTOCOL_VERSION}/{}", endpoint.trim_matches('/'))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worker_path_prefixes_the_protocol_version() {
        assert_eq!(worker_path("lease"), "/worker/v1/lease");
    }

    #[test]
    fn worker_path_ignores_surrounding_slashes() {
        assert_eq!(worker_path("/files/presign/"), "/worker/v1/files/presign");
    }
}
