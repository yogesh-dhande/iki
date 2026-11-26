//! A workbook's ZIP, read by hand (APPNOTE 6.3): its end of central directory record (and
//! ZIP64's), its central directory, then each part's local header and data, stored or
//! Deflate-compressed. Every part is inflated through [`Part`], which stops at a bound
//! whatever the headers claim, so a ZIP bomb is refused at its first megabytes.

use std::io::Read;

use miniz_oxide::inflate::stream::InflateState;
use miniz_oxide::{DataFormat, MZError, MZFlush, MZStatus};

use super::MAX_PART_BYTES;

/// The most parts a workbook may have, as the importer allows.
const MAX_PARTS: usize = 10_000;
/// How many times its compressed size a part may grow, past a megabyte, as the importer
/// allows: sheets' XML compresses 10 to 30 times, ZIP bombs a thousand.
const MAX_RATIO: u64 = 200;
/// The megabyte any part may grow to whatever its compressed size.
const MIN_LIMIT: u64 = 1024 * 1024;
/// The most all the parts read may inflate to together, as the importer allows.
const MAX_UNPACKED: u64 = 512 * 1024 * 1024;
/// How far from the end the end of central directory record can be: its 22 bytes and a
/// comment of up to 65,535.
const EOCD_SEARCH: usize = 22 + 65_535;
/// The importer's message for a part past its bounds.
const TOO_BIG: &str = "It unpacks to far more than it holds: it isn't a workbook Iki \
    reads (at most 512 MiB unpacked).";

/// One part (file) in the ZIP.
#[derive(Debug, Clone)]
pub(super) struct Entry {
    pub(super) name: String,
    method: u16,
    flags: u16,
    compressed: u64,
    local: u64,
}

/// The ZIP's parts.
pub(super) struct Zip<'a> {
    bytes: &'a [u8],
    entries: Vec<Entry>,
    unpacked: std::rc::Rc<std::cell::Cell<u64>>,
}

fn u16_at(bytes: &[u8], at: usize) -> Option<u16> {
    let b = bytes.get(at..at.checked_add(2)?)?;
    Some(u16::from_le_bytes([b[0], b[1]]))
}

