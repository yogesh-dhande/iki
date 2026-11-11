//! Files in and out of a job.
//!
//! - **Inputs**: before the command starts, every file its inputs hold (the run input's
//!   `files`) is downloaded into the job's directory, its size and SHA-256 checked, and
//!   its `path` added to `input.json`.
//! - **Results**: where `output.json` gives a file field `{"$file": "model.pdb"}` (a path
//!   in `IKI_OUT_DIR`, optionally with `media_type` and `name`), the file is uploaded in
//!   parts, checked by the server, and replaced with its file ID.
//!
//! Object storage URLs are signed for this one transfer, so they get no `Authorization`
//! header: the worker's token never leaves for anywhere but the server.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

use iki_protocol::{
    DownloadOut, LeaseIn, PartOut, PartsIn, RunInputFile, StartedOut, UploadIn, UploadOut,
    UploadOutState, fill, paths,
};
use sha2::{Digest, Sha256};
use ureq::Agent;

use crate::api::{self, Api, Error};

/// How long staging or uploading may take, all told. Heartbeats keep the lease meanwhile.
const TRANSFERS_FOR: Duration = Duration::from_secs(3600);

/// The job a transfer is for.
pub struct Lease<'a> {
    pub api: &'a Api,
    pub job: &'a str,
    pub token: &'a str,
}

/// Why a transfer failed: the lease is gone, or the job can't go on.
#[derive(Debug)]
pub enum Failed {
    Lost(Error),
    Job(String),
}

fn failed(what: &str, error: Error) -> Failed {
    match error {
        Error::Stale(_) | Error::Unauthorized(_) => Failed::Lost(error),
        error => Failed::Job(format!("{what}: {error}")),
    }
}

/// HTTP for object storage: no token, and no overall timeout, since files can be large,
/// but a stalled connection still fails.
pub fn storage_agent() -> Agent {
    api::agent_config()
        .timeout_global(None)
        .timeout_recv_body(None)
        .timeout_send_body(None)
        .timeout_per_read(Some(Duration::from_secs(120)))
        .timeout_per_write(Some(Duration::from_secs(120)))
        .build()
        .new_agent()
}

/// A file name that stays one path component, whatever the name it was added under.
fn safe_name(name: &str) -> String {
    let cleaned: String = name
        .chars()
        .map(|c| {
            if c == '/' || c == '\\' || c.is_control() {
                '_'
            } else {
                c
            }
        })
        .collect();
    match cleaned.trim() {
        "" | "." | ".." => "file".to_owned(),
        trimmed => trimmed.to_owned(),
    }
}

fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    bytes
        .iter()
        .fold(String::with_capacity(bytes.len() * 2), |mut out, b| {
            let _ = write!(out, "{b:02x}");
            out
        })
}

/// Size and SHA-256 of a file, reading it once.
pub fn digest(path: &Path) -> std::io::Result<(u64, String)> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0; 1 << 20];
    let mut size = 0u64;
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            return Ok((size, hex(&hasher.finalize())));
        }
        hasher.update(&buffer[..read]);
        size += read as u64;
    }
}

/// Downloads every input file into `dir`, checked, and returns the run input's `files`
/// with each one's `path`.
pub fn stage(
    lease: &Lease,
    storage: &Agent,
    files: &BTreeMap<String, RunInputFile>,
    dir: &Path,
) -> Result<BTreeMap<String, RunInputFile>, Failed> {
    let deadline = Instant::now() + TRANSFERS_FOR;
    let mut staged = BTreeMap::new();
    for (id, file) in files {
        let target = dir.join(safe_name(id)).join(safe_name(&file.name));
        let body = LeaseIn {
            lease_token: lease.token.to_owned(),
        };
        let path = fill(paths::JOBS_FILES, &[lease.job, id]);
        let fetched = api::retrying(deadline, || {
            let signed = lease
                .api
                .post::<_, DownloadOut>(&path, &body)?
                .ok_or_else(|| Error::Transient("The server's answer was empty.".into()))?;
            fetch(storage, &signed.url, &target, file)
        });
        if let Err(error) = fetched {
            let _ = std::fs::remove_file(&target);
            return Err(failed(&format!("Couldn't download {}", file.name), error));
        }
        let mut file = file.clone();
        file.path = Some(target.to_string_lossy().into_owned());
        staged.insert(id.clone(), file);
    }
    Ok(staged)
}

