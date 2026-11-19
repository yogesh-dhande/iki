//! A job's image, for Apptainer: checked with cosign against who the deployment says may
//! sign images (ADR 0063), then pulled by its digest into a SIF file in the worker's
//! image cache, which every job of that image shares.
//!
//! Nothing stands in front of a cluster's nodes as Kubernetes' admission controller does,
//! so the worker checks the signature itself before an image is pulled, and only a SIF
//! file it pulled after checking is in the cache: one cut short is pulled again. Two
//! workers sharing the cache (two login nodes) take turns, by a lock on the file.

use std::fs::{File, OpenOptions};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use iki_protocol::RunInputJobSlurmImagePolicy;
use rustix::fs::{FlockOperation, flock};

use crate::say;

/// Where an image is, and who signed it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pulled {
    pub sif: PathBuf,
    /// Who signed it, as cosign checked: empty when the deployment names no signers.
    pub signer: String,
}

/// The programs that check and pull images.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Tools {
    pub apptainer: String,
    pub cosign: String,
}

/// The image's digest, from `registry/name@sha256:…`.
fn digest(image: &str) -> Result<&str, String> {
    image
        .rsplit_once("@sha256:")
        .map(|(_, hex)| hex)
        .filter(|hex| hex.len() == 64 && hex.bytes().all(|b| b.is_ascii_hexdigit()))
        .ok_or_else(|| format!("The image {image} isn't pinned by its digest."))
}

/// The image by its repository and digest alone: Apptainer refuses a reference with both a
/// tag and a digest (`registry/name:tag@sha256:…`), and the digest is what's pinned.
fn by_digest(image: &str) -> String {
    let Some((name, digest)) = image.rsplit_once('@') else {
        return image.to_owned();
    };
    // A `:` after the last `/` starts a tag; one before it is the registry's port.
    let last = name.rfind('/').map_or(0, |i| i + 1);
    let repository = match name[last..].find(':') {
        Some(tag) => &name[..last + tag],
        None => name,
    };
    format!("{repository}@{digest}")
}

/// The image's SIF file in `cache`, checked and pulled if it isn't there yet.
pub fn ensure(
    tools: &Tools,
    cache: &Path,
    image: &str,
    policy: Option<&RunInputJobSlurmImagePolicy>,
) -> Result<Pulled, String> {
    let digest = digest(image)?;
    crate::slurm::private_dir(cache)
        .map_err(|e| format!("Couldn't make the image cache {}: {e}", cache.display()))?;
    let sif = cache.join(format!("sha256-{digest}.sif"));
    let lock = lock(&cache.join(format!("sha256-{digest}.lock")))?;
    // Whether it's pulled or not, the signature is checked for each job: an authority the
    // deployment stopped trusting runs nothing more.
    let signer = if let Some(policy) = policy {
        verify(tools, image, policy)?
    } else {
        say::warn(
            "The deployment names no image signers, so this image runs unchecked. An Iki \
             administrator can set containerTier.imagePolicy in the chart.",
            &[("image", &image)],
        );
        String::new()
    };
    if !sif.is_file() {
        say::info("Pulling an image for Apptainer.", &[("image", &image)]);
        let partial = cache.join(format!("sha256-{digest}.sif.partial"));
        let _ = std::fs::remove_file(&partial);
        run(
            Command::new(&tools.apptainer)
                .args(["pull", "--disable-cache"])
                .arg(&partial)
                .arg(format!("docker://{}", by_digest(image))),
            "apptainer pull",
        )?;
        std::fs::rename(&partial, &sif)
            .map_err(|e| format!("Couldn't keep the pulled image: {e}"))?;
    }
    drop(lock);
    Ok(Pulled { sif, signer })
}

/// Holds an exclusive lock on `path` until it's dropped.
fn lock(path: &Path) -> Result<File, String> {
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(path)
        .map_err(|e| format!("Couldn't lock {}: {e}", path.display()))?;
    flock(&file, FlockOperation::LockExclusive)
        .map_err(|e| format!("Couldn't lock {}: {e}", path.display()))?;
    Ok(file)
}