fn u32_at(bytes: &[u8], at: usize) -> Option<u32> {
    let b = bytes.get(at..at.checked_add(4)?)?;
    Some(u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
}

fn u64_at(bytes: &[u8], at: usize) -> Option<u64> {
    let b = bytes.get(at..at.checked_add(8)?)?;
    let mut a = [0u8; 8];
    a.copy_from_slice(b);
    Some(u64::from_le_bytes(a))
}

fn damaged(what: &str) -> String {
    format!("It's damaged: {what}.")
}

impl<'a> Zip<'a> {
    /// Reads the central directory, refusing encrypted parts and compression methods
    /// workbooks don't use, as the importer does, before any part is opened.
    #[allow(clippy::too_many_lines)] // The directory's records, field by field.
    pub(super) fn open(bytes: &'a [u8]) -> Result<Self, String> {
        let not_zip = || {
            "It isn't a workbook Iki can read: save it as an Excel Workbook (.xlsx), or as CSV."
                .to_owned()
        };
        if !bytes.starts_with(b"PK\x03\x04") {
            return Err(not_zip());
        }
        let floor = bytes.len().saturating_sub(EOCD_SEARCH);
        let mut eocd = None;
        let mut at = bytes.len().saturating_sub(22);
        loop {
            if u32_at(bytes, at) == Some(0x0605_4b50) {
                eocd = Some(at);
                break;
            }
            if at <= floor || at == 0 {
                break;
            }
            at -= 1;
        }
        let eocd =
            eocd.ok_or_else(|| damaged("its ZIP directory is missing, as if it was cut short"))?;
        let short = || damaged("its ZIP directory is cut short");
        let mut count = u64::from(u16_at(bytes, eocd + 10).ok_or_else(short)?);
        let mut size = u64::from(u32_at(bytes, eocd + 12).ok_or_else(short)?);
        let mut offset = u64::from(u32_at(bytes, eocd + 16).ok_or_else(short)?);
        if count == 0xffff || size == 0xffff_ffff || offset == 0xffff_ffff {
            // ZIP64: its locator is just before the record.
            let locator = eocd.checked_sub(20).ok_or_else(short)?;
            if u32_at(bytes, locator) == Some(0x0706_4b50) {
                let record = u64_at(bytes, locator + 8).ok_or_else(short)?;
                let record = usize::try_from(record).map_err(|_| short())?;
                if u32_at(bytes, record) != Some(0x0606_4b50) {
                    return Err(damaged("its ZIP64 directory isn't where it says"));
                }
                count = u64_at(bytes, record + 32).ok_or_else(short)?;
                size = u64_at(bytes, record + 40).ok_or_else(short)?;
                offset = u64_at(bytes, record + 48).ok_or_else(short)?;
            }
        }
        if count > MAX_PARTS as u64 {
            return Err(format!(
                "It has over {} parts: it isn't a workbook.",
                super::thousands(MAX_PARTS)
            ));
        }
        let start = usize::try_from(offset).map_err(|_| short())?;
        let end = usize::try_from(offset.saturating_add(size)).map_err(|_| short())?;
        let directory = bytes
            .get(start..end)
            .ok_or_else(|| damaged("its ZIP directory says it's past the end of the file"))?;
        let mut entries = Vec::new();
        let mut at = 0usize;
        while entries.len() < usize::try_from(count).unwrap_or(MAX_PARTS) {
            if u32_at(directory, at) != Some(0x0201_4b50) {
                return Err(damaged("its ZIP directory has an entry that isn't one"));
            }
            let field = |o: usize| u16_at(directory, at + o).ok_or_else(short);
            let flags = field(8)?;
            let method = field(10)?;
            let mut compressed = u64::from(u32_at(directory, at + 20).ok_or_else(short)?);
            let mut uncompressed = u64::from(u32_at(directory, at + 24).ok_or_else(short)?);
            let name_len = usize::from(field(28)?);
            let extra_len = usize::from(field(30)?);
            let comment_len = usize::from(field(32)?);
            let mut local = u64::from(u32_at(directory, at + 42).ok_or_else(short)?);
            let name = directory
                .get(at + 46..at + 46 + name_len)
                .ok_or_else(short)?;
            let extra = directory
                .get(at + 46 + name_len..at + 46 + name_len + extra_len)
                .ok_or_else(short)?;
            // ZIP64's extra field holds whichever of the sizes and offset are 0xFFFFFFFF.
            let mut e = 0usize;
            while e + 4 <= extra.len() {
                let id = u16_at(extra, e).unwrap_or(0);
                let len = usize::from(u16_at(extra, e + 2).unwrap_or(0));
                if id == 1 {
                    let mut p = e + 4;
                    for value in [&mut uncompressed, &mut compressed, &mut local] {
                        if *value == 0xffff_ffff {
                            *value = u64_at(extra, p).ok_or_else(short)?;
                            p += 8;
                        }
                    }
                }
                e += 4 + len;
            }
            entries.push(Entry {
                name: String::from_utf8_lossy(name).into_owned(),
                method,
                flags,
                compressed,
                local,
            });
            at += 46 + name_len + extra_len + comment_len;
        }
        for entry in &entries {
            if entry.flags & 1 != 0 {
                return Err(
                    "Its parts are encrypted: remove the password and save it again.".to_owned(),
                );
            }
            if entry.method != 0 && entry.method != 8 {
                return Err("Its parts are compressed in a way workbooks aren't.".to_owned());
            }
        }
        Ok(Self {
            bytes,
            entries,
            unpacked: std::rc::Rc::default(),
        })
    }

    /// Whether it has a part of this name.
    pub(super) fn has(&self, name: &str) -> bool {
        self.find(name).is_some()
    }

    /// The part of this name: OPC part names don't depend on case, so neither does this.
    fn find(&self, name: &str) -> Option<&Entry> {
        let name = name.trim_start_matches('/');
        self.entries.iter().find(|e| e.name == name).or_else(|| {
            self.entries
                .iter()
                .find(|e| e.name.eq_ignore_ascii_case(name))
        })
    }

    /// The part of this name, to read (inflating as it's read), or `None` if there's none.
    pub(super) fn open_part(&self, name: &str) -> Result<Option<Part<'a>>, String> {
        let Some(entry) = self.find(name) else {
            return Ok(None);
        };
        let gone = || damaged(&format!("its part {} is cut short", entry.name));
        let local = usize::try_from(entry.local).map_err(|_| gone())?;
        if u32_at(self.bytes, local) != Some(0x0403_4b50) {
            return Err(damaged(&format!(
                "its part {} isn't where its directory says",
                entry.name
            )));
        }
        let name_len = usize::from(u16_at(self.bytes, local + 26).ok_or_else(gone)?);
        let extra_len = usize::from(u16_at(self.bytes, local + 28).ok_or_else(gone)?);
        let start = local + 30 + name_len + extra_len;
        let end = usize::try_from(entry.compressed)
            .ok()
            .and_then(|c| start.checked_add(c))
            .ok_or_else(gone)?;
        let data = self.bytes.get(start..end).ok_or_else(gone)?;
        let limit = (entry.compressed.saturating_mul(MAX_RATIO))
            .max(MIN_LIMIT)
            .min(MAX_PART_BYTES as u64);
        Ok(Some(Part {
            name: entry.name.clone(),
            data,
            at: 0,
            inflate: (entry.method == 8).then(|| InflateState::new_boxed(DataFormat::Raw)),
            out: Vec::new(),
            out_at: 0,
            done: false,
            produced: 0,
            limit,
            unpacked: std::rc::Rc::clone(&self.unpacked),
            failure: None,
        }))
    }
}

