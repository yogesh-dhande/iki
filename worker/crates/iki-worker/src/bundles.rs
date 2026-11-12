//! Plugin code from the server, for a worker on someone's computer (ADR 0059).
//!
//! Without `IKI_PLUGINS_DIR`, the worker asks the server which plugins it installs, at
//! which commits, and fetches each one's bundle: a gzipped tar that `iki deploy build`
//! wrote. It checks the bundle's SHA-256 before unpacking it, and unpacks only regular
//! files and directories, inside the plugin's own directory. It then builds each
//! environment the plugin declares with uv or micromamba, from the bundle's pinned and
//! hashed files, after checking they give the digest the server expects. What it fetched
//! and built is kept in its cache, by commit and digest, so the next start reuses it.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};

use iki_protocol::{
    AvailableListOut, AvailableOut, EnvironmentBuildOut, EnvironmentBuildOutKind, PluginCode, fill,
    paths,
};
use sha2::{Digest, Sha256};

use crate::api::{Api, Error};
use crate::files::hex;
use crate::plugins::{Built, Have, Kind};
use crate::say;

/// More than any plugin's code should be, packed or not, and more files than it has.
const MAX_BUNDLE: u64 = 1 << 30;
const MAX_UNPACKED: u64 = 4 << 30;
const MAX_FILES: usize = 100_000;
/// Marks a finished unpack or build, so one cut short is done again.
const DONE: &str = ".iki-done";

/// Where a worker keeps what it fetched and built unless `IKI_WORKER_CACHE` says: the
/// user's cache directory.
pub fn default_cache(var: &dyn Fn(&str) -> Option<String>) -> Option<PathBuf> {
    let set = |name: &str| {
        var(name)
            .filter(|v| !v.trim().is_empty())
            .map(PathBuf::from)
    };
    let dir = if cfg!(windows) {
        set("LOCALAPPDATA")?
    } else {
        set("XDG_CACHE_HOME").or_else(|| set("HOME").map(|home| home.join(".cache")))?
    };
    Some(dir.join("iki").join("worker"))
}

/// What the server says it installs, to tell when it changes.
pub fn available(api: &Api) -> Result<Vec<AvailableOut>, Error> {
    Ok(api
        .get::<AvailableListOut>(paths::PLUGINS)?
        .map(|list| list.plugins)
        .unwrap_or_default())
}

/// Fetches and builds what `listed` names into `cache`, and says what the worker has.
/// A plugin it can't fetch, or an environment it can't build, is left out with a warning:
/// the worker runs the rest.
pub fn sync(api: &Api, cache: &Path, listed: &[AvailableOut]) -> Have {
    let mut code = Vec::new();
    let mut dirs = BTreeMap::new();
    let mut built = BTreeMap::new();
    let mut kept_code = BTreeSet::new();
    let mut kept_envs = BTreeSet::new();
    for plugin in listed {
        let dir = match fetch(api, cache, plugin) {
            Ok(dir) => dir,
            Err(error) => {
                say::warn(
                    "Couldn't fetch a plugin's code; running without it.",
                    &[("plugin", &plugin.name), ("detail", &error)],
                );
                continue;
            }
        };
        kept_code.insert(dir.clone());
        let mut digests = serde_json::Map::new();
        for (env, wanted) in &plugin.environments {
            let shown = format!("{}.{env}", plugin.name);
            match build(cache, &plugin.name, env, wanted, &dir) {
                Ok(done) => {
                    kept_envs.insert(done.prefix.clone());
                    digests.insert(env.clone(), wanted.digest.clone().into());
                    built.insert(shown, done);
                }
                Err(error) => say::warn(
                    "Couldn't build an environment; its actions won't run here.",
                    &[("environment", &shown), ("detail", &error)],
                ),
            }
        }
        dirs.insert(plugin.name.clone(), dir);
        code.push(PluginCode {
            name: plugin.name.clone(),
            sha: Some(plugin.sha.clone()),
            environments: Some(digests),
        });
    }
    tidy(&cache.join("plugins"), &kept_code);
    tidy(&cache.join("envs"), &kept_envs);
    code.sort_by(|a, b| a.name.cmp(&b.name));
    Have::fetched(code, built, dirs)
}

