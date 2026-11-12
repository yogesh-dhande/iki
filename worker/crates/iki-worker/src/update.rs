//! A worker on someone's computer updating itself (ADR 0060).
//!
//! The server says which release to run. While idle, the worker downloads that release
//! for its target and checks its Sigstore signature against the trusted root it embeds,
//! offline. The signature must come from Iki's release workflow at that version's tag,
//! as its certificate says. Only then does the worker replace its own executable and start
//! again as the new one. A download mirror therefore needn't be trusted. A worker built
//! from source, with no repository to check releases against, never updates itself.

use std::fs::File;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use iki_protocol::UpdateOut;
use sigstore_verify::trust_root::{SIGSTORE_PRODUCTION_TRUSTED_ROOT, TrustedRoot};
use sigstore_verify::types::Bundle;
use sigstore_verify::{VerificationPolicy, Verifier};

use crate::api;
use crate::say;

/// The GitHub repository whose release workflow signs iki-worker, set when a release is
/// built (`IKI_RELEASE_REPOSITORY`).
pub const REPOSITORY: Option<&str> = option_env!("IKI_RELEASE_REPOSITORY");
/// Who issues the release workflow's identity.
const ISSUER: &str = "https://token.actions.githubusercontent.com";
const MAX_RELEASE: u64 = 256 << 20;
const MAX_BUNDLE: u64 = 1 << 20;

/// The target this worker was built for, as release files name it.
pub fn target() -> Option<&'static str> {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("linux", "x86_64") => Some("x86_64-unknown-linux-musl"),
        ("linux", "aarch64") => Some("aarch64-unknown-linux-musl"),
        ("macos", "x86_64") => Some("x86_64-apple-darwin"),
        ("macos", "aarch64") => Some("aarch64-apple-darwin"),
        _ => None,
    }
}

/// The identity a release's signing certificate must name: the release workflow at the
/// version's tag.
pub fn identity(repository: &str, version: &str) -> String {
    format!("https://github.com/{repository}/.github/workflows/release.yml@refs/tags/v{version}")
}

fn is_version(version: &str) -> bool {
    !version.is_empty()
        && version.len() <= 64
        && version.starts_with(|c: char| c.is_ascii_digit())
        && version
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b'+'))
}

/// Checks `artifact` was signed by `identity` (through GitHub Actions), with the bundle in
/// `bundle`, against Sigstore's public-good trusted root.
pub fn verify(artifact: &Path, bundle: &str, identity: &str) -> Result<(), String> {
    let root = TrustedRoot::from_json(SIGSTORE_PRODUCTION_TRUSTED_ROOT)
        .map_err(|e| format!("couldn't read Sigstore's trusted root: {e}"))?;
    let bundle =
        Bundle::from_json(bundle).map_err(|e| format!("it isn't a Sigstore bundle: {e}"))?;
    let policy = VerificationPolicy::any_identity()
        .require_identity(identity)
        .require_issuer(ISSUER);
    let file = File::open(artifact).map_err(|e| format!("couldn't read it: {e}"))?;
    Verifier::new(&root)
        .and_then(|verifier| verifier.verify_reader(file, &bundle, &policy))
        .map(|_| ())
        .map_err(|e| format!("its signature doesn't check out: {e}"))
}

/// Downloads `url` to `target`, no more than `limit` bytes.
fn download(url: &str, target: &Path, limit: u64) -> Result<(), String> {
    let agent = api::agent_config()
        .timeout_global(Some(std::time::Duration::from_secs(600)))
        .build()
        .new_agent();
    let reply = agent
        .get(url)
        .call()
        .map_err(|e| format!("couldn't download {url}: {e}"))?;
    let status = reply.status().as_u16();
    if status != 200 {
        return Err(format!("{url} answered {status}"));
    }
    let mut reader = reply.into_body().into_with_config().limit(limit).reader();
    let mut out = File::create(target).map_err(|e| e.to_string())?;
    std::io::copy(&mut reader, &mut out).map_err(|e| format!("couldn't download {url}: {e}"))?;
    out.sync_all().map_err(|e| e.to_string())
}