/// A part being read, inflated a block at a time and never past its bound.
pub(super) struct Part<'a> {
    pub(super) name: String,
    data: &'a [u8],
    at: usize,
    inflate: Option<Box<InflateState>>,
    out: Vec<u8>,
    out_at: usize,
    done: bool,
    produced: u64,
    limit: u64,
    unpacked: std::rc::Rc<std::cell::Cell<u64>>,
    /// Why reading it failed, for a person: the XML reader sees only that it did.
    pub(super) failure: Option<String>,
}

impl Part<'_> {
    fn fail(&mut self, message: String) -> std::io::Error {
        let error = std::io::Error::other(message.clone());
        self.failure = Some(message);
        error
    }

    fn count(&mut self, n: usize) -> Result<(), std::io::Error> {
        let n = n as u64;
        self.produced += n;
        let total = self.unpacked.get() + n;
        self.unpacked.set(total);
        if self.produced > self.limit || total > MAX_UNPACKED {
            return Err(self.fail(TOO_BIG.to_owned()));
        }
        Ok(())
    }
}

impl Read for Part<'_> {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        if buf.is_empty() {
            return Ok(0);
        }
        let Some(state) = self.inflate.as_mut() else {
            // Stored.
            let rest = &self.data[self.at..];
            let n = rest.len().min(buf.len());
            buf[..n].copy_from_slice(&rest[..n]);
            self.at += n;
            self.count(n)?;
            return Ok(n);
        };
        if self.out_at == self.out.len() {
            if self.done {
                return Ok(0);
            }
            self.out.resize(64 * 1024, 0);
            self.out_at = 0;
            let mut written = 0;
            while written == 0 {
                let result = miniz_oxide::inflate::stream::inflate(
                    state,
                    &self.data[self.at..],
                    &mut self.out,
                    MZFlush::None,
                );
                self.at += result.bytes_consumed;
                written = result.bytes_written;
                match result.status {
                    Ok(MZStatus::StreamEnd) => {
                        self.done = true;
                        break;
                    }
                    Err(MZError::Buf) if written == 0 => {
                        let message = damaged(&format!("its part {} is cut short", self.name));
                        return Err(self.fail(message));
                    }
                    Ok(_) | Err(MZError::Buf) => {}
                    Err(_) => {
                        let message = damaged(&format!("its part {} doesn't inflate", self.name));
                        return Err(self.fail(message));
                    }
                }
                if written == 0 && result.bytes_consumed == 0 {
                    let message = damaged(&format!("its part {} is cut short", self.name));
                    return Err(self.fail(message));
                }
            }
            self.out.truncate(written);
            self.count(written)?;
        }
        let n = (self.out.len() - self.out_at).min(buf.len());
        buf[..n].copy_from_slice(&self.out[self.out_at..self.out_at + n]);
        self.out_at += n;
        Ok(n)
    }
}