fn is_sha(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

/// The plugin's code at its commit, from the cache or the server.
fn fetch(api: &Api, cache: &Path, plugin: &AvailableOut) -> Result<PathBuf, String> {
    if !crate::plugins::is_plugin_name(&plugin.name) || !is_sha(&plugin.sha) {
        return Err("the server named it oddly".to_owned());
    }
    let dir = cache.join("plugins").join(&plugin.name).join(&plugin.sha);
    if dir.join(DONE).is_file() {
        return Ok(dir);
    }
    let size = u64::try_from(plugin.bundle.size)
        .ok()
        .filter(|s| *s <= MAX_BUNDLE)
        .ok_or("its bundle is too big")?;
    let downloads = cache.join("downloads");
    std::fs::create_dir_all(&downloads).map_err(|e| e.to_string())?;
    let packed = downloads.join(format!("{}-{}.tar.gz", plugin.name, plugin.sha));
    let path = fill(paths::PLUGINS_NAME_SHA, &[&plugin.name, &plugin.sha]);
    let body = api.download(&path).map_err(|e| e.to_string())?;
    let got = save(body, &packed, size);
    let result = got.and_then(|sha256| {
        if sha256 == plugin.bundle.sha256 {
            say::info(
                "Fetched a plugin's code.",
                &[("plugin", &plugin.name), ("sha", &plugin.sha)],
            );
            let partial = dir.with_extension("partial");
            let _ = std::fs::remove_dir_all(&partial);
            unpack(&packed, &partial)?;
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::rename(&partial, &dir).map_err(|e| e.to_string())?;
            std::fs::write(dir.join(DONE), b"").map_err(|e| e.to_string())?;
            Ok(dir)
        } else {
            Err(format!(
                "its bundle's SHA-256 is {sha256}, not the {} the server listed",
                plugin.bundle.sha256
            ))
        }
    });
    let _ = std::fs::remove_file(&packed);
    result
}

/// Writes `body` to `target`, no more than `size` bytes, and returns its SHA-256.
fn save(body: ureq::Body, target: &Path, size: u64) -> Result<String, String> {
    let mut reader = body.into_with_config().limit(size + 1).reader();
    let mut out = File::create(target).map_err(|e| e.to_string())?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0; 1 << 20];
    let mut got = 0u64;
    loop {
        let read = match reader.read(&mut buffer) {
            Ok(0) => break,
            Ok(read) => read,
            Err(_) if got >= size => break,
            Err(error) => return Err(format!("the download broke off: {error}")),
        };
        got += read as u64;
        if got > size {
            return Err(format!(
                "it's bigger than the {size} bytes the server listed"
            ));
        }
        hasher.update(&buffer[..read]);
        out.write_all(&buffer[..read]).map_err(|e| e.to_string())?;
    }
    out.sync_all().map_err(|e| e.to_string())?;
    if got != size {
        return Err(format!(
            "it came to {got} bytes, not the {size} the server listed"
        ));
    }
    Ok(hex(&hasher.finalize()))
}

/// Where an entry of a bundle goes inside `dir`: only plain relative paths.
fn inside(dir: &Path, name: &Path) -> Option<PathBuf> {
    let mut target = dir.to_path_buf();
    let mut any = false;
    for part in name.components() {
        match part {
            Component::Normal(part) => {
                target.push(part);
                any = true;
            }
            Component::CurDir => {}
            _ => return None,
        }
    }
    any.then_some(target)
}