/// GETs `url` into `target`, checking it's the file `expected` describes.
fn fetch(storage: &Agent, url: &str, target: &Path, expected: &RunInputFile) -> Result<(), Error> {
    let reply = storage
        .get(url)
        .call()
        .map_err(|e| Error::Transient(e.to_string()))?;
    let status = reply.status().as_u16();
    if status != 200 {
        // Expired (403) or the store's trouble: sign it again and retry.
        return Err(Error::Transient(format!(
            "Object storage answered {status}."
        )));
    }
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent).map_err(|e| Error::Transient(e.to_string()))?;
    }
    let size = u64::try_from(expected.size).unwrap_or_default();
    let mut body = reply
        .into_body()
        .into_with_config()
        .limit(size + 1)
        .reader();
    let mut out = File::create(target).map_err(|e| Error::Transient(e.to_string()))?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0; 1 << 20];
    let mut got = 0u64;
    loop {
        let read = match body.read(&mut buffer) {
            Ok(0) => break,
            Ok(read) => read,
            // Past the limit: more bytes than the file has.
            Err(_) if got >= size => break,
            Err(error) => return Err(Error::Transient(format!("The download broke off: {error}"))),
        };
        hasher.update(&buffer[..read]);
        out.write_all(&buffer[..read])
            .map_err(|e| Error::Transient(e.to_string()))?;
        got += read as u64;
        if got > size {
            break;
        }
    }
    out.sync_all()
        .map_err(|e| Error::Transient(e.to_string()))?;
    let sha256 = hex(&hasher.finalize());
    if got != size || sha256 != expected.sha256 {
        // Not worth trying again: what's stored isn't what Iki recorded.
        return Err(Error::Refused {
            status: 0,
            code: "checksum_mismatch".into(),
            detail: format!(
                "it came to {got} bytes with SHA-256 {sha256}, not the {size} bytes and \
                 {} Iki recorded",
                expected.sha256
            ),
            body: serde_json::Value::Null,
        });
    }
    Ok(())
}

/// A media type from a file's extension, for `$file`s that don't give one.
fn guess_media_type(path: &Path) -> &'static str {
    let extension = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or_default();
    match extension.to_ascii_lowercase().as_str() {
        "pdb" | "ent" => "chemical/x-pdb",
        "cif" | "mmcif" => "chemical/x-mmcif",
        "sdf" | "mol" => "chemical/x-mdl-sdfile",
        "fasta" | "fa" | "faa" | "fna" => "text/x-fasta",
        "csv" => "text/csv",
        "tsv" => "text/tab-separated-values",
        "json" => "application/json",
        "txt" | "log" => "text/plain",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "tif" | "tiff" => "image/tiff",
        "pdf" => "application/pdf",
        "gz" => "application/gzip",
        "zip" => "application/zip",
        "h5" | "hdf5" => "application/x-hdf5",
        "parquet" => "application/vnd.apache.parquet",
        _ => "application/octet-stream",
    }
}

/// A `$file` reference in output.json, once checked.
struct Made {
    path: PathBuf,
    name: String,
    media_type: String,
}

/// What a `{"$file": …}` value names, if it's a file in `out_dir`.
fn resolve(
    value: &serde_json::Map<String, serde_json::Value>,
    out_dir: &Path,
) -> Result<Made, String> {
    let given = value
        .get("$file")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    let outside =
        || format!("output.json names the file {given:?}, which isn't a file in IKI_OUT_DIR.");
    let root = out_dir.canonicalize().map_err(|_| outside())?;
    let path = root.join(given).canonicalize().map_err(|_| outside())?;
    // Canonical paths resolve `..` and symlinks, so this can't reach outside.
    if !path.starts_with(&root) || !path.is_file() {
        return Err(outside());
    }
    let text = |key: &str| {
        value
            .get(key)
            .and_then(serde_json::Value::as_str)
            .map(str::to_owned)
    };
    let name = text("name")
        .or_else(|| path.file_name().map(|n| n.to_string_lossy().into_owned()))
        .unwrap_or_else(|| "file".to_owned());
    let media_type = text("media_type").unwrap_or_else(|| guess_media_type(&path).to_owned());
    Ok(Made {
        path,
        name,
        media_type,
    })
}

fn is_file_ref(value: &serde_json::Value) -> bool {
    value
        .as_object()
        .is_some_and(|object| object.contains_key("$file"))
}

