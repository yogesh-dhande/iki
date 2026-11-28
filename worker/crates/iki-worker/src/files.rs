//! Files in and out of a job.
//!
//! - **Inputs**: before the command starts, every file its inputs hold (the run input's
//!   `files`) is downloaded into the job's directory, its size and SHA-256 checked, and
//!   its `path` added to `input.json`.
//! - **Artifacts**: once the command ends, each file in `IKI_OUT_DIR` one of the action's
//!   `artifacts.keep` patterns matches is uploaded as an artifact (ADR 0068).
//! - **Results**: where `output.json` gives a file field `{"$file": "model.pdb"}` (a path
//!   in `IKI_OUT_DIR`, optionally with `media_type` and `name`), the file is uploaded in
//!   parts, checked by the server, and replaced with its file ID.
//!
//! A local run (`run --local`) copies inputs from the paths input.json gives instead,
//! and checks results the same way but describes them rather than uploading them.
//!
//! Object storage URLs are signed for this one transfer, so they get no `Authorization`
//! header: the worker's token never leaves for anywhere but the server.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{Read, Write};
use std::os::unix::fs::FileExt;
use std::path::{Component, Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

use iki_protocol::{
    ArtifactIn, CheckpointDownloadOut, CheckpointIn, DownloadOut, LeaseIn, PartOut, PartsIn,
    RunInputFile, RunInputJobCheckpointSaved, StartedOut, UploadIn, UploadOut, UploadOutState,
    fill, paths,
};
use sha2::{Digest, Sha256};
use ureq::Agent;

use crate::api::{self, Api, Error};
use crate::{checkpoint, globs};

/// Reads a command-written file without opening a link, device, or blocking pipe.
pub fn open_regular(path: &Path) -> std::io::Result<File> {
    use rustix::fs::{Mode, OFlags, open};
    let file = File::from(open(
        path,
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::NONBLOCK,
        Mode::empty(),
    )?);
    if !file.metadata()?.is_file() {
        return Err(std::io::Error::other("expected a regular file"));
    }
    Ok(file)
}

/// Opens the directory `relative` in `root`, each step from the one before and none
/// through a link. A command, or a process it left running, can rename what's in its
/// directories while the worker reads them; it can't make the worker read anything else.
pub fn open_dir_in(root: &Path, relative: &Path) -> std::io::Result<File> {
    use rustix::fs::{Mode, OFlags, open, openat};
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    let mut dir = open(root, flags, Mode::empty())?;
    for component in relative.components() {
        let Component::Normal(name) = component else {
            return Err(std::io::ErrorKind::InvalidInput.into());
        };
        dir = openat(&dir, name, flags, Mode::empty())?;
    }
    Ok(File::from(dir))
}

/// Opens the regular file `relative` in `root`, none of the way through a link (see
/// [`open_dir_in`]). Everything the worker reads of a command's file comes from this one
/// descriptor, so what it hashes is what it sends.
pub fn open_in(root: &Path, relative: &Path) -> std::io::Result<File> {
    use rustix::fs::{Mode, OFlags, openat};
    let parent = open_dir_in(root, relative.parent().unwrap_or(Path::new("")))?;
    let Some(Component::Normal(name)) = relative.components().next_back() else {
        return Err(std::io::ErrorKind::InvalidInput.into());
    };
    let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC;
    let file = File::from(openat(&parent, name, flags, Mode::empty())?);
    if !file.metadata()?.is_file() {
        return Err(std::io::Error::other("expected a regular file"));
    }
    Ok(file)
}

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

pub fn hex(bytes: &[u8]) -> String {
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
    digest_file(&File::open(path)?)
}

/// Size and SHA-256 of an open file, from its start, wherever its offset is.
pub fn digest_file(file: &File) -> std::io::Result<(u64, String)> {
    let mut hasher = Sha256::new();
    let mut buffer = vec![0; 1 << 20];
    let mut size = 0u64;
    loop {
        let read = match file.read_at(&mut buffer, size) {
            Ok(read) => read,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        };
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
            fetch(storage, &signed.url, &target, file.size, &file.sha256)
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

/// GETs `url` into `target`, checking it has the size and SHA-256 Iki recorded.
fn fetch(
    storage: &Agent,
    url: &str,
    target: &Path,
    expected_size: i64,
    expected_sha256: &str,
) -> Result<(), Error> {
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
    let size = u64::try_from(expected_size).unwrap_or_default();
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
    if got != size || sha256 != expected_sha256 {
        // Not worth trying again: what's stored isn't what Iki recorded.
        return Err(Error::Refused {
            status: 0,
            code: "checksum_mismatch".into(),
            detail: format!(
                "it came to {got} bytes with SHA-256 {sha256}, not the {size} bytes and \
                 {expected_sha256} Iki recorded"
            ),
            body: serde_json::Value::Null,
        });
    }
    Ok(())
}

const OCTET_STREAM: &str = "application/octet-stream";

/// Offline jobs use the generated core registry. Leased jobs use the deployment's registry
/// when the server checks the uploaded bytes, including formats installed by plugins.
fn local_media_type(name: &str) -> &'static str {
    let lower = name
        .rsplit('/')
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase();
    iki_protocol::CORE_FILE_EXTENSIONS
        .iter()
        .find(|(suffix, _)| lower.len() > suffix.len() && lower.ends_with(*suffix))
        .map_or(OCTET_STREAM, |(_, media_type)| media_type)
}

/// A `$file` reference in output.json, once checked: the file it names, open.
struct Made {
    file: File,
    /// Its path in `IKI_OUT_DIR`.
    relative: PathBuf,
    name: String,
    media_type: Option<String>,
}

/// Where in `out_dir` a `$file` path is: relative, or absolute under `out_dir`, with no
/// `..`. `None` for anywhere else.
fn inside(given: &str, out_dir: &Path) -> Option<PathBuf> {
    let given = Path::new(given);
    let given = if given.is_absolute() {
        let root = out_dir.canonicalize().ok();
        given
            .strip_prefix(out_dir)
            .ok()
            .or_else(|| given.strip_prefix(root.as_deref()?).ok())?
            .to_path_buf()
    } else {
        given.to_path_buf()
    };
    let mut relative = PathBuf::new();
    for component in given.components() {
        match component {
            Component::Normal(part) => relative.push(part),
            Component::CurDir => {}
            _ => return None,
        }
    }
    (!relative.as_os_str().is_empty()).then_some(relative)
}

/// What a `{"$file": …}` value names, if it's a regular file in `out_dir`, opened without
/// following a link anywhere along its path.
fn resolve(
    value: &serde_json::Map<String, serde_json::Value>,
    out_dir: &Path,
) -> Result<Made, String> {
    let given = value
        .get("$file")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    let outside = || {
        format!(
            "output.json names the file {given:?}, which isn't a regular file in IKI_OUT_DIR \
             (links aren't followed)."
        )
    };
    let relative = inside(given, out_dir).ok_or_else(outside)?;
    let file = open_in(out_dir, &relative).map_err(|_| outside())?;
    let text = |key: &str| {
        value
            .get(key)
            .and_then(serde_json::Value::as_str)
            .map(str::to_owned)
    };
    let name = text("name")
        .or_else(|| {
            relative
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
        })
        .unwrap_or_else(|| "file".to_owned());
    let media_type = text("media_type");
    Ok(Made {
        file,
        relative,
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

/// Puts what `place` makes of each `$file` in `output` (a field's value, an item of a
/// list field's, or `creates_file`) in its place, once per file. Returns how many files
/// there were.
fn replace_file_refs(
    out_dir: &Path,
    output: &mut serde_json::Map<String, serde_json::Value>,
    mut place: impl FnMut(&Made) -> Result<serde_json::Value, Failed>,
) -> Result<usize, Failed> {
    let mut placed: BTreeMap<PathBuf, serde_json::Value> = BTreeMap::new();
    let mut put = |slot: &mut serde_json::Value| -> Result<(), Failed> {
        let reference = slot.as_object().cloned().unwrap_or_default();
        let made = resolve(&reference, out_dir).map_err(Failed::Job)?;
        let value = if let Some(value) = placed.get(&made.relative) {
            value.clone()
        } else {
            let value = place(&made)?;
            placed.insert(made.relative.clone(), value.clone());
            value
        };
        *slot = value;
        Ok(())
    };
    // Creates too many for output.json, one per line of a file (ADR 0056).
    if let Some(slot) = output.get_mut("creates_file").filter(|v| is_file_ref(v)) {
        put(slot)?;
    }
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
                put(slot)?;
            }
        }
    }
    Ok(placed.len())
}

/// Uploads every `$file` in `output`, and puts each one's file ID in its place. Returns
/// how many files went.
pub fn upload_results(
    lease: &Lease,
    storage: &Agent,
    out_dir: &Path,
    output: &mut serde_json::Map<String, serde_json::Value>,
) -> Result<usize, Failed> {
    let deadline = Instant::now() + TRANSFERS_FOR;
    replace_file_refs(out_dir, output, |made| {
        upload(lease, storage, made, deadline).map(serde_json::Value::String)
    })
}

/// For a local run: checks every `$file` in `output` as an upload would, and describes
/// each one in its place, with its size, SHA-256 and where it is.
pub fn describe_results(
    out_dir: &Path,
    output: &mut serde_json::Map<String, serde_json::Value>,
) -> Result<usize, Failed> {
    replace_file_refs(out_dir, output, |made| {
        let shown = made.relative.to_string_lossy();
        let (size, sha256) = digest_file(&made.file)
            .map_err(|e| Failed::Job(format!("Couldn't read {shown}: {e}.")))?;
        Ok(serde_json::json!({
            "$file": shown,
            "name": made.name,
            "media_type": made.media_type.as_deref().unwrap_or_else(|| local_media_type(&made.name)),
            "size": size,
            "sha256": sha256,
            "path": out_dir.join(&made.relative).to_string_lossy(),
        }))
    })
}

/// For a local run: fills in what an input file's entry leaves out from the file at its
/// `path` (relative to `base`), and makes that path absolute.
pub fn describe_local(
    entry: &mut serde_json::Map<String, serde_json::Value>,
    base: &Path,
) -> Result<(), String> {
    let given = entry
        .get("path")
        .and_then(serde_json::Value::as_str)
        .map(str::to_owned)
        .ok_or("give its path, relative to input.json.")?;
    let source = base
        .join(&given)
        .canonicalize()
        .ok()
        .filter(|p| p.is_file())
        .ok_or_else(|| format!("there's no file at {given}."))?;
    let name = source
        .file_name()
        .map_or_else(|| "file".to_owned(), |n| n.to_string_lossy().into_owned());
    let name = entry.entry("name").or_insert_with(|| name.into());
    let inferred = local_media_type(name.as_str().unwrap_or_default());
    entry.entry("media_type").or_insert_with(|| inferred.into());
    if !entry.contains_key("size") || !entry.contains_key("sha256") {
        let (size, sha256) = digest(&source).map_err(|e| format!("couldn't read {given}: {e}."))?;
        entry.entry("size").or_insert_with(|| size.into());
        entry.entry("sha256").or_insert_with(|| sha256.into());
    }
    entry.insert("path".into(), source.to_string_lossy().into_owned().into());
    Ok(())
}

/// For a local run: copies every input file from its `path` into `dir`, laid out as
/// downloads are, checks each against its entry, and returns them with their new paths.
pub fn copy_in(
    files: &BTreeMap<String, RunInputFile>,
    dir: &Path,
) -> Result<BTreeMap<String, RunInputFile>, String> {
    let mut staged = BTreeMap::new();
    for (id, file) in files {
        let source = file.path.as_deref().unwrap_or_default();
        let target = dir.join(safe_name(id)).join(safe_name(&file.name));
        let copied = target
            .parent()
            .map_or(Ok(()), std::fs::create_dir_all)
            .and_then(|()| std::fs::copy(source, &target))
            .and_then(|_| digest(&target));
        let (size, sha256) =
            copied.map_err(|e| format!("Couldn't copy {} from {source}: {e}.", file.name))?;
        if u64::try_from(file.size).ok() != Some(size) {
            return Err(format!(
                "{} is {size} bytes, not the {} input.json gives.",
                file.name, file.size
            ));
        }
        if sha256 != file.sha256 {
            return Err(format!(
                "{}'s SHA-256 is {sha256}, not the {} input.json gives.",
                file.name, file.sha256
            ));
        }
        let mut file = file.clone();
        file.path = Some(target.to_string_lossy().into_owned());
        staged.insert(id.clone(), file);
    }
    Ok(staged)
}

/// Uploads one result file: start, PUT each part, then finish until the server has
/// checked it. Returns its file's ID.
fn upload(
    lease: &Lease,
    storage: &Agent,
    made: &Made,
    deadline: Instant,
) -> Result<String, Failed> {
    let what = format!("Couldn't upload {}", made.name);
    let (size, sha256) =
        digest_file(&made.file).map_err(|e| Failed::Job(format!("{what}: {e}")))?;
    let body = UploadIn {
        lease_token: lease.token.to_owned(),
        name: made.name.clone(),
        media_type: made
            .media_type
            .as_deref()
            .unwrap_or(OCTET_STREAM)
            .to_owned(),
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
    send(lease, storage, &made.file, &what, &started, deadline)?
        .ok_or_else(|| Failed::Job(format!("{what}: the server made no file of it")))
}

/// Sends a started upload's parts from `file`, then finishes it until the server has
/// checked it. Returns its file's ID; an artifact has none.
fn send(
    lease: &Lease,
    storage: &Agent,
    file: &File,
    what: &str,
    started: &StartedOut,
    deadline: Instant,
) -> Result<Option<String>, Failed> {
    let mut parts = started.parts.clone();
    parts.sort_by_key(|p| p.number);
    let mut offset = 0u64;
    for part in &parts {
        put_part(lease, storage, &started.id, file, offset, part, deadline)
            .map_err(|e| failed(what, e))?;
        offset += u64::try_from(part.size).unwrap_or_default();
    }
    if started.state == iki_protocol::StartedOutState::Ready {
        return Ok(started.file.clone());
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
        .map_err(|e| failed(what, e))?;
        match state.state {
            UploadOutState::Ready => return Ok(state.file),
            UploadOutState::Pending | UploadOutState::Checking if Instant::now() < deadline => {
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

/// Artifacts one attempt keeps, at most, as the server allows.
pub const MAX_ARTIFACTS: usize = 1000;

/// The regular files in `out_dir` one of `patterns` matches, by their paths in it, sorted.
/// Symbolic links aren't followed or kept: an artifact is a file the command wrote. Each
/// directory is read from the one above it, never through a link. Also says how many
/// more there were than an attempt keeps.
pub fn find_artifacts(out_dir: &Path, patterns: &[String]) -> (Vec<String>, usize) {
    use rustix::fs::{AtFlags, Dir, FileType, statat};
    let mut found = Vec::new();
    let mut pending = vec![String::new()];
    while let Some(prefix) = pending.pop() {
        let Ok(dir) = open_dir_in(out_dir, Path::new(&prefix)) else {
            continue;
        };
        let Ok(entries) = Dir::read_from(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let name = entry.file_name();
            let Ok(text) = name.to_str() else {
                continue; // a name that isn't UTF-8 can't be named in a pattern
            };
            if matches!(text, "." | "..") {
                continue;
            }
            let Ok(stat) = statat(&dir, name, AtFlags::SYMLINK_NOFOLLOW) else {
                continue;
            };
            let relative = if prefix.is_empty() {
                text.to_owned()
            } else {
                format!("{prefix}/{text}")
            };
            match FileType::from_raw_mode(stat.st_mode) {
                FileType::Directory => pending.push(relative),
                FileType::RegularFile if patterns.iter().any(|p| globs::matches(p, &relative)) => {
                    found.push(relative);
                }
                _ => {}
            }
        }
    }
    found.sort();
    let over = found.len().saturating_sub(MAX_ARTIFACTS);
    found.truncate(MAX_ARTIFACTS);
    (found, over)
}

/// Uploads one artifact: a file the command wrote that its action keeps, opened by
/// [`open_in`].
pub fn upload_artifact(
    lease: &Lease,
    storage: &Agent,
    relative: &str,
    file: &File,
) -> Result<(), Failed> {
    let deadline = Instant::now() + TRANSFERS_FOR;
    let what = format!("Couldn't keep {relative}");
    let (size, sha256) = digest_file(file).map_err(|e| Failed::Job(format!("{what}: {e}")))?;
    let body = ArtifactIn {
        lease_token: lease.token.to_owned(),
        path: relative.to_owned(),
        media_type: OCTET_STREAM.to_owned(),
        size: i64::try_from(size).unwrap_or(i64::MAX),
        sha256,
    };
    let started = api::retrying(deadline, || {
        lease
            .api
            .post::<_, StartedOut>(&fill(paths::JOBS_ARTIFACTS, &[lease.job]), &body)?
            .ok_or_else(|| Error::Transient("The server's answer was empty.".into()))
    })
    .map_err(|e| failed(&what, e))?;
    send(lease, storage, file, &what, &started, deadline).map(|_| ())
}

/// Uploads a packed checkpoint (ADR 0069), and finishes it until the server has checked it.
pub fn upload_checkpoint(
    lease: &Lease,
    storage: &Agent,
    packed: &checkpoint::Packed,
) -> Result<(), Failed> {
    let deadline = Instant::now() + TRANSFERS_FOR;
    let what = "Couldn't save a checkpoint";
    let file = open_regular(&packed.path).map_err(|e| Failed::Job(format!("{what}: {e}")))?;
    let body = CheckpointIn {
        lease_token: lease.token.to_owned(),
        size: i64::try_from(packed.size).unwrap_or(i64::MAX),
        sha256: packed.sha256.clone(),
    };
    let started = api::retrying(deadline, || {
        lease
            .api
            .post::<_, StartedOut>(&fill(paths::JOBS_CHECKPOINTS, &[lease.job]), &body)?
            .ok_or_else(|| Error::Transient("The server's answer was empty.".into()))
    })
    .map_err(|e| failed(what, e))?;
    send(lease, storage, &file, what, &started, deadline).map(|_| ())
}

/// Downloads the checkpoint an earlier attempt saved into `target`, checked.
pub fn download_checkpoint(
    lease: &Lease,
    storage: &Agent,
    saved: &RunInputJobCheckpointSaved,
    target: &Path,
) -> Result<(), Failed> {
    let deadline = Instant::now() + TRANSFERS_FOR;
    let body = LeaseIn {
        lease_token: lease.token.to_owned(),
    };
    let path = fill(paths::JOBS_CHECKPOINTS_DOWNLOAD, &[lease.job, &saved.id]);
    api::retrying(deadline, || {
        let signed = lease
            .api
            .post::<_, CheckpointDownloadOut>(&path, &body)?
            .ok_or_else(|| Error::Transient("The server's answer was empty.".into()))?;
        fetch(storage, &signed.url, target, signed.size, &signed.sha256)
    })
    .map_err(|e| failed("Couldn't download it", e))
}

/// Tells the server the attempt resumed from the checkpoint.
pub fn restored_checkpoint(lease: &Lease, id: &str) -> Result<(), Failed> {
    let deadline = Instant::now() + Duration::from_secs(120);
    let body = LeaseIn {
        lease_token: lease.token.to_owned(),
    };
    let path = fill(paths::JOBS_CHECKPOINTS_RESTORED, &[lease.job, id]);
    api::retrying(deadline, || {
        lease
            .api
            .post::<_, serde_json::Value>(&path, &body)
            .map(|_| ())
    })
    .map_err(|e| failed("Couldn't record it", e))
}

/// PUTs one part's bytes, read from `file` at `offset`, signing its URL again if it ran
/// out.
fn put_part(
    lease: &Lease,
    storage: &Agent,
    upload_id: &str,
    file: &File,
    offset: u64,
    part: &PartOut,
    deadline: Instant,
) -> Result<(), Error> {
    let size = usize::try_from(part.size).unwrap_or_default();
    let mut bytes = vec![0; size];
    file.read_exact_at(&mut bytes, offset)
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
    fn finds_the_artifacts_the_patterns_match_and_no_links() {
        let dir = scratch("artifacts");
        std::fs::create_dir_all(dir.join("msas/deep")).unwrap();
        for name in [
            "alignment.txt",
            "result.json",
            "msas/a.a3m",
            "msas/deep/b.a3m",
        ] {
            std::fs::write(dir.join(name), b"x").unwrap();
        }
        std::os::unix::fs::symlink("/etc/passwd", dir.join("msas/passwd")).unwrap();
        let patterns = ["alignment.*".to_owned(), "msas/**".to_owned()];
        // A linked directory isn't walked either.
        std::os::unix::fs::symlink(dir.join("msas"), dir.join("msas/again")).unwrap();
        let (found, over) = find_artifacts(&dir, &patterns);
        assert_eq!(found, ["alignment.txt", "msas/a.a3m", "msas/deep/b.a3m"]);
        assert_eq!(over, 0);
    }

    #[test]
    fn keeps_at_most_an_attempts_share_of_artifacts() {
        let dir = scratch("too-many");
        for n in 0..(MAX_ARTIFACTS + 3) {
            std::fs::write(dir.join(format!("{n:05}.log")), b"").unwrap();
        }
        let (found, over) = find_artifacts(&dir, &["*.log".to_owned()]);
        assert_eq!((found.len(), over), (MAX_ARTIFACTS, 3));
        assert_eq!(found[0], "00000.log");
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
            (made.name.as_str(), made.media_type.as_deref()),
            ("model.pdb", None)
        );
        assert!(resolve(&reference("../secret"), &out).is_err());
        assert!(resolve(&reference("sub"), &out).is_err());
        assert!(resolve(&reference("missing.pdb"), &out).is_err());
        assert!(resolve(&reference(dir.join("secret").to_str().unwrap()), &out).is_err());
        assert!(resolve(&reference("sub/../sub/model.pdb"), &out).is_err());
        // A link isn't followed, even to a file in the out dir, nor a linked directory.
        std::os::unix::fs::symlink(dir.join("secret"), out.join("link")).unwrap();
        assert!(resolve(&reference("link"), &out).is_err());
        std::os::unix::fs::symlink(out.join("sub/model.pdb"), out.join("alias.pdb")).unwrap();
        assert!(resolve(&reference("alias.pdb"), &out).is_err());
        std::os::unix::fs::symlink(out.join("sub"), out.join("linked")).unwrap();
        assert!(resolve(&reference("linked/model.pdb"), &out).is_err());
        // An absolute path in the out dir is the same file.
        let absolute = out.join("sub/model.pdb");
        let made = resolve(&reference(absolute.to_str().unwrap()), &out).unwrap();
        assert_eq!(made.relative, Path::new("sub/model.pdb"));
        let made = resolve(&reference("./sub/model.pdb"), &out).unwrap();
        assert_eq!(made.relative, Path::new("sub/model.pdb"));
        let named = serde_json::json!({"$file": "sub/model.pdb", "name": "a.cif", "media_type": "chemical/x-mmcif"});
        let made = resolve(named.as_object().unwrap(), &out).unwrap();
        assert_eq!(
            (made.name.as_str(), made.media_type.as_deref()),
            ("a.cif", Some("chemical/x-mmcif"))
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn offline_files_follow_the_core_format_declarations() {
        assert_eq!(local_media_type("ligand.MOL"), "chemical/x-mdl-molfile");
        assert_eq!(local_media_type("library.sdf"), "chemical/x-mdl-sdfile");
        assert_eq!(local_media_type("sequences.fasta"), "chemical/seq-na-fasta");
        assert_eq!(local_media_type("sequences.faa"), "chemical/seq-aa-fasta");
        assert_eq!(local_media_type("reads.FASTQ.GZ"), "application/gzip");
        assert_eq!(local_media_type(".mol"), OCTET_STREAM);
        assert_eq!(local_media_type("unknown"), OCTET_STREAM);
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

    #[test]
    fn replaces_a_creates_file_once_with_the_rest() {
        let dir = scratch("creates-file");
        let out = dir.join("out");
        std::fs::create_dir_all(&out).unwrap();
        std::fs::write(out.join("creates.ndjson"), "{}\n").unwrap();
        let mut output = serde_json::json!({
            "creates_file": {"$file": "creates.ndjson"},
            "creates": [{"type": "t", "values": {"log": {"$file": "creates.ndjson"}}}],
        });
        let mut placed = 0;
        let count = replace_file_refs(&out, output.as_object_mut().unwrap(), |made| {
            placed += 1;
            Ok(serde_json::Value::String(
                made.relative.to_string_lossy().into_owned(),
            ))
        })
        .unwrap();
        assert_eq!((count, placed), (1, 1));
        assert_eq!(output["creates_file"], "creates.ndjson");
        assert_eq!(output["creates"][0]["values"]["log"], "creates.ndjson");
        let mut outside = serde_json::json!({"creates_file": {"$file": "../input.json"}});
        assert!(
            replace_file_refs(&out, outside.as_object_mut().unwrap(), |_| Ok(
                serde_json::Value::Null
            ))
            .is_err()
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// A file only the worker should read: on Linux, the worker's own environment, which
    /// holds its tokens; elsewhere, a stand-in.
    fn the_workers_secret(dir: &Path) -> PathBuf {
        if cfg!(target_os = "linux") {
            PathBuf::from("/proc/self/environ")
        } else {
            let secret = dir.join("secret");
            std::fs::write(&secret, b"IKI_REGISTRATION_TOKEN=iki_wreg_x").unwrap();
            secret
        }
    }

    fn swap_for_a_link(path: &Path, target: &Path) {
        let partial = path.with_extension("partial");
        std::os::unix::fs::symlink(target, &partial).unwrap();
        std::fs::rename(&partial, path).unwrap();
    }

    #[test]
    fn an_artifact_swapped_for_a_link_after_it_was_listed_is_not_read() {
        let dir = scratch("artifact-swap");
        let out = dir.join("out");
        std::fs::create_dir_all(out.join("logs")).unwrap();
        std::fs::write(out.join("a.bin"), b"large").unwrap();
        std::fs::write(out.join("z.txt"), b"the command's").unwrap();
        std::fs::write(out.join("logs/run.log"), b"the command's").unwrap();
        let secret = the_workers_secret(&dir);
        let (found, _) = find_artifacts(&out, &["**".to_owned()]);
        assert_eq!(found, ["a.bin", "logs/run.log", "z.txt"]);
        // A process the command left running swaps a listed file, and a listed file's
        // directory, for links to what only the worker can read.
        swap_for_a_link(&out.join("z.txt"), &secret);
        let elsewhere = dir.join("elsewhere");
        std::fs::create_dir_all(&elsewhere).unwrap();
        std::fs::write(elsewhere.join("run.log"), b"not the command's").unwrap();
        std::fs::rename(out.join("logs"), dir.join("logs-moved")).unwrap();
        std::os::unix::fs::symlink(&elsewhere, out.join("logs")).unwrap();
        assert!(open_in(&out, Path::new("z.txt")).is_err());
        assert!(open_in(&out, Path::new("logs/run.log")).is_err());
        assert!(open_in(&out, Path::new("a.bin")).is_ok());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn what_is_hashed_and_sent_is_the_file_the_command_wrote() {
        let dir = scratch("one-descriptor");
        let out = dir.join("out");
        std::fs::create_dir_all(&out).unwrap();
        std::fs::write(out.join("model.pdb"), b"ATOM 1").unwrap();
        let secret = the_workers_secret(&dir);
        let reference = serde_json::json!({"$file": "model.pdb"});
        let made = resolve(reference.as_object().unwrap(), &out).unwrap();
        // Swapped once it was checked: the upload still reads the file it opened.
        swap_for_a_link(&out.join("model.pdb"), &secret);
        assert_eq!(
            digest_file(&made.file).unwrap(),
            (6, hex(&Sha256::digest(b"ATOM 1")))
        );
        let mut part = vec![0; 4];
        made.file.read_exact_at(&mut part, 2).unwrap();
        assert_eq!(part, b"OM 1");
        // And named again, it's refused.
        assert!(resolve(reference.as_object().unwrap(), &out).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn only_regular_files_are_outputs() {
        let dir = scratch("regular");
        let out = dir.join("out");
        std::fs::create_dir_all(&out).unwrap();
        let made = std::process::Command::new("mkfifo")
            .arg(out.join("pipe"))
            .status()
            .unwrap();
        assert!(made.success());
        let (found, _) = find_artifacts(&out, &["*".to_owned()]);
        assert!(found.is_empty());
        // Opening a pipe nobody writes to doesn't hang the worker, and is refused.
        assert!(open_in(&out, Path::new("pipe")).is_err());
        let reference = serde_json::json!({"$file": "pipe"});
        assert!(resolve(reference.as_object().unwrap(), &out).is_err());
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