/// Unpacks a bundle into `dir`: regular files and directories only, none outside it, and
/// no more than a plugin should hold.
pub fn unpack(packed: &Path, dir: &Path) -> Result<(), String> {
    let file = File::open(packed).map_err(|e| e.to_string())?;
    let mut archive = tar::Archive::new(flate2::read::GzDecoder::new(file));
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    let mut total = 0u64;
    let mut count = 0usize;
    for entry in archive
        .entries()
        .map_err(|e| format!("it isn't a bundle: {e}"))?
    {
        let mut entry = entry.map_err(|e| format!("it isn't a bundle: {e}"))?;
        count += 1;
        if count > MAX_FILES {
            return Err("it holds too many files".to_owned());
        }
        let name = entry.path().map_err(|e| e.to_string())?.into_owned();
        let shown = name.display().to_string();
        let target = inside(dir, &name).ok_or(format!("it names a file outside it: {shown}"))?;
        let kind = entry.header().entry_type();
        if kind.is_dir() {
            std::fs::create_dir_all(&target).map_err(|e| e.to_string())?;
            continue;
        }
        if !kind.is_file() {
            return Err(format!("it holds something other than a file: {shown}"));
        }
        total += entry.size();
        if total > MAX_UNPACKED {
            return Err("it unpacks to more than a plugin should hold".to_owned());
        }
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let executable = entry.header().mode().unwrap_or(0) & 0o111 != 0;
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        std::os::unix::fs::OpenOptionsExt::mode(
            &mut options,
            if executable { 0o755 } else { 0o644 },
        );
        let mut out = options
            .open(&target)
            .map_err(|e| format!("couldn't write {shown}: {e}"))?;
        std::io::copy(&mut entry, &mut out).map_err(|e| format!("couldn't write {shown}: {e}"))?;
    }
    Ok(())
}

/// The digest an environment's definition gives, as `iki deploy build` computes it.
fn digest(definition: &BTreeMap<&str, String>) -> String {
    let canonical = serde_json::to_vec(definition).unwrap_or_default();
    format!("sha256:{}", hex(&Sha256::digest(canonical)))
}

fn file_sha256(path: &Path) -> Result<String, String> {
    crate::files::digest(path)
        .map(|(_, sha)| sha)
        .map_err(|e| format!("couldn't read {}: {e}", path.display()))
}

/// A file a bundle names, inside the plugin's directory.
fn bundled(dir: &Path, name: Option<&str>) -> Result<PathBuf, String> {
    let name = name.ok_or("the server didn't say which file builds it")?;
    inside(dir, Path::new(name))
        .filter(|path| path.is_file())
        .ok_or(format!("the bundle has no {name}"))
}

/// What a command line printed, for a message: its last few lines.
fn tail(output: &[u8]) -> String {
    let text = String::from_utf8_lossy(output);
    let lines: Vec<&str> = text.lines().collect();
    lines[lines.len().saturating_sub(5)..].join(" | ")
}

fn run(program: &str, args: &[&std::ffi::OsStr]) -> Result<(), String> {
    let output = Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .output()
        .map_err(|error| match error.kind() {
            std::io::ErrorKind::NotFound => format!(
                "{program} isn't installed: install it to run these actions on this computer"
            ),
            _ => format!("couldn't run {program}: {error}"),
        })?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!("{program} failed: {}", tail(&output.stderr)))
    }
}

/// Builds the environment at `prefix`, with uv or micromamba.
fn install(
    kind: Kind,
    prefix: &Path,
    definition: &BTreeMap<&str, String>,
    wanted: &EnvironmentBuildOut,
    file: &Path,
) -> Result<(), String> {
    let os = |s: &str| std::ffi::OsString::from(s);
    match kind {
        Kind::Python => {
            let python = definition["python"].clone();
            run(
                "uv",
                &[
                    &os("venv"),
                    &os("--quiet"),
                    &os("--python"),
                    &os(&python),
                    prefix.as_os_str(),
                ],
            )
            .and_then(|()| {
                if wanted.packages != Some(true) {
                    return Ok(());
                }
                // Every package pinned and hashed, so nothing else is installed.
                run(
                    "uv",
                    &[
                        &os("pip"),
                        &os("install"),
                        &os("--quiet"),
                        &os("--python"),
                        prefix.as_os_str(),
                        &os("--require-hashes"),
                        &os("--no-deps"),
                        &os("--requirement"),
                        file.as_os_str(),
                    ],
                )
            })
        }
        Kind::Conda => run(
            "micromamba",
            &[
                &os("create"),
                &os("--yes"),
                &os("--quiet"),
                &os("--prefix"),
                prefix.as_os_str(),
                &os("--file"),
                file.as_os_str(),
            ],
        ),
    }
}