/// Checks the image's signature against each authority the policy names, in turn: Iki's
/// release workflow, for Iki's own images, then the deployment's build. Returns who
/// signed it.
fn verify(
    tools: &Tools,
    image: &str,
    policy: &RunInputJobSlurmImagePolicy,
) -> Result<String, String> {
    let mut tried = Vec::new();
    let authorities = [
        policy.iki.as_ref().map(|a| {
            (
                "Iki's release workflow",
                &a.issuer,
                a.subject.as_deref(),
                a.subject_regexp.as_deref(),
                a.repository.as_deref(),
            )
        }),
        policy.approvals.as_ref().map(|a| {
            (
                "the deployment's build",
                &a.issuer,
                a.subject.as_deref(),
                a.subject_regexp.as_deref(),
                a.repository.as_deref(),
            )
        }),
    ];
    for (who, issuer, subject, pattern, repository) in authorities.into_iter().flatten() {
        let mut command = Command::new(&tools.cosign);
        command.args(["verify", "--certificate-oidc-issuer", issuer]);
        match (
            subject.filter(|s| !s.is_empty()),
            pattern.filter(|p| !p.is_empty()),
        ) {
            (Some(subject), _) => command.args(["--certificate-identity", subject]),
            (None, Some(pattern)) => command.args(["--certificate-identity-regexp", pattern]),
            (None, None) => continue,
        };
        // The deployment's build signs into a repository of its own (ADR 0063).
        if let Some(repository) = repository.filter(|r| !r.is_empty()) {
            command.env("COSIGN_REPOSITORY", repository);
        }
        command.arg(image);
        match run(&mut command, "cosign verify") {
            Ok(()) => {
                let identity = subject.or(pattern).unwrap_or_default();
                say::info(
                    "Checked an image's signature.",
                    &[("image", &image), ("signer", &identity)],
                );
                return Ok(format!("{who}: {identity}"));
            }
            Err(error) => tried.push(format!("{who}: {error}")),
        }
    }
    Err(format!(
        "Its image {image} isn't signed by anyone the deployment trusts, so it didn't run. {}",
        tried.join(" ")
    ))
}

