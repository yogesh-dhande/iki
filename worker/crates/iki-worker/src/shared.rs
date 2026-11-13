//! Versions of an environment's volume on a shared file system (ADR 0077) or in a bucket
//! (ADR 0078), with the file system's (or bucket's) directory for Iki mounted at `<root>`:
//!
//! - `iki-worker prepare <root> <directory>...` makes each version's own directory, before
//!   a setup build's pod mounts only that one (Kubernetes would make a missing one as
//!   root, which runs' user couldn't write).
//! - `iki-worker publish <from> <root> <directory>` copies what a setup build wrote to its
//!   disk, mounted at `<from>`, into its version's directory in a bucket, which can't be
//!   written in place: each file once, from start to end.
//! - `iki-worker remove <root> <directory>...` deletes versions once no machine reads them.
//!
//! Each directory must be a plain path under `<root>` (no `..`, not absolute). Neither
//! follows a symbolic link out of it.

use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};

use crate::say;

/// Whether `directory` is a plain relative path: no `..`, not absolute, not empty.
pub fn plain(directory: &str) -> bool {
    !directory.is_empty()
        && Path::new(directory)
            .components()
            .all(|c| matches!(c, Component::Normal(_)))
}

fn outcome(verb: &str, failed: &[String]) -> Result<(), String> {
    if failed.is_empty() {
        Ok(())
    } else {
        Err(format!("Couldn't {verb} {}", failed.join("; ")))
    }
}

/// Makes each directory under `root`, and any above it, each a real directory, not a
/// link. One that's there already is fine. Any it couldn't is an error, naming them all.
pub fn prepare(root: &Path, directories: &[String]) -> Result<(), String> {
    if !root.is_dir() {
        return Err(format!("{} isn't a directory.", root.display()));
    }
    let mut failed = Vec::new();
    'each: for directory in directories {
        if !plain(directory) {
            failed.push(format!("{directory}: not a plain relative path"));
            continue;
        }
        let mut path = root.to_path_buf();
        for part in Path::new(directory).components() {
            path.push(part);
            let made: Result<(), String> = match std::fs::symlink_metadata(&path) {
                Ok(found) if found.is_dir() => continue,
                Ok(_) => Err("not a directory".to_owned()),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    match std::fs::create_dir(&path) {
                        Ok(()) => continue,
                        // Another build made it meanwhile: fine, if it's a directory.
                        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                            match std::fs::symlink_metadata(&path) {
                                Ok(found) if found.is_dir() => continue,
                                _ => Err("not a directory".to_owned()),
                            }
                        }
                        Err(error) => Err(error.to_string()),
                    }
                }
                Err(error) => Err(error.to_string()),
            };
            if let Err(why) = made {
                let at = path
                    .strip_prefix(root)
                    .unwrap_or(&path)
                    .display()
                    .to_string();
                failed.push(format!("{directory}: {at}: {why}"));
                continue 'each;
            }
        }
        say::info("Ready.", &[("directory", directory)]);
    }
    outcome("make", &failed)
}

/// Deletes each directory under `root`. One that's gone already is fine. Any it couldn't
/// is an error, naming them all.
pub fn remove(root: &Path, directories: &[String]) -> Result<(), String> {
    if !root.is_dir() {
        return Err(format!("{} isn't a directory.", root.display()));
    }
    let mut failed = Vec::new();
    for directory in directories {
        if !plain(directory) {
            failed.push(format!("{directory}: not a plain relative path"));
            continue;
        }
        let path = root.join(directory);
        match std::fs::symlink_metadata(&path) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                say::info("Already gone.", &[("directory", directory)]);
            }
            Err(error) => failed.push(format!("{directory}: {error}")),
            Ok(found) if !found.is_dir() => {
                failed.push(format!("{directory}: not a directory"));
            }
            // remove_dir_all doesn't follow symbolic links, so nothing outside goes.
            Ok(_) => match std::fs::remove_dir_all(&path) {
                Ok(()) => say::info("Removed.", &[("directory", directory)]),
                // A bucket's directory is gone once nothing's in it, so the last step
                // (removing it) may find it gone already.
                Err(_) if gone(&path) => say::info("Removed.", &[("directory", directory)]),
                Err(error) => failed.push(format!("{directory}: {error}")),
            },
        }
    }
    outcome("remove", &failed)
}

fn gone(path: &Path) -> bool {
    matches!(std::fs::symlink_metadata(path), Err(e) if e.kind() == std::io::ErrorKind::NotFound)
}

/// What there is to copy: directories, then each file with its size, by path under the
/// disk.
#[derive(Default)]
struct Found {
    directories: Vec<PathBuf>,
    files: Vec<(PathBuf, u64)>,
    refused: Vec<String>,
}

