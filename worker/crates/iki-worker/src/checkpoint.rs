//! Checkpoints (ADR 0069): what a `checkpointable` action's command saved of its progress
//! in `IKI_CHECKPOINT_DIR`, packed to upload, and unpacked again for a later attempt.
//!
//! A checkpoint is an uncompressed tar file of the directory's regular files and
//! directories, in a set order, without owners or times. Links aren't saved: what a
//! command saves is its own files. A file that changes size while it's packed spoils
//! that save, which is left for the next one; commands should write each file under a
//! temporary name and rename it into place.
//!
//! The worker saves only when the directory changed since it last saved (or restored):
//! its fingerprint is each entry's path, size and modification time.

use std::fs::{File, Metadata};
use std::io::{self, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use sha2::{Digest, Sha256};

use crate::bundles::{self, Limits};
use crate::files::hex;

/// More files than a checkpoint should hold.
const MAX_FILES: usize = 1_000_000;

/// A packed checkpoint, ready to upload.
#[derive(Debug)]
pub struct Packed {
    pub path: PathBuf,
    pub size: u64,
    pub sha256: String,
    /// Regular files it holds.
    pub files: usize,
    /// Links and other entries left out.
    pub left_out: usize,
    /// The directory's fingerprint when it was packed.
    pub fingerprint: String,
}

/// An entry of the directory, by its path inside it.
struct Entry {
    relative: PathBuf,
    path: PathBuf,
    metadata: Metadata,
}

/// The directory's regular files and directories, sorted by path, and how many other
/// entries (links, sockets) there were. A directory that isn't there holds nothing.
fn walk(dir: &Path) -> io::Result<(Vec<Entry>, usize)> {
    let mut found = Vec::new();
    let mut left_out = 0usize;
    let mut pending = vec![PathBuf::new()];
    while let Some(relative) = pending.pop() {
        let entries = match std::fs::read_dir(dir.join(&relative)) {
            Ok(entries) => entries,
            Err(error)
                if error.kind() == io::ErrorKind::NotFound && relative.as_os_str().is_empty() =>
            {
                return Ok((found, 0));
            }
            Err(error) => return Err(error),
        };
        for entry in entries {
            let entry = entry?;
            // Not followed: a link's metadata is the link's own.
            let metadata = entry.metadata()?;
            let inner = relative.join(entry.file_name());
            if metadata.is_dir() {
                pending.push(inner.clone());
            } else if !metadata.is_file() {
                left_out += 1;
                continue;
            }
            found.push(Entry {
                path: entry.path(),
                relative: inner,
                metadata,
            });
        }
    }
    found.sort_by(|a, b| a.relative.cmp(&b.relative));
    Ok((found, left_out))
}

fn mtime(metadata: &Metadata) -> u128 {
    metadata
        .modified()
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map_or(0, |d| d.as_nanos())
}

fn fingerprint_of(entries: &[Entry]) -> String {
    let mut hasher = Sha256::new();
    for entry in entries {
        let kind = if entry.metadata.is_dir() { "d" } else { "f" };
        hasher.update(kind.as_bytes());
        hasher.update(entry.relative.to_string_lossy().as_bytes());
        hasher.update([0]);
        hasher.update(entry.metadata.len().to_le_bytes());
        hasher.update(mtime(&entry.metadata).to_le_bytes());
    }
    hex(&hasher.finalize())
}

/// What the directory holds now, to tell whether it changed.
pub fn fingerprint(dir: &Path) -> io::Result<String> {
    walk(dir).map(|(entries, _)| fingerprint_of(&entries))
}

/// Writes through, keeping the SHA-256 and size of what it wrote.
struct Hashing<W> {
    inner: W,
    hasher: Sha256,
    written: u64,
}

impl<W: Write> Write for Hashing<W> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let n = self.inner.write(bytes)?;
        self.hasher.update(&bytes[..n]);
        self.written += n as u64;
        Ok(n)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

/// Reads exactly `left` bytes, or fails: a file that shrank while it was packed.
struct Exact<R> {
    inner: R,
    left: u64,
}

impl<R: Read> Read for Exact<R> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        if self.left == 0 {
            return Ok(0);
        }
        let most = usize::try_from(self.left)
            .unwrap_or(usize::MAX)
            .min(buffer.len());
        let n = self.inner.read(&mut buffer[..most])?;
        if n == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "a file changed while it was saved",
            ));
        }
        self.left -= n as u64;
        Ok(n)
    }
}