/// Each field-value map in output.json: `updates.<input>[].values` and `creates[].values`.
fn value_maps(
    output: &mut serde_json::Map<String, serde_json::Value>,
) -> Vec<&mut serde_json::Value> {
    let mut found = Vec::new();
    for (key, section) in output.iter_mut() {
        match (key.as_str(), section) {
            ("updates", serde_json::Value::Object(updates)) => {
                for given in updates
                    .values_mut()
                    .filter_map(serde_json::Value::as_array_mut)
                {
                    found.extend(given.iter_mut().filter_map(|u| u.get_mut("values")));
                }
            }
            ("creates", serde_json::Value::Array(creates)) => {
                found.extend(creates.iter_mut().filter_map(|c| c.get_mut("values")));
            }
            _ => {}
        }
    }
    found
}

/// Uploads every `$file` in `output` (a field's value, or an item of a list field's), and
/// puts each one's file ID in its place. Returns how many files went.
pub fn upload_results(
    lease: &Lease,
    storage: &Agent,
    out_dir: &Path,
    output: &mut serde_json::Map<String, serde_json::Value>,
) -> Result<usize, Failed> {
    let deadline = Instant::now() + TRANSFERS_FOR;
    let mut uploaded: BTreeMap<PathBuf, String> = BTreeMap::new();
    for values in value_maps(output) {
        let Some(values) = values.as_object_mut() else {
            continue;
        };
        for value in values.values_mut() {
            let slots: Vec<&mut serde_json::Value> = match value {
                serde_json::Value::Array(items) => {
                    items.iter_mut().filter(|v| is_file_ref(v)).collect()
                }
                single if is_file_ref(single) => vec![single],
                _ => continue,
            };
            for slot in slots {
                let reference = slot.as_object().cloned().unwrap_or_default();
                let made = resolve(&reference, out_dir).map_err(Failed::Job)?;
                let id = if let Some(id) = uploaded.get(&made.path) {
                    id.clone()
                } else {
                    let id = upload(lease, storage, &made, deadline)?;
                    uploaded.insert(made.path.clone(), id.clone());
                    id
                };
                *slot = serde_json::Value::String(id);
            }
        }
    }
    Ok(uploaded.len())
}

/// Uploads one file: start, PUT each part, then finish until the server has checked it.
fn upload(
    lease: &Lease,
    storage: &Agent,
    made: &Made,
    deadline: Instant,
) -> Result<String, Failed> {
    let what = format!("Couldn't upload {}", made.name);
    let (size, sha256) = digest(&made.path).map_err(|e| Failed::Job(format!("{what}: {e}")))?;
    let body = UploadIn {
        lease_token: lease.token.to_owned(),
        name: made.name.clone(),
        media_type: made.media_type.clone(),
        size: i64::try_from(size).unwrap_or(i64::MAX),
        sha256,
    };
    let started = api::retrying(deadline, || {
        lease
            .api
            .post::<_, StartedOut>(&fill(paths::JOBS_UPLOADS, &[lease.job]), &body)?
            .ok_or_else(|| Error::Transient("The server's answer was empty.".into()))
    })
    .map_err(|e| failed(&what, e))?;
    let mut parts = started.parts.clone();
    parts.sort_by_key(|p| p.number);
    let mut offset = 0u64;
    for part in &parts {
        put_part(
            lease,
            storage,
            &started.id,
            &made.path,
            offset,
            part,
            deadline,
        )
        .map_err(|e| failed(&what, e))?;
        offset += u64::try_from(part.size).unwrap_or_default();
    }
    if started.state == iki_protocol::StartedOutState::Ready
        && let Some(file) = started.file
    {
        return Ok(file);
    }
    let finish = fill(paths::JOBS_UPLOADS_COMPLETE, &[lease.job, &started.id]);
    let lease_body = LeaseIn {
        lease_token: lease.token.to_owned(),
    };
    let mut attempt = 0;
    loop {
        attempt += 1;
        let state = api::retrying(deadline, || {
            lease
                .api
                .post::<_, UploadOut>(&finish, &lease_body)?
                .ok_or_else(|| Error::Transient("The server's answer was empty.".into()))
        })
        .map_err(|e| failed(&what, e))?;
        match (state.state, state.file) {
            (UploadOutState::Ready, Some(file)) => return Ok(file),
            (UploadOutState::Pending | UploadOutState::Checking | UploadOutState::Ready, _)
                if Instant::now() < deadline =>
            {
                // Iki is reading the bytes back to check them.
                thread::sleep(api::backoff(attempt.min(3)));
            }
            _ => {
                let why = if state.error.is_empty() {
                    "the server didn't accept it".to_owned()
                } else {
                    state.error
                };
                return Err(Failed::Job(format!("{what}: {why}")));
            }
        }
    }
}