/// The `iki-worker` executable in a release tarball, written to `target`.
fn extract(tarball: &Path, folder: &str, target: &Path) -> Result<(), String> {
    let file = File::open(tarball).map_err(|e| e.to_string())?;
    let mut archive = tar::Archive::new(flate2::read::GzDecoder::new(file));
    let wanted = Path::new(folder).join("iki-worker");
    for entry in archive
        .entries()
        .map_err(|e| format!("it isn't a release: {e}"))?
    {
        let mut entry = entry.map_err(|e| format!("it isn't a release: {e}"))?;
        if entry.path().map_err(|e| e.to_string())?.as_ref() != wanted.as_path() {
            continue;
        }
        if !entry.header().entry_type().is_file() || entry.size() > MAX_RELEASE {
            return Err("its iki-worker isn't a regular file".to_owned());
        }
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create(true).truncate(true);
        #[cfg(unix)]
        std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o755);
        let mut out = options.open(target).map_err(|e| e.to_string())?;
        let mut buffer = Vec::new();
        entry
            .read_to_end(&mut buffer)
            .map_err(|e| format!("couldn't unpack it: {e}"))?;
        out.write_all(&buffer).map_err(|e| e.to_string())?;
        out.sync_all().map_err(|e| e.to_string())?;
        return Ok(());
    }
    Err(format!("it has no {}", wanted.display()))
}

/// Whether `url` may be fetched: HTTPS, or HTTP when the worker allows it.
fn fetchable(url: &str, allow_http: bool) -> bool {
    url.starts_with("https://") || (allow_http && url.starts_with("http://"))
}