fn walk(from: &Path, under: &Path, found: &mut Found) -> std::io::Result<()> {
    let mut entries = std::fs::read_dir(from.join(under))?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(std::fs::DirEntry::file_name);
    for entry in entries {
        // The disk's file system's own (ext4's), which only root can read: not the script's.
        if under.as_os_str().is_empty() && entry.file_name() == "lost+found" {
            continue;
        }
        let path = under.join(entry.file_name());
        let kind = entry.file_type()?;
        if kind.is_dir() {
            found.directories.push(path.clone());
            walk(from, &path, found)?;
        } else if kind.is_file() {
            found.files.push((path, entry.metadata()?.len()));
        } else {
            let what = if kind.is_symlink() {
                "a symbolic link"
            } else {
                "not a regular file"
            };
            found.refused.push(format!("{}: {what}", path.display()));
        }
    }
    Ok(())
}

/// The most of a file read at once: large, as a bucket takes it in large parts.
const BUFFER: usize = 8 * 1024 * 1024;

fn copy(from: &Path, to: &Path, size: u64, buffer: &mut [u8]) -> Result<bool, String> {
    match std::fs::symlink_metadata(to) {
        // An earlier try copied it: a bucket only shows a file once it's all there.
        Ok(there) if there.is_file() && there.len() == size => return Ok(false),
        Ok(there) if there.is_file() => std::fs::remove_file(to).map_err(|e| e.to_string())?,
        Ok(_) => return Err("there's something else there".to_owned()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.to_string()),
    }
    let mut source = std::fs::File::open(from).map_err(|e| e.to_string())?;
    let mut out = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(to)
        .map_err(|e| e.to_string())?;
    loop {
        let read = match source.read(buffer) {
            Ok(0) => break,
            Ok(read) => read,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error.to_string()),
        };
        out.write_all(&buffer[..read]).map_err(|e| e.to_string())?;
    }
    // Finishes the upload, so a failure to is seen here rather than lost on close.
    out.sync_all().map_err(|e| e.to_string())?;
    drop(out);
    match std::fs::symlink_metadata(to) {
        Ok(there) if there.len() == size => Ok(true),
        Ok(there) => Err(format!("{} bytes of {size} arrived", there.len())),
        Err(error) => Err(error.to_string()),
    }
}