/// PUTs one part's bytes, signing its URL again if it ran out.
fn put_part(
    lease: &Lease,
    storage: &Agent,
    upload_id: &str,
    path: &Path,
    offset: u64,
    part: &PartOut,
    deadline: Instant,
) -> Result<(), Error> {
    let size = usize::try_from(part.size).unwrap_or_default();
    let mut bytes = vec![0; size];
    let mut file = File::open(path).map_err(|e| Error::Transient(e.to_string()))?;
    file.seek(SeekFrom::Start(offset))
        .and_then(|_| file.read_exact(&mut bytes))
        .map_err(|e| Error::Refused {
            status: 0,
            code: "file_changed".into(),
            detail: format!("it changed while it was being uploaded ({e})"),
            body: serde_json::Value::Null,
        })?;
    let mut url = part.url.clone();
    api::retrying(deadline, || {
        let reply = storage
            .put(&url)
            .send(&bytes[..])
            .map_err(|e| Error::Transient(e.to_string()))?;
        match reply.status().as_u16() {
            200..=299 => Ok(()),
            403 => {
                // The URL ran out (they last an hour): sign this part again.
                let body = PartsIn {
                    lease_token: lease.token.to_owned(),
                    numbers: Some(vec![part.number]),
                };
                let again = lease
                    .api
                    .post::<_, Vec<PartOut>>(
                        &fill(paths::JOBS_UPLOADS_PARTS, &[lease.job, upload_id]),
                        &body,
                    )?
                    .unwrap_or_default();
                if let Some(fresh) = again.into_iter().find(|p| p.number == part.number) {
                    url = fresh.url;
                }
                Err(Error::Transient(
                    "A part's URL ran out; signed it again.".into(),
                ))
            }
            status => Err(Error::Transient(format!(
                "Object storage answered {status}."
            ))),
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("iki-files-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn names_stay_one_path_component() {
        assert_eq!(safe_name("model_1.pdb"), "model_1.pdb");
        assert_eq!(safe_name("../../etc/passwd"), ".._.._etc_passwd");
        assert_eq!(safe_name(".."), "file");
        assert_eq!(safe_name(""), "file");
        assert_eq!(safe_name("a\\b\nc"), "a_b_c");
    }

    #[test]
    fn digests_files() {
        let dir = scratch("digest");
        let path = dir.join("x");
        std::fs::write(&path, b"abc").unwrap();
        assert_eq!(
            digest(&path).unwrap(),
            (
                3,
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad".to_owned()
            )
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn file_references_must_stay_in_the_out_dir() {
        let dir = scratch("resolve");
        let out = dir.join("out");
        std::fs::create_dir_all(out.join("sub")).unwrap();
        std::fs::write(out.join("sub/model.pdb"), b"x").unwrap();
        std::fs::write(dir.join("secret"), b"x").unwrap();
        let reference = |path: &str| {
            serde_json::json!({"$file": path})
                .as_object()
                .cloned()
                .unwrap()
        };
        let made = resolve(&reference("sub/model.pdb"), &out).unwrap();
        assert_eq!(
            (made.name.as_str(), made.media_type.as_str()),
            ("model.pdb", "chemical/x-pdb")
        );
        assert!(resolve(&reference("../secret"), &out).is_err());
        assert!(resolve(&reference("sub"), &out).is_err());
        assert!(resolve(&reference("missing.pdb"), &out).is_err());
        assert!(resolve(&reference(dir.join("secret").to_str().unwrap()), &out).is_err());
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(dir.join("secret"), out.join("link")).unwrap();
            assert!(resolve(&reference("link"), &out).is_err());
        }
        let named = serde_json::json!({"$file": "sub/model.pdb", "name": "a.cif", "media_type": "chemical/x-mmcif"});
        let made = resolve(named.as_object().unwrap(), &out).unwrap();
        assert_eq!(
            (made.name.as_str(), made.media_type.as_str()),
            ("a.cif", "chemical/x-mmcif")
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn finds_file_references_where_field_values_go() {
        let mut output = serde_json::json!({
            "updates": {"protein": [{"entity": "e", "values": {"report": {"$file": "a"}}}]},
            "creates": [{"type": "t", "values": {"files": [{"$file": "b"}, "existing-id"]}}],
            "metrics": {"x": 1},
        });
        let output = output.as_object_mut().unwrap();
        let maps = value_maps(output);
        assert_eq!(maps.len(), 2);
        let (creates, updates) = (&maps[0], &maps[1]);
        assert!(is_file_ref(&updates["report"]));
        assert!(is_file_ref(&creates["files"][0]));
        assert!(!is_file_ref(&creates["files"][1]));
    }
}