/// Packs `dir` into `target`.
pub fn pack(dir: &Path, target: &Path) -> Result<Packed, String> {
    let (entries, left_out) = walk(dir).map_err(|e| format!("couldn't read it: {e}"))?;
    let out = File::create(target).map_err(|e| e.to_string())?;
    let mut builder = tar::Builder::new(Hashing {
        inner: BufWriter::new(out),
        hasher: Sha256::new(),
        written: 0,
    });
    builder.mode(tar::HeaderMode::Deterministic);
    let mut files = 0usize;
    for entry in &entries {
        let shown = entry.relative.display();
        let mut header = tar::Header::new_gnu();
        header.set_metadata_in_mode(&entry.metadata, tar::HeaderMode::Deterministic);
        if entry.metadata.is_dir() {
            header.set_size(0);
            builder
                .append_data(&mut header, &entry.relative, io::empty())
                .map_err(|e| format!("couldn't save {shown}: {e}"))?;
            continue;
        }
        let size = entry.metadata.len();
        header.set_size(size);
        let file = File::open(&entry.path).map_err(|e| format!("couldn't read {shown}: {e}"))?;
        builder
            .append_data(
                &mut header,
                &entry.relative,
                Exact {
                    inner: file,
                    left: size,
                },
            )
            .map_err(|e| format!("couldn't save {shown}: {e}"))?;
        files += 1;
    }
    let mut hashing = builder.into_inner().map_err(|e| e.to_string())?;
    hashing.flush().map_err(|e| e.to_string())?;
    hashing
        .inner
        .into_inner()
        .map_err(|e| e.to_string())?
        .sync_all()
        .map_err(|e| e.to_string())?;
    Ok(Packed {
        path: target.to_path_buf(),
        size: hashing.written,
        sha256: hex(&hashing.hasher.finalize()),
        files,
        left_out,
        fingerprint: fingerprint_of(&entries),
    })
}

/// Unpacks a checkpoint into `dir`, which should be empty: regular files and directories
/// only, none outside it.
pub fn unpack(packed: &Path, dir: &Path) -> Result<(), String> {
    let file = File::open(packed).map_err(|e| e.to_string())?;
    let limits = Limits {
        what: "a checkpoint",
        files: MAX_FILES,
        bytes: u64::MAX,
    };
    bundles::extract(io::BufReader::new(file), dir, &limits)
}

/// Empties `dir`, for a command that starts afresh.
pub fn clear(dir: &Path) -> io::Result<()> {
    match std::fs::remove_dir_all(dir) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let mut builder = std::fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    std::os::unix::fs::DirBuilderExt::mode(&mut builder, 0o700);
    builder.create(dir)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("iki-checkpoint-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn packs_and_unpacks_files_and_directories_but_not_links() {
        let dir = scratch("round-trip");
        let saved = dir.join("saved");
        std::fs::create_dir_all(saved.join("state/empty")).unwrap();
        std::fs::write(saved.join("state/step.json"), b"{\"step\": 42}").unwrap();
        std::fs::write(saved.join("weights.bin"), vec![7u8; 100_000]).unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink("/etc/passwd", saved.join("link")).unwrap();
        let packed = pack(&saved, &dir.join("checkpoint.tar")).unwrap();
        assert_eq!(packed.files, 2);
        assert_eq!(packed.left_out, usize::from(cfg!(unix)));
        let (size, sha256) = crate::files::digest(&packed.path).unwrap();
        assert_eq!(
            (size, sha256.as_str()),
            (packed.size, packed.sha256.as_str())
        );

        let restored = dir.join("restored");
        unpack(&packed.path, &restored).unwrap();
        assert_eq!(
            std::fs::read(restored.join("state/step.json")).unwrap(),
            b"{\"step\": 42}"
        );
        assert_eq!(
            std::fs::read(restored.join("weights.bin")).unwrap().len(),
            100_000
        );
        assert!(restored.join("state/empty").is_dir());
        assert!(!restored.join("link").exists());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn packs_the_same_directory_to_the_same_bytes() {
        let dir = scratch("same");
        let saved = dir.join("saved");
        std::fs::create_dir_all(saved.join("a")).unwrap();
        std::fs::write(saved.join("a/1"), b"one").unwrap();
        std::fs::write(saved.join("b"), b"two").unwrap();
        let first = pack(&saved, &dir.join("1.tar")).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(20));
        let second = pack(&saved, &dir.join("2.tar")).unwrap();
        assert_eq!(first.sha256, second.sha256);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn the_fingerprint_changes_when_a_file_does() {
        let dir = scratch("fingerprint");
        let saved = dir.join("saved");
        assert_eq!(fingerprint(&saved).unwrap(), fingerprint_of(&[]));
        std::fs::create_dir_all(&saved).unwrap();
        std::fs::write(saved.join("step"), b"1").unwrap();
        let before = fingerprint(&saved).unwrap();
        assert_eq!(fingerprint(&saved).unwrap(), before);
        std::fs::write(saved.join("step"), b"22").unwrap();
        assert_ne!(fingerprint(&saved).unwrap(), before);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_file_that_shrinks_while_it_is_packed_spoils_the_save() {
        let mut short = Exact {
            inner: &b"abc"[..],
            left: 10,
        };
        let mut out = Vec::new();
        assert!(io::copy(&mut short, &mut out).is_err());
    }

    #[test]
    fn unpacks_nothing_outside_the_directory() {
        let dir = scratch("outside");
        let path = dir.join("evil.tar");
        let mut builder = tar::Builder::new(File::create(&path).unwrap());
        let mut header = tar::Header::new_gnu();
        header.set_size(1);
        header.set_entry_type(tar::EntryType::Regular);
        // `append_data` refuses `..`; write the name as an attacker would.
        header.as_gnu_mut().unwrap().name[..9].copy_from_slice(b"../escape");
        header.set_cksum();
        builder.append(&header, &b"x"[..]).unwrap();
        builder.into_inner().unwrap();
        assert!(unpack(&path, &dir.join("into")).is_err());
        assert!(!dir.join("escape").exists());
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