/// The environment `wanted` describes, built in the cache unless it already is.
fn build(
    cache: &Path,
    plugin: &str,
    env: &str,
    wanted: &EnvironmentBuildOut,
    dir: &Path,
) -> Result<Built, String> {
    let hex_digest = wanted
        .digest
        .strip_prefix("sha256:")
        .filter(|h| h.len() == 64 && h.bytes().all(|b| b.is_ascii_hexdigit()))
        .ok_or("the server gave an odd digest")?;
    if !crate::plugins::is_plugin_name(plugin)
        || env.is_empty()
        || !env
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return Err("the server named it oddly".to_owned());
    }
    let prefix = cache
        .join("envs")
        .join(plugin)
        .join(env)
        .join(&hex_digest[..16]);
    let (kind, definition, file) = match wanted.kind {
        EnvironmentBuildOutKind::Python => {
            let python = wanted
                .python
                .clone()
                .ok_or("the server gave no Python version")?;
            let file = bundled(dir, wanted.requirements.as_deref())?;
            let definition = BTreeMap::from([
                ("kind", "python".to_owned()),
                ("python", python),
                ("requirements_sha256", file_sha256(&file)?),
            ]);
            (Kind::Python, definition, file)
        }
        EnvironmentBuildOutKind::Conda => {
            let file = bundled(dir, wanted.lockfile.as_deref())?;
            let definition = BTreeMap::from([
                ("kind", "conda".to_owned()),
                ("lockfile_sha256", file_sha256(&file)?),
            ]);
            (Kind::Conda, definition, file)
        }
        EnvironmentBuildOutKind::Unknown => {
            return Err("it's a kind this worker doesn't know: update iki-worker".to_owned());
        }
    };
    // The bundle was checked, but check the build gets what the server will expect.
    let computed = digest(&definition);
    if computed != wanted.digest {
        return Err(format!(
            "its files give {computed}, not the {} the server expects",
            wanted.digest
        ));
    }
    let done = Built {
        digest: wanted.digest.clone(),
        kind,
        prefix: prefix.clone(),
    };
    if prefix.join(DONE).is_file() {
        return Ok(done);
    }
    say::info(
        "Building an environment.",
        &[("environment", &format!("{plugin}.{env}"))],
    );
    let _ = std::fs::remove_dir_all(&prefix);
    if let Some(parent) = prefix.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let built = install(kind, &prefix, &definition, wanted, &file);
    if let Err(error) = built {
        let _ = std::fs::remove_dir_all(&prefix);
        return Err(error);
    }
    std::fs::write(prefix.join(DONE), b"").map_err(|e| e.to_string())?;
    Ok(done)
}

