//! Versions of an environment's volume on a shared file system (ADR 0077), with the file
//! system's directory for Iki mounted at `<root>`:
//!
//! - `iki-worker prepare <root> <directory>...` makes each version's own directory, before
//!   a setup build's pod mounts only that one (Kubernetes would make a missing one as
//!   root, which runs' user couldn't write).
//! - `iki-worker remove <root> <directory>...` deletes versions once no machine reads them.
//!
//! Each directory must be a plain path under `<root>` (no `..`, not absolute). Neither
//! follows a symbolic link out of it.

use std::path::{Component, Path};

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
                Err(error) => failed.push(format!("{directory}: {error}")),
            },
        }
    }
    outcome("remove", &failed)
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