/// Copies everything under `from` into `directory` under `root`, made as
/// [`prepare`] makes it. Anything that a bucket can't hold (a symbolic link, a device) is
/// an error before anything's copied. A file there already with the same size is one an
/// earlier try copied, so trying again carries on.
pub fn publish(from: &Path, root: &Path, directory: &str) -> Result<(), String> {
    if !from.is_dir() {
        return Err(format!("{} isn't a directory.", from.display()));
    }
    prepare(root, &[directory.to_owned()])?;
    let mut found = Found::default();
    walk(from, Path::new(""), &mut found)
        .map_err(|e| format!("Couldn't read {}: {e}", from.display()))?;
    if !found.refused.is_empty() {
        let shown = found.refused.iter().take(10).cloned().collect::<Vec<_>>();
        let more = found.refused.len() - shown.len();
        return Err(format!(
            "A bucket can only hold directories and regular files, so these can't be put \
             there: {}{}. Write them as regular files instead.",
            shown.join("; "),
            if more > 0 {
                format!(" (and {more} more)")
            } else {
                String::new()
            }
        ));
    }
    let to = root.join(directory);
    for path in &found.directories {
        match std::fs::create_dir(to.join(path)) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                if !std::fs::symlink_metadata(to.join(path)).is_ok_and(|m| m.is_dir()) {
                    return Err(format!("{}: not a directory", path.display()));
                }
            }
            Err(error) => return Err(format!("{}: {error}", path.display())),
        }
    }
    let total: u64 = found.files.iter().map(|(_, size)| size).sum();
    let mut buffer = vec![0; BUFFER];
    let (mut copied, mut bytes) = (0_usize, 0_u64);
    for (path, size) in &found.files {
        let fresh = copy(&from.join(path), &to.join(path), *size, &mut buffer)
            .map_err(|why| format!("Couldn't copy {}: {why}", path.display()))?;
        copied += usize::from(fresh);
        let before = bytes;
        bytes += size;
        // Every tenth of the way, for its log.
        if total > 0 && before * 10 / total != bytes * 10 / total {
            say::info("Copying.", &[("percent", &(bytes * 100 / total))]);
        }
    }
    say::info(
        "Published.",
        &[
            ("directory", &directory),
            ("files", &found.files.len()),
            ("copied", &copied),
            ("bytes", &total),
        ],
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base(name: &str) -> std::path::PathBuf {
        let base = std::env::temp_dir().join(format!("iki-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        base
    }

    #[test]
    fn it_makes_versions_directories_under_the_root_and_nowhere_else() {
        let base = base("prepare");
        let root = base.join("iki");
        let outside = base.join("outside");
        std::fs::create_dir_all(root.join("env")).unwrap();
        std::fs::create_dir_all(&outside).unwrap();
        let asked = ["env/af-db/SETUP-000001".to_owned()];
        assert_eq!(prepare(&root, &asked), Ok(()));
        assert!(root.join("env/af-db/SETUP-000001").is_dir());
        // Again, as a build's next machine would: it's there already.
        assert_eq!(prepare(&root, &asked), Ok(()));
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(&outside, root.join("env/linked")).unwrap();
            let refused = prepare(&root, &["env/linked/SETUP-000002".to_owned()]).unwrap_err();
            assert!(refused.contains("env/linked: not a directory"), "{refused}");
            assert!(
                !outside.join("SETUP-000002").exists(),
                "it followed a link out"
            );
        }
        let refused =
            prepare(&root, &["../outside/x".to_owned(), "/etc/x".to_owned()]).unwrap_err();
        assert!(
            refused.contains("../outside/x: not a plain relative path"),
            "{refused}"
        );
        assert!(
            refused.contains("/etc/x: not a plain relative path"),
            "{refused}"
        );
        assert!(prepare(&base.join("missing"), &asked).is_err());
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn it_publishes_a_disk_into_a_versions_directory_and_carries_on_after_a_failed_try() {
        let base = base("publish");
        let from = base.join("disk");
        let root = base.join("iki");
        std::fs::create_dir_all(from.join("db/params")).unwrap();
        std::fs::create_dir_all(from.join("empty")).unwrap();
        std::fs::create_dir_all(from.join("lost+found")).unwrap();
        std::fs::write(from.join("db/params/a.npz"), vec![7_u8; BUFFER + 3]).unwrap();
        std::fs::write(from.join("README"), "hi").unwrap();
        std::fs::create_dir_all(&root).unwrap();
        let directory = "env/af-db/SETUP-000001";
        // An earlier try copied part of one file, and all of another.
        let to = root.join(directory);
        std::fs::create_dir_all(to.join("db/params")).unwrap();
        std::fs::write(to.join("db/params/a.npz"), "partial").unwrap();
        std::fs::write(to.join("README"), "hi").unwrap();
        assert_eq!(publish(&from, &root, directory), Ok(()));
        assert_eq!(
            std::fs::read(to.join("db/params/a.npz")).unwrap(),
            vec![7_u8; BUFFER + 3]
        );
        assert_eq!(std::fs::read_to_string(to.join("README")).unwrap(), "hi");
        assert!(to.join("empty").is_dir());
        assert!(!to.join("lost+found").exists());
        // Again: nothing to copy.
        assert_eq!(publish(&from, &root, directory), Ok(()));
        assert!(publish(&from, &root, "../escape").is_err());
        assert!(publish(&base.join("missing"), &root, directory).is_err());
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink("README", from.join("db/link")).unwrap();
            let refused = publish(&from, &root, "env/af-db/SETUP-000002").unwrap_err();
            assert!(refused.contains("db/link: a symbolic link"), "{refused}");
            assert!(
                !root.join("env/af-db/SETUP-000002/README").exists(),
                "it copied before refusing"
            );
        }
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn it_removes_versions_under_the_root_and_nothing_else() {
        let base = base("remove");
        let root = base.join("iki");
        let outside = base.join("outside");
        std::fs::create_dir_all(root.join("env/af-db/SETUP-000001/sub")).unwrap();
        std::fs::create_dir_all(root.join("env/af-db/SETUP-000002")).unwrap();
        std::fs::create_dir_all(&outside).unwrap();
        std::fs::write(outside.join("keep"), "x").unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(&outside, root.join("env/af-db/SETUP-000001/sub/link")).unwrap();
        let asked = [
            "env/af-db/SETUP-000001".to_owned(),
            "env/af-db/SETUP-000009".to_owned(),
        ];
        assert_eq!(remove(&root, &asked), Ok(()));
        assert!(!root.join("env/af-db/SETUP-000001").exists());
        assert!(root.join("env/af-db/SETUP-000002").is_dir());
        assert!(outside.join("keep").exists(), "it followed a link out");
        let refused = remove(&root, &["../outside".to_owned(), "/etc".to_owned()]).unwrap_err();
        assert!(
            refused.contains("../outside: not a plain relative path"),
            "{refused}"
        );
        assert!(outside.is_dir());
        let _ = std::fs::remove_dir_all(&base);
    }
}