/// Removes what `root` holds, three levels down (name, then commit or environment and
/// digest), that isn't `kept`: code and builds no plugin the server installs needs.
fn tidy(root: &Path, kept: &BTreeSet<PathBuf>) {
    let Ok(names) = std::fs::read_dir(root) else {
        return;
    };
    for name in names.flatten() {
        let mut stack = vec![(name.path(), 1)];
        while let Some((path, depth)) = stack.pop() {
            if kept.iter().any(|k| k.starts_with(&path)) {
                if depth < 3
                    && !kept.contains(&path)
                    && let Ok(children) = std::fs::read_dir(&path)
                {
                    stack.extend(children.flatten().map(|c| (c.path(), depth + 1)));
                }
                continue;
            }
            let _ = std::fs::remove_dir_all(&path);
        }
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("iki-bundles-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn tarball(dir: &Path, entries: &[(&str, tar::EntryType, &[u8])]) -> PathBuf {
        let path = dir.join("bundle.tar.gz");
        let file = File::create(&path).unwrap();
        let gz = flate2::write::GzEncoder::new(file, flate2::Compression::default());
        let mut builder = tar::Builder::new(gz);
        for (name, kind, data) in entries {
            let mut header = tar::Header::new_gnu();
            header.set_entry_type(*kind);
            header.set_size(data.len() as u64);
            header.set_mode(0o755);
            if *kind == tar::EntryType::Symlink {
                header.set_link_name("/etc/passwd").unwrap();
            }
            // Written raw, so a name with `..` gets through as an attacker would send it.
            let bytes = name.as_bytes();
            header.as_old_mut().name[..bytes.len()].copy_from_slice(bytes);
            header.set_cksum();
            builder.append(&header, *data).unwrap();
        }
        builder.into_inner().unwrap().finish().unwrap();
        path
    }

    #[test]
    fn unpacks_only_regular_files_inside_the_plugin() {
        let dir = scratch("unpack");
        let ok = tarball(
            &dir,
            &[
                ("scripts/run.py", tar::EntryType::Regular, b"print(1)"),
                ("plugin.yaml", tar::EntryType::Regular, b"name: x"),
            ],
        );
        let out = dir.join("out");
        unpack(&ok, &out).unwrap();
        assert_eq!(
            std::fs::read(out.join("scripts/run.py")).unwrap(),
            b"print(1)"
        );
        for (bad, kind) in [
            ("../escape", tar::EntryType::Regular),
            ("/etc/evil", tar::EntryType::Regular),
            ("link", tar::EntryType::Symlink),
            ("dev", tar::EntryType::Char),
        ] {
            let packed = tarball(&dir, &[(bad, kind, b"")]);
            let target = dir.join(format!("bad-{}", bad.replace('/', "_")));
            assert!(unpack(&packed, &target).is_err(), "{bad}");
        }
        assert!(!dir.join("escape").exists());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn computes_the_digest_iki_deploy_build_does() {
        // From cli/tests: {"kind":"python","python":"3.12","requirements_sha256":"…"}.
        let definition = BTreeMap::from([
            ("kind", "python".to_owned()),
            ("python", "3.12".to_owned()),
            ("requirements_sha256", "ab".to_owned()),
        ]);
        let expected = format!(
            "sha256:{}",
            hex(&Sha256::digest(
                br#"{"kind":"python","python":"3.12","requirements_sha256":"ab"}"#
            ))
        );
        assert_eq!(digest(&definition), expected);
    }

    #[test]
    fn keeps_only_what_is_still_needed() {
        let root = scratch("tidy");
        for path in ["protein/aaa", "protein/bbb", "dna/ccc"] {
            std::fs::create_dir_all(root.join(path)).unwrap();
        }
        tidy(&root, &BTreeSet::from([root.join("protein/bbb")]));
        assert!(root.join("protein/bbb").is_dir());
        assert!(!root.join("protein/aaa").exists());
        assert!(!root.join("dna").exists());
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    #[cfg(unix)]
    fn keeps_its_cache_in_the_users_cache_directory() {
        let env = |pairs: &[(&str, &str)]| {
            let map: HashMap<String, String> = pairs
                .iter()
                .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
                .collect();
            move |name: &str| map.get(name).cloned()
        };
        assert_eq!(
            default_cache(&env(&[("HOME", "/home/ada")])),
            Some(PathBuf::from("/home/ada/.cache/iki/worker"))
        );
        assert_eq!(default_cache(&env(&[])), None);
    }
}