/// Downloads, checks and installs `update` over `exe`, and returns it to run. It doesn't
/// touch `exe` unless the release checks out and runs.
pub fn install(update: &UpdateOut, exe: &Path, allow_http: bool) -> Result<PathBuf, String> {
    let repository = REPOSITORY.ok_or(
        "this iki-worker was built from source, so it can't check a release's signature; \
         update it yourself",
    )?;
    let target = target().ok_or("there's no release of iki-worker for this computer")?;
    if !is_version(&update.version) {
        return Err("the server named an odd version".to_owned());
    }
    let url = update.url.replace("{target}", target);
    let bundle_url = update.bundle_url.replace("{target}", target);
    if !fetchable(&url, allow_http) || !fetchable(&bundle_url, allow_http) {
        return Err("the server gave a download address that isn't https://".to_owned());
    }
    let dir = exe.parent().ok_or("couldn't tell where iki-worker is")?;
    let work = dir.join(format!(".iki-worker-update-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&work);
    std::fs::create_dir_all(&work).map_err(|e| {
        format!(
            "couldn't write beside {} ({e}): whoever installed it updates it",
            exe.display()
        )
    })?;
    let result = (|| {
        let tarball = work.join("release.tar.gz");
        let bundle = work.join("release.tar.gz.sigstore.json");
        download(&url, &tarball, MAX_RELEASE)?;
        download(&bundle_url, &bundle, MAX_BUNDLE)?;
        let bundle = std::fs::read_to_string(&bundle).map_err(|e| e.to_string())?;
        verify(&tarball, &bundle, &identity(repository, &update.version))?;
        let fresh = dir.join(".iki-worker.new");
        extract(
            &tarball,
            &format!("iki-worker-{}-{target}", update.version),
            &fresh,
        )?;
        // It must run, and be the version it says, before it replaces this one.
        let said = std::process::Command::new(&fresh)
            .arg("--version")
            .output()
            .map_err(|e| format!("the new iki-worker didn't run: {e}"))?;
        let line = String::from_utf8_lossy(&said.stdout);
        if !said.status.success() || !line.starts_with(&format!("iki-worker {} ", update.version)) {
            let _ = std::fs::remove_file(&fresh);
            return Err(format!("the new iki-worker says it's {}", line.trim()));
        }
        std::fs::rename(&fresh, exe).map_err(|e| {
            let _ = std::fs::remove_file(&fresh);
            format!(
                "couldn't replace {} ({e}): whoever installed it updates it",
                exe.display()
            )
        })
    })();
    let _ = std::fs::remove_dir_all(&work);
    result.map(|()| exe.to_path_buf())
}

/// Starts `exe` in this process's place, with the same arguments. Returns only if it can't.
pub fn restart(exe: &Path) -> String {
    say::info(
        "Starting the updated iki-worker.",
        &[("path", &exe.display())],
    );
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let error = std::process::Command::new(exe)
            .args(std::env::args_os().skip(1))
            .exec();
        format!("Couldn't start the updated iki-worker: {error}")
    }
    #[cfg(not(unix))]
    {
        "Updated iki-worker: start it again.".to_owned()
    }
}

#[cfg(test)]
mod tests {
    use std::io::{BufRead, BufReader};
    use std::net::TcpListener;

    use super::*;

    #[test]
    fn names_the_release_workflow_at_the_versions_tag() {
        assert_eq!(
            identity("yogesh-dhande/iki", "0.5.0"),
            "https://github.com/yogesh-dhande/iki/.github/workflows/release.yml@refs/tags/v0.5.0"
        );
        assert!(is_version("0.5.0") && is_version("1.2.3-rc.1"));
        assert!(!is_version("../0.5.0") && !is_version("v0.5.0") && !is_version(""));
    }

    #[test]
    fn refuses_what_isnt_signed_by_the_release_workflow() {
        let dir = std::env::temp_dir().join(format!("iki-update-verify-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("release.tar.gz");
        std::fs::write(&file, b"not a release").unwrap();
        let identity = identity("yogesh-dhande/iki", "0.5.0");
        assert!(verify(&file, "{}", &identity).is_err());
        assert!(verify(&file, "not json", &identity).is_err());
        let bundle = serde_json::json!({
            "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
            "verificationMaterial": {"certificate": {"rawBytes": "AAAA"}, "tlogEntries": []},
            "messageSignature": {
                "messageDigest": {"algorithm": "SHA2_256", "digest": "AAAA"},
                "signature": "AAAA"
            }
        });
        let refused = verify(&file, &bundle.to_string(), &identity).unwrap_err();
        assert!(
            refused.contains("signature") || refused.contains("bundle"),
            "{refused}"
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn only_downloads_over_https_unless_allowed() {
        assert!(fetchable("https://github.com/x", false));
        assert!(!fetchable("http://mirror/x", false));
        assert!(fetchable("http://mirror/x", true));
        assert!(!fetchable("file:///etc/passwd", true));
    }

    /// Serves `body` for every request, `count` times.
    fn serve(body: &'static [u8], count: usize) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        std::thread::spawn(move || {
            for stream in listener.incoming().take(count).flatten() {
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut line = String::new();
                while reader.read_line(&mut line).is_ok_and(|n| n > 2) {
                    line.clear();
                }
                let mut stream = stream;
                let head = format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n", body.len());
                let _ = stream.write_all(head.as_bytes());
                let _ = stream.write_all(body);
            }
        });
        format!("http://{address}")
    }

    #[test]
    fn leaves_itself_alone_when_a_release_doesnt_check_out() {
        if REPOSITORY.is_none() || target().is_none() {
            // Built from source: it refuses before downloading anything.
            let update = UpdateOut {
                version: "0.5.0".into(),
                url: "https://example.invalid/{target}.tar.gz".into(),
                bundle_url: "https://example.invalid/{target}.tar.gz.sigstore.json".into(),
            };
            let refused = install(&update, Path::new("/nonexistent/iki-worker"), false);
            assert!(refused.unwrap_err().contains("built from source") || target().is_none());
            return;
        }
        let dir = std::env::temp_dir().join(format!("iki-update-install-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let exe = dir.join("iki-worker");
        std::fs::write(&exe, b"the old one").unwrap();
        let base = serve(b"{}", 2);
        let update = UpdateOut {
            version: "0.5.0".into(),
            url: format!("{base}/{{target}}.tar.gz"),
            bundle_url: format!("{base}/{{target}}.tar.gz.sigstore.json"),
        };
        assert!(install(&update, &exe, true).is_err());
        assert_eq!(std::fs::read(&exe).unwrap(), b"the old one");
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