fn run(command: &mut Command, what: &str) -> Result<(), String> {
    let output = command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| format!("Couldn't run {what}: {e}."))?;
    if output.status.success() {
        return Ok(());
    }
    let said = String::from_utf8_lossy(&output.stderr);
    let tail: Vec<&str> = said.lines().rev().take(5).collect();
    let tail: Vec<&str> = tail.into_iter().rev().collect();
    Err(format!(
        "{what} failed ({}): {}",
        output.status,
        tail.join(" ")
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use iki_protocol::{RunInputJobSlurmImagePolicyApprovals, RunInputJobSlurmImagePolicyIki};

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("iki-image-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// A script standing in for a tool. A child process writes it, so no thread here holds
    /// it open for writing when another test's thread forks: running it then would fail
    /// with "text file busy", and the fake would never run.
    fn fake(dir: &Path, name: &str, script: &str) -> String {
        use std::io::Write;
        let path = dir.join(name);
        let mut writer = Command::new("/bin/sh")
            .args(["-c", "cat > \"$1\" && chmod 755 \"$1\"", "sh"])
            .arg(&path)
            .stdin(Stdio::piped())
            .spawn()
            .unwrap();
        let mut stdin = writer.stdin.take().unwrap();
        write!(stdin, "#!/bin/sh\n{script}\n").unwrap();
        drop(stdin);
        assert!(writer.wait().unwrap().success());
        path.to_string_lossy().into_owned()
    }

    fn image() -> String {
        format!("ghcr.io/acme/fold@sha256:{}", "c".repeat(64))
    }

    fn policy() -> RunInputJobSlurmImagePolicy {
        RunInputJobSlurmImagePolicy {
            iki: Some(RunInputJobSlurmImagePolicyIki {
                issuer: "https://token.actions.githubusercontent.com".into(),
                subject: None,
                subject_regexp: Some("^iki$".into()),
                repository: None,
            }),
            approvals: Some(RunInputJobSlurmImagePolicyApprovals {
                issuer: "https://token.actions.githubusercontent.com".into(),
                subject: Some("build".into()),
                subject_regexp: None,
                repository: Some("ghcr.io/acme/approvals".into()),
            }),
        }
    }

    #[test]
    fn apptainer_pulls_an_image_by_its_digest_alone() {
        let hex = "c".repeat(64);
        for (given, pulled) in [
            (
                format!("ghcr.io/acme/fold@sha256:{hex}"),
                format!("ghcr.io/acme/fold@sha256:{hex}"),
            ),
            (
                format!("docker.io/library/python:3.13-alpine@sha256:{hex}"),
                format!("docker.io/library/python@sha256:{hex}"),
            ),
            (
                format!("registry.lab:5000/fold:1.2@sha256:{hex}"),
                format!("registry.lab:5000/fold@sha256:{hex}"),
            ),
            (
                format!("registry.lab:5000/fold@sha256:{hex}"),
                format!("registry.lab:5000/fold@sha256:{hex}"),
            ),
        ] {
            assert_eq!(by_digest(&given), pulled);
        }
    }

    #[test]
    fn an_image_runs_once_someone_the_deployment_trusts_signed_it() {
        let dir = scratch("signed");
        let log = dir.join("log");
        let log_s = log.display();
        // Iki didn't sign it; the deployment's build did, into its own repository.
        let cosign = fake(
            &dir,
            "cosign",
            &format!(
                "echo \"cosign $* repo=$COSIGN_REPOSITORY\" >> {log_s}\n\
                 case \"$*\" in *--certificate-identity\\ build*) exit 0;; esac\nexit 1"
            ),
        );
        let apptainer = fake(
            &dir,
            "apptainer",
            &format!("echo \"apptainer $*\" >> {log_s}\necho sif > \"$3\""),
        );
        let tools = Tools { apptainer, cosign };
        let cache = dir.join("images");
        let pulled = ensure(&tools, &cache, &image(), Some(&policy())).unwrap();
        assert_eq!(pulled.signer, "the deployment's build: build");
        assert_eq!(
            pulled.sif,
            cache.join(format!("sha256-{}.sif", "c".repeat(64)))
        );
        assert!(pulled.sif.is_file());
        let said = std::fs::read_to_string(&log).unwrap();
        let lines: Vec<&str> = said.lines().collect();
        assert!(
            lines[0].contains("--certificate-identity-regexp ^iki$"),
            "{said}"
        );
        assert!(lines[1].ends_with("repo=ghcr.io/acme/approvals"), "{said}");
        assert!(
            lines[2].starts_with("apptainer pull --disable-cache"),
            "{said}"
        );
        assert!(
            lines[2].ends_with(&format!("docker://{}", image())),
            "{said}"
        );
        // The next job's checked again, and not pulled again.
        ensure(&tools, &cache, &image(), Some(&policy())).unwrap();
        let said = std::fs::read_to_string(&log).unwrap();
        assert_eq!(
            said.lines().filter(|l| l.starts_with("apptainer")).count(),
            1
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&cache).unwrap().permissions().mode();
            assert_eq!(mode & 0o777, 0o700);
        }
    }

    #[test]
    fn an_image_no_one_trusted_signed_doesnt_run() {
        let dir = scratch("unsigned");
        let cosign = fake(&dir, "cosign", "echo 'no matching signatures' >&2\nexit 1");
        let apptainer = fake(&dir, "apptainer", "exit 0");
        let tools = Tools { apptainer, cosign };
        let error = ensure(&tools, &dir.join("images"), &image(), Some(&policy())).unwrap_err();
        assert!(
            error.contains("isn't signed by anyone the deployment trusts"),
            "{error}"
        );
        assert!(error.contains("no matching signatures"), "{error}");
        assert!(
            !dir.join("images")
                .join(format!("sha256-{}.sif", "c".repeat(64)))
                .exists()
        );
        let floating = ensure(
            &tools,
            &dir.join("images"),
            "ghcr.io/acme/fold:latest",
            None,
        );
        assert!(floating.unwrap_err().contains("isn't pinned by its digest"));
    }
}
