//! ABIF (AB1) Sanger traces, as Applied Biosystems' "ABIF File Format" specification
//! describes them: a directory of tagged entries, of which a chromatogram needs the four
//! analyzed traces (`DATA` 9–12), the order of their bases (`FWO_` 1), the base calls
//! (`PBAS` 2, else 1), where each call's peak is (`PLOC` 2, else 1) and each call's quality
//! (`PCON` 2, else 1).
//!
//! The file is big-endian. It starts `ABIF`, a version, then one directory entry (`tdir`)
//! that says where the directory is and how many entries it has. Each entry is 28 bytes:
//! its name (4 bytes), number (i32), element type (i16), element size (i16), number of
//! elements (i32), data size (i32) and data offset (i32), then a handle. Data of 4 bytes or
//! fewer is kept in the offset field itself. Every size and offset is checked against the
//! file before it's read. Some old files have a 128-byte `MacBinary` header before `ABIF`;
//! offsets are then from its end.

use serde::Serialize;

/// The most bytes an AB1 file may be to be read: traces are a few hundred kilobytes.
pub const MAX_BYTES: usize = 32 * 1024 * 1024;
/// The most points a trace may have.
pub const MAX_POINTS: usize = 500_000;
/// The most base calls.
pub const MAX_BASES: usize = 100_000;
/// The most directory entries read. AB1 files have a few hundred.
pub const MAX_ENTRIES: usize = 65_536;

/// What a chromatogram preview draws.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Chromatogram {
    /// The sample's name (`SMPL` 1), if it has one.
    pub sample: Option<String>,
    /// The instrument's model and name (`MODL` 1, `MCHN` 1), if recorded.
    pub instrument: Option<String>,
    /// The base calls, as recorded (IUPAC codes, `N` for none).
    pub bases: String,
    /// Each call's peak, as a point index into the traces.
    pub peaks: Vec<u32>,
    /// Each call's quality value (Phred-scaled), if the file records them.
    pub quality: Option<Vec<u8>>,
    /// The analyzed traces of A, C, G and T, in that order, whatever order `FWO_` gives.
    pub traces: Traces,
    /// How many points each trace has.
    pub points: usize,
    /// Things worth saying that didn't stop the read: a trace cut short, peaks past the
    /// traces' end left out, and so on.
    pub problems: Vec<String>,
}

/// The four analyzed traces, one intensity per point each.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Traces {
    /// Adenine's trace.
    #[serde(rename = "A")]
    pub a: Vec<u16>,
    /// Cytosine's.
    #[serde(rename = "C")]
    pub c: Vec<u16>,
    /// Guanine's.
    #[serde(rename = "G")]
    pub g: Vec<u16>,
    /// Thymine's.
    #[serde(rename = "T")]
    pub t: Vec<u16>,
}

/// Where the directory entry that points to the directory is, after `ABIF` and a version.
const TDIR: usize = 6;
/// How long a directory entry is.
const ENTRY: usize = 28;
/// The `MacBinary` header some files written on old Macs start with.
const MACBINARY: usize = 128;

/// Element types, as the specification numbers them.
const BYTE: i16 = 1;
const CHAR: i16 = 2;
const WORD: i16 = 3;
const SHORT: i16 = 4;
const LONG: i16 = 5;
const PSTRING: i16 = 18;
const CSTRING: i16 = 19;

/// One directory entry.
struct Entry {
    name: [u8; 4],
    number: i32,
    element_type: i16,
    element_size: i16,
    elements: i32,
    data_size: i32,
    data_offset: i32,
    /// Where the entry itself is in the file, for data kept in its offset field.
    at: usize,
}

impl Entry {
    fn label(&self) -> String {
        format!("{} {}", String::from_utf8_lossy(&self.name), self.number)
    }
}

/// The file and its directory.
struct File<'a> {
    bytes: &'a [u8],
    /// Where `ABIF` is: 0, or 128 after a `MacBinary` header.
    base: usize,
    entries: Vec<Entry>,
}

/// Reads an AB1 file.
///
/// # Errors
///
/// A message for a person if it isn't an ABIF file, or lacks what a chromatogram needs.
pub fn read(bytes: &[u8]) -> Result<Chromatogram, String> {
    if bytes.len() > MAX_BYTES {
        return Err(format!(
            "It's {} bytes, bigger than the {} an AB1 preview reads; Sanger traces are a few \
             hundred kilobytes.",
            count(bytes.len()),
            count(MAX_BYTES)
        ));
    }
    let file = File::open(bytes)?;
    let mut problems = Vec::new();

    let order = base_order(&file, &mut problems)?;
    let (traces, points) = traces(&file, order, &mut problems)?;

    let sample = optional_text(&file, *b"SMPL", 1, &mut problems);
    let model = optional_text(&file, *b"MODL", 1, &mut problems);
    let machine = optional_text(&file, *b"MCHN", 1, &mut problems);
    let instrument = match (model, machine) {
        (Some(model), Some(machine)) if model != machine => Some(format!("{model} ({machine})")),
        (Some(model), _) => Some(model),
        (None, machine) => machine,
    };

    let (bases, peaks, quality) = calls(&file, points, &mut problems)?;

    Ok(Chromatogram {
        sample,
        instrument,
        bases,
        peaks,
        quality,
        traces,
        points,
        problems,
    })
}

impl<'a> File<'a> {
    fn open(bytes: &'a [u8]) -> Result<Self, String> {
        let base = if bytes.starts_with(b"ABIF") {
            0
        } else if bytes.get(MACBINARY..MACBINARY + 4) == Some(b"ABIF") {
            MACBINARY
        } else if bytes.is_empty() {
            return Err("The file is empty.".to_owned());
        } else {
            return Err("This isn't an AB1 file: it doesn't start with \"ABIF\".".to_owned());
        };
        let tdir_at = base + TDIR;
        let tdir = entry_at(bytes, tdir_at).ok_or_else(|| {
            format!(
                "This AB1 file is cut short: it's {} bytes, too few to hold its header.",
                count(bytes.len())
            )
        })?;
        let stride = usize::try_from(tdir.element_size)
            .ok()
            .filter(|size| *size >= ENTRY)
            .ok_or_else(|| {
                format!(
                    "This AB1 file's header says its directory entries are {} bytes long; \
                     they're {ENTRY}.",
                    tdir.element_size
                )
            })?;
        let entries = usize::try_from(tdir.elements).map_err(|_| {
            format!(
                "This AB1 file's header says its directory has {} entries.",
                tdir.elements
            )
        })?;
        if entries > MAX_ENTRIES {
            return Err(format!(
                "This AB1 file's header says its directory has {} entries, more than the {} \
                 a preview reads.",
                count(entries),
                count(MAX_ENTRIES)
            ));
        }
        let start = usize::try_from(tdir.data_offset)
            .ok()
            .and_then(|offset| offset.checked_add(base))
            .ok_or_else(|| {
                format!(
                    "This AB1 file's header says its directory is at byte {}.",
                    tdir.data_offset
                )
            })?;
        let end = entries
            .checked_mul(stride)
            .and_then(|length| length.checked_add(start))
            .filter(|end| *end <= bytes.len())
            .ok_or_else(|| {
                format!(
                    "This AB1 file is cut short or damaged: its directory of {} entries at byte \
                     {} runs past its end (it's {} bytes).",
                    count(entries),
                    count(start),
                    count(bytes.len())
                )
            })?;
        let mut list = Vec::with_capacity(entries);
        let mut at = start;
        while at < end {
            if let Some(entry) = entry_at(bytes, at) {
                list.push(entry);
            }
            at += stride;
        }
        Ok(Self {
            bytes,
            base,
            entries: list,
        })
    }

    fn find(&self, name: [u8; 4], number: i32) -> Option<&Entry> {
        self.entries
            .iter()
            .find(|entry| entry.name == name && entry.number == number)
    }

    /// An entry's data, checked against the file.
    fn data(&self, entry: &Entry) -> Result<&'a [u8], String> {
        let label = entry.label();
        let (Ok(elements), Ok(size)) = (
            usize::try_from(entry.elements),
            usize::try_from(entry.element_size),
        ) else {
            return Err(format!(
                "The file's {label} entry says it has {} elements of {} bytes.",
                entry.elements, entry.element_size
            ));
        };
        let length = elements.checked_mul(size).ok_or_else(|| {
            format!(
                "The file's {label} entry says it has {} elements of {size} bytes, more than \
                 any file holds.",
                count(elements)
            )
        })?;
        let data_size = usize::try_from(entry.data_size).unwrap_or(0);
        if length > data_size {
            return Err(format!(
                "The file's {label} entry says it has {} elements of {size} bytes, but only {} \
                 bytes of data.",
                count(elements),
                count(data_size)
            ));
        }
        let start = if data_size <= 4 {
            // Kept in the entry's own offset field, 20 bytes into it.
            entry.at + 20
        } else {
            usize::try_from(entry.data_offset)
                .ok()
                .and_then(|offset| offset.checked_add(self.base))
                .ok_or_else(|| {
                    format!(
                        "The file's {label} entry says its data is at byte {}.",
                        entry.data_offset
                    )
                })?
        };
        start
            .checked_add(length)
            .and_then(|end| self.bytes.get(start..end))
            .ok_or_else(|| {
                format!(
                    "The file is cut short or damaged: its {label} entry's {} bytes at byte {} \
                     run past its end (it's {} bytes).",
                    count(length),
                    count(start),
                    count(self.bytes.len())
                )
            })
    }

    /// An entry's data as integers, whatever integer type it's stored as.
    fn integers(&self, entry: &Entry) -> Result<Vec<i64>, String> {
        let data = self.data(entry)?;
        let width = match (entry.element_type, entry.element_size) {
            (BYTE | CHAR, 1) => 1,
            (WORD | SHORT, 2) => 2,
            (LONG, 4) => 4,
            _ => {
                return Err(format!(
                    "The file's {} entry holds elements of type {} and {} bytes, not integers.",
                    entry.label(),
                    entry.element_type,
                    entry.element_size
                ));
            }
        };
        Ok(data
            .chunks_exact(width)
            .map(|chunk| match (entry.element_type, chunk) {
                (BYTE, [byte]) => i64::from(i8::from_be_bytes([*byte])),
                (CHAR, [byte]) => i64::from(*byte),
                (WORD, [a, b]) => i64::from(u16::from_be_bytes([*a, *b])),
                (SHORT, [a, b]) => i64::from(i16::from_be_bytes([*a, *b])),
                (_, [a, b, c, d]) => i64::from(i32::from_be_bytes([*a, *b, *c, *d])),
                _ => 0,
            })
            .collect())
    }

    /// An entry's data as text: a pString, a cString or characters.
    fn text(&self, entry: &Entry) -> Result<String, String> {
        let data = self.data(entry)?;
        let raw = match entry.element_type {
            PSTRING => {
                let length = usize::from(data.first().copied().unwrap_or(0));
                data.get(1..)
                    .map_or(&[][..], |rest| &rest[..length.min(rest.len())])
            }
            CSTRING => data.split(|byte| *byte == 0).next().unwrap_or(&[]),
            _ => data,
        };
        let text = match std::str::from_utf8(raw) {
            Ok(text) => text.to_owned(),
            // Latin-1, as older instruments wrote.
            Err(_) => raw.iter().map(|byte| char::from(*byte)).collect(),
        };
        Ok(text
            .trim_matches(|c: char| c == '\0' || c.is_whitespace())
            .to_owned())
    }
}

fn entry_at(bytes: &[u8], at: usize) -> Option<Entry> {
    let raw = bytes.get(at..at.checked_add(ENTRY)?)?;
    let i32_at = |offset: usize| -> Option<i32> {
        Some(i32::from_be_bytes(
            raw.get(offset..offset + 4)?.try_into().ok()?,
        ))
    };
    let i16_at = |offset: usize| -> Option<i16> {
        Some(i16::from_be_bytes(
            raw.get(offset..offset + 2)?.try_into().ok()?,
        ))
    };
    Some(Entry {
        name: raw.get(0..4)?.try_into().ok()?,
        number: i32_at(4)?,
        element_type: i16_at(8)?,
        element_size: i16_at(10)?,
        elements: i32_at(12)?,
        data_size: i32_at(16)?,
        data_offset: i32_at(20)?,
        at,
    })
}

/// Which base each of `DATA` 9–12 (or 1–4) is, from `FWO_` 1.
fn base_order(file: &File, problems: &mut Vec<String>) -> Result<[u8; 4], String> {
    const USUAL: [u8; 4] = *b"GATC";
    let Some(entry) = file.find(*b"FWO_", 1) else {
        problems.push(
            "The file doesn't record which trace is which base (FWO_ 1), so they're taken in \
             the usual order: G, A, T, C."
                .to_owned(),
        );
        return Ok(USUAL);
    };
    let data = file.data(entry)?;
    let mut order = [0u8; 4];
    for (slot, byte) in order.iter_mut().zip(data) {
        *slot = byte.to_ascii_uppercase();
    }
    let mut sorted = order;
    sorted.sort_unstable();
    if data.len() < 4 || &sorted != b"ACGT" {
        return Err(format!(
            "The file's base order (FWO_ 1) is \"{}\", not the four bases A, C, G and T in \
             some order.",
            String::from_utf8_lossy(data)
        ));
    }
    Ok(order)
}

/// The four traces, in A, C, G, T order, and how many points they have.
fn traces(
    file: &File,
    order: [u8; 4],
    problems: &mut Vec<String>,
) -> Result<(Traces, usize), String> {
    let analyzed = (9..=12).all(|number| file.find(*b"DATA", number).is_some());
    let raw = (1..=4).all(|number| file.find(*b"DATA", number).is_some());
    let first = if analyzed {
        9
    } else if raw {
        problems.push(
            "The file has no analyzed traces (DATA 9–12), so its raw traces (DATA 1–4) are \
             drawn: they haven't been baselined, smoothed or corrected for dye mobility, so \
             they're noisier and the base calls' peaks may not sit on them."
                .to_owned(),
        );
        1
    } else {
        return Err(
            "The file has no traces to draw: neither analyzed (DATA 9–12) nor raw (DATA 1–4)."
                .to_owned(),
        );
    };

    let mut channels: [Vec<u16>; 4] = Default::default();
    let mut negative = 0usize;
    for (number, base) in (first..first + 4).zip(order) {
        let entry = file
            .find(*b"DATA", number)
            .ok_or_else(|| format!("The file has no DATA {number} trace."))?;
        if !matches!(entry.element_type, WORD | SHORT) {
            return Err(format!(
                "The file's DATA {number} trace holds elements of type {}, not 16-bit \
                 intensities.",
                entry.element_type
            ));
        }
        let values = file.integers(entry)?;
        let slot = match base {
            b'A' => 0,
            b'C' => 1,
            b'G' => 2,
            _ => 3,
        };
        channels[slot] = values
            .iter()
            .take(MAX_POINTS)
            .map(|value| {
                if *value < 0 {
                    negative += 1;
                }
                u16::try_from((*value).max(0)).unwrap_or(u16::MAX)
            })
            .collect();
        if values.len() > MAX_POINTS {
            problems.push(format!(
                "The DATA {number} trace has {} points; only the first {} are drawn.",
                count(values.len()),
                count(MAX_POINTS)
            ));
        }
    }
    if negative > 0 {
        problems.push(format!(
            "{} trace intensities were below zero and are drawn as zero.",
            count(negative)
        ));
    }
    let points = channels.iter().map(Vec::len).min().unwrap_or(0);
    let longest = channels.iter().map(Vec::len).max().unwrap_or(0);
    if longest != points {
        problems.push(format!(
            "The four traces aren't the same length ({} to {} points); each is cut to the \
             shortest.",
            count(points),
            count(longest)
        ));
        for channel in &mut channels {
            channel.truncate(points);
        }
    }
    if points == 0 {
        return Err("The file's traces are empty.".to_owned());
    }
    let [a, c, g, t] = channels;
    Ok((Traces { a, c, g, t }, points))
}

/// An entry, numbered 2 if there is one, else 1: the base caller's, else the user's edit.
fn called<'f>(file: &'f File, name: [u8; 4]) -> Option<&'f Entry> {
    file.find(name, 2).or_else(|| file.find(name, 1))
}

/// The base calls, each call's peak and each call's quality value, if recorded.
struct Calls {
    bases: Vec<u8>,
    peaks: Option<Vec<i64>>,
    quality: Option<Vec<u8>>,
}

/// The base calls as text, their peaks as points on the traces, and their quality values.
type Placed = (String, Vec<u32>, Option<Vec<u8>>);

/// The base calls, their peaks and their quality values, lined up and on the traces.
fn calls(file: &File, points: usize, problems: &mut Vec<String>) -> Result<Placed, String> {
    let Some(mut calls) = read_calls(file, problems)? else {
        return Ok((String::new(), Vec::new(), None));
    };
    line_up(&mut calls, problems);
    let Calls {
        bases,
        peaks,
        quality,
    } = calls;
    let Some(peaks) = peaks else {
        return Ok((
            bases.into_iter().map(char::from).collect(),
            Vec::new(),
            quality,
        ));
    };

    // Calls whose peaks aren't on the traces can't be drawn.
    let mut kept_bases = Vec::with_capacity(bases.len());
    let mut kept_peaks = Vec::with_capacity(bases.len());
    let mut kept_quality = quality.as_ref().map(|_| Vec::with_capacity(bases.len()));
    let mut outside = 0usize;
    for (index, peak) in peaks.iter().enumerate() {
        let on_trace = u32::try_from(*peak)
            .ok()
            .filter(|peak| usize::try_from(*peak).is_ok_and(|peak| peak < points));
        let Some(peak) = on_trace else {
            outside += 1;
            continue;
        };
        kept_peaks.push(peak);
        if let Some(base) = bases.get(index) {
            kept_bases.push(*base);
        }
        if let (Some(kept), Some(value)) = (
            &mut kept_quality,
            quality.as_ref().and_then(|quality| quality.get(index)),
        ) {
            kept.push(*value);
        }
    }
    if outside > 0 {
        problems.push(format!(
            "{} base calls have peaks past the traces' end (they have {} points) and are left \
             out.",
            count(outside),
            count(points)
        ));
    }
    Ok((
        kept_bases.into_iter().map(char::from).collect(),
        kept_peaks,
        kept_quality,
    ))
}

/// `PBAS`, `PLOC` and `PCON`, as recorded; none if there are no base calls.
fn read_calls(file: &File, problems: &mut Vec<String>) -> Result<Option<Calls>, String> {
    let Some(pbas) = called(file, *b"PBAS") else {
        problems.push(
            "The file has no base calls (PBAS 2 or 1); only its traces are drawn.".to_owned(),
        );
        return Ok(None);
    };
    let raw = file.data(pbas)?;
    let mut odd = 0usize;
    let bases: Vec<u8> = raw
        .iter()
        .take(MAX_BASES)
        .map(|byte| {
            if byte.is_ascii_alphabetic() || *byte == b'-' {
                *byte
            } else {
                odd += 1;
                b'N'
            }
        })
        .collect();
    if raw.len() > MAX_BASES {
        problems.push(format!(
            "The file has {} base calls; only the first {} are shown.",
            count(raw.len()),
            count(MAX_BASES)
        ));
    }
    if odd > 0 {
        problems.push(format!(
            "{} base calls weren't letters and are shown as N.",
            count(odd)
        ));
    }

    let peaks = if let Some(entry) = called(file, *b"PLOC") {
        // Peak locations are stored as 16-bit shorts; past 32,767 the sign bit is set, so
        // they're read unsigned.
        let unsigned = entry.element_type == SHORT;
        Some(
            file.integers(entry)?
                .into_iter()
                .map(|value| {
                    if unsigned && value < 0 {
                        value + 65_536
                    } else {
                        value
                    }
                })
                .collect(),
        )
    } else {
        problems.push(
            "The file doesn't say where the base calls' peaks are (PLOC 2 or 1), so the calls \
             can't be placed on the traces."
                .to_owned(),
        );
        None
    };
    let quality = called(file, *b"PCON").and_then(|entry| match file.data(entry) {
        Ok(data) => Some(data.to_vec()),
        Err(message) => {
            problems.push(format!("{message} Quality values aren't shown."));
            None
        }
    });
    Ok(Some(Calls {
        bases,
        peaks,
        quality,
    }))
}

/// Cuts the calls, peaks and quality values to the shortest, so each call has one of each.
fn line_up(calls: &mut Calls, problems: &mut Vec<String>) {
    let peaks = calls.peaks.as_ref().map(Vec::len);
    let quality = calls.quality.as_ref().map(Vec::len);
    let shortest = [Some(calls.bases.len()), peaks, quality]
        .into_iter()
        .flatten()
        .min()
        .unwrap_or(0);
    let mismatched = calls.bases.len() != shortest
        || peaks.is_some_and(|n| n != shortest)
        || quality.is_some_and(|n| n != shortest);
    if !mismatched {
        return;
    }
    let mut parts = vec![format!("{} base calls", count(calls.bases.len()))];
    if let Some(peaks) = peaks {
        parts.push(format!("{} peak locations", count(peaks)));
    }
    if let Some(quality) = quality {
        parts.push(format!("{} quality values", count(quality)));
    }
    problems.push(format!(
        "The file has {}; they're cut to the shortest, {}.",
        parts.join(", "),
        count(shortest)
    ));
    calls.bases.truncate(shortest);
    if let Some(peaks) = &mut calls.peaks {
        peaks.truncate(shortest);
    }
    if let Some(quality) = &mut calls.quality {
        quality.truncate(shortest);
    }
}

/// A text entry that's nice to have: a problem, not an error, if it can't be read.
fn optional_text(
    file: &File,
    name: [u8; 4],
    number: i32,
    problems: &mut Vec<String>,
) -> Option<String> {
    let entry = file.find(name, number)?;
    match file.text(entry) {
        Ok(text) if text.is_empty() => None,
        Ok(text) => Some(text),
        Err(message) => {
            problems.push(message);
            None
        }
    }
}

/// A count with thousands separators, as people read them.
fn count(value: usize) -> String {
    let digits = value.to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (index, digit) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            out.push(',');
        }
        out.push(digit);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A directory entry to build: name, number, element type, element size, data.
    type Raw = (&'static [u8; 4], i32, i16, i16, Vec<u8>);

    /// Builds an AB1 file from entries.
    fn build(entries: &[Raw]) -> Vec<u8> {
        let mut file = vec![0u8; 128];
        file[0..4].copy_from_slice(b"ABIF");
        file[4..6].copy_from_slice(&101i16.to_be_bytes());
        // Data first, then the directory.
        let mut placed = Vec::new();
        for (_, _, _, _, data) in entries {
            if data.len() > 4 {
                placed.push(Some(file.len()));
                file.extend_from_slice(data);
            } else {
                placed.push(None);
            }
        }
        let directory = file.len();
        for ((name, number, kind, size, data), at) in entries.iter().zip(&placed) {
            let mut entry = Vec::new();
            entry.extend_from_slice(*name);
            entry.extend_from_slice(&number.to_be_bytes());
            entry.extend_from_slice(&kind.to_be_bytes());
            entry.extend_from_slice(&size.to_be_bytes());
            let elements = i32::try_from(data.len()).unwrap() / i32::from(*size);
            entry.extend_from_slice(&elements.to_be_bytes());
            entry.extend_from_slice(&i32::try_from(data.len()).unwrap().to_be_bytes());
            if let Some(at) = at {
                entry.extend_from_slice(&i32::try_from(*at).unwrap().to_be_bytes());
            } else {
                let mut inline = [0u8; 4];
                inline[..data.len()].copy_from_slice(data);
                entry.extend_from_slice(&inline);
            }
            entry.extend_from_slice(&0i32.to_be_bytes());
            file.extend_from_slice(&entry);
        }
        // The tdir entry.
        let mut tdir = Vec::new();
        tdir.extend_from_slice(b"tdir");
        tdir.extend_from_slice(&1i32.to_be_bytes());
        tdir.extend_from_slice(&1023i16.to_be_bytes());
        tdir.extend_from_slice(&28i16.to_be_bytes());
        tdir.extend_from_slice(&i32::try_from(entries.len()).unwrap().to_be_bytes());
        tdir.extend_from_slice(&i32::try_from(entries.len() * 28).unwrap().to_be_bytes());
        tdir.extend_from_slice(&i32::try_from(directory).unwrap().to_be_bytes());
        tdir.extend_from_slice(&0i32.to_be_bytes());
        file[6..34].copy_from_slice(&tdir);
        file
    }

    fn shorts(values: &[i16]) -> Vec<u8> {
        values
            .iter()
            .flat_map(|value| value.to_be_bytes())
            .collect()
    }

    fn pstring(text: &str) -> Vec<u8> {
        let mut out = vec![u8::try_from(text.len()).unwrap()];
        out.extend_from_slice(text.as_bytes());
        out
    }

    /// A small, well-formed trace: 10 points, three calls.
    fn small() -> Vec<Raw> {
        vec![
            (b"FWO_", 1, CHAR, 1, b"GATC".to_vec()),
            (
                b"DATA",
                9,
                SHORT,
                2,
                shorts(&[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]),
            ),
            (
                b"DATA",
                10,
                SHORT,
                2,
                shorts(&[10, 20, 30, 40, 50, 60, 70, 80, 90, 100]),
            ),
            (
                b"DATA",
                11,
                SHORT,
                2,
                shorts(&[0, 0, 0, 0, 0, 0, 0, 0, 0, 500]),
            ),
            (
                b"DATA",
                12,
                SHORT,
                2,
                shorts(&[-5, 1, 1, 1, 1, 1, 1, 1, 1, 1]),
            ),
            (b"PBAS", 2, CHAR, 1, b"GAT".to_vec()),
            (b"PLOC", 2, SHORT, 2, shorts(&[1, 4, 9])),
            (b"PCON", 2, CHAR, 1, vec![20, 30, 40]),
            (b"SMPL", 1, PSTRING, 1, pstring("Clone 7 fwd")),
            (b"MODL", 1, CHAR, 1, b"3730".to_vec()),
        ]
    }

    #[test]
    fn reads_traces_in_acgt_order_whatever_fwo_says() {
        let reading = read(&build(&small())).unwrap();
        // FWO_ GATC: DATA 9 is G, 10 is A, 11 is T, 12 is C.
        assert_eq!(reading.traces.g, vec![1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
        assert_eq!(reading.traces.a[9], 100);
        assert_eq!(reading.traces.t[9], 500);
        assert_eq!(
            reading.traces.c[0], 0,
            "negative intensities are drawn as zero"
        );
        assert_eq!(reading.points, 10);
        assert_eq!(reading.bases, "GAT");
        assert_eq!(reading.peaks, vec![1, 4, 9]);
        assert_eq!(reading.quality, Some(vec![20, 30, 40]));
        assert_eq!(reading.sample.as_deref(), Some("Clone 7 fwd"));
        assert_eq!(reading.instrument.as_deref(), Some("3730"));
        assert_eq!(reading.problems.len(), 1, "{:?}", reading.problems);
        assert!(reading.problems[0].contains("below zero"));
    }

    #[test]
    fn json_names_the_traces_by_base() {
        let json = crate::to_json(read(&build(&small())));
        let value: serde_json::Value = serde_json::from_slice(&json).unwrap();
        assert_eq!(value["traces"]["G"][9], 10);
        assert_eq!(value["bases"], "GAT");
    }

    #[test]
    fn drops_calls_whose_peaks_are_past_the_traces() {
        let mut entries = small();
        entries[6] = (b"PLOC", 2, SHORT, 2, shorts(&[1, 4, 12]));
        let reading = read(&build(&entries)).unwrap();
        assert_eq!(reading.bases, "GA");
        assert_eq!(reading.peaks, vec![1, 4]);
        assert_eq!(reading.quality, Some(vec![20, 30]));
        assert!(
            reading
                .problems
                .iter()
                .any(|p| p.contains("past the traces' end")),
            "{:?}",
            reading.problems
        );
    }

    #[test]
    fn cuts_mismatched_calls_to_the_shortest() {
        let mut entries = small();
        entries[7] = (b"PCON", 2, CHAR, 1, vec![20, 30]);
        let reading = read(&build(&entries)).unwrap();
        assert_eq!(reading.bases, "GA");
        assert_eq!(reading.peaks, vec![1, 4]);
        assert!(
            reading
                .problems
                .iter()
                .any(|p| p.contains("3 base calls, 3 peak locations, 2 quality values")),
            "{:?}",
            reading.problems
        );
    }

    #[test]
    fn missing_quality_is_none() {
        let mut entries = small();
        entries.remove(7);
        let reading = read(&build(&entries)).unwrap();
        assert_eq!(reading.quality, None);
        assert_eq!(reading.bases, "GAT");
    }

    #[test]
    fn falls_back_to_raw_traces_and_says_so() {
        let entries: Vec<_> = small()
            .into_iter()
            .map(|(name, number, kind, size, data)| {
                let number = if name == b"DATA" { number - 8 } else { number };
                (name, number, kind, size, data)
            })
            .collect();
        let reading = read(&build(&entries)).unwrap();
        assert_eq!(reading.traces.g[9], 10);
        assert!(reading.problems.iter().any(|p| p.contains("raw traces")));
    }

    #[test]
    fn no_traces_is_an_error() {
        let entries: Vec<_> = small().into_iter().filter(|e| e.0 != b"DATA").collect();
        let error = read(&build(&entries)).unwrap_err();
        assert!(error.contains("no traces"), "{error}");
    }

    #[test]
    fn missing_base_order_assumes_gatc() {
        let entries: Vec<_> = small().into_iter().filter(|e| e.0 != b"FWO_").collect();
        let reading = read(&build(&entries)).unwrap();
        assert_eq!(reading.traces.g[9], 10);
        assert!(reading.problems.iter().any(|p| p.contains("FWO_")));
    }

    #[test]
    fn a_base_order_that_isnt_acgt_is_an_error() {
        let mut entries = small();
        entries[0] = (b"FWO_", 1, CHAR, 1, b"GGTC".to_vec());
        let error = read(&build(&entries)).unwrap_err();
        assert!(error.contains("\"GGTC\""), "{error}");
    }

    #[test]
    fn peak_locations_past_32767_are_read_unsigned() {
        let mut entries = small();
        let long: Vec<i16> = (0..40_000)
            .map(|i| i16::try_from(i % 100).unwrap())
            .collect();
        for entry in entries.iter_mut().filter(|e| e.0 == b"DATA") {
            entry.4 = shorts(&long);
        }
        entries[6] = (b"PLOC", 2, SHORT, 2, shorts(&[1, 4, -30_000]));
        let reading = read(&build(&entries)).unwrap();
        assert_eq!(reading.peaks, vec![1, 4, 35_536]);
    }

    #[test]
    fn refuses_what_isnt_abif() {
        assert!(read(b"").unwrap_err().contains("empty"));
        assert!(
            read(b"GIF89a....")
                .unwrap_err()
                .contains("doesn't start with \"ABIF\"")
        );
        assert!(read(b"ABIF\x00\x65").unwrap_err().contains("cut short"));
        assert!(
            read(&vec![0u8; MAX_BYTES + 1])
                .unwrap_err()
                .contains("bigger than")
        );
    }

    #[test]
    fn reads_past_a_macbinary_header() {
        let file = build(&small());
        // Offsets in the file are from ABIF, so shifting the whole file keeps them right.
        let mut wrapped = vec![0u8; 128];
        wrapped.extend_from_slice(&file);
        let reading = read(&wrapped).unwrap();
        assert_eq!(reading.bases, "GAT");
    }

    #[test]
    fn absurd_directories_are_errors_not_panics() {
        let good = build(&small());
        // Directory count: negative, huge, and past the end.
        for count in [-1i32, i32::MAX, 1000] {
            let mut file = good.clone();
            file[18..22].copy_from_slice(&count.to_be_bytes());
            assert!(read(&file).is_err(), "count {count}");
        }
        // Directory offset: negative and past the end.
        for offset in [-1i32, i32::MAX, i32::try_from(good.len()).unwrap()] {
            let mut file = good.clone();
            file[26..30].copy_from_slice(&offset.to_be_bytes());
            assert!(read(&file).is_err(), "offset {offset}");
        }
        // An entry size smaller than an entry.
        let mut file = good.clone();
        file[16..18].copy_from_slice(&4i16.to_be_bytes());
        assert!(read(&file).is_err());
    }

    #[test]
    fn absurd_entries_are_errors_or_problems_not_panics() {
        let good = build(&small());
        let directory =
            usize::try_from(i32::from_be_bytes(good[26..30].try_into().unwrap())).unwrap();
        // Every field of every entry, set to values that overflow or point nowhere.
        for entry in 0..small().len() {
            for field in [12usize, 16, 20] {
                for value in [-1i32, i32::MAX, i32::MIN, 0x4000_0000] {
                    let mut file = good.clone();
                    let at = directory + entry * 28 + field;
                    file[at..at + 4].copy_from_slice(&value.to_be_bytes());
                    let _ = read(&file);
                }
            }
            for value in [-1i16, 0, i16::MAX] {
                let mut file = good.clone();
                let at = directory + entry * 28 + 10;
                file[at..at + 2].copy_from_slice(&value.to_be_bytes());
                let _ = read(&file);
            }
        }
        // Truncated anywhere.
        for length in (0..good.len()).step_by(7) {
            let _ = read(&good[..length]);
        }
    }

    #[test]
    fn a_trace_past_the_end_names_its_entry() {
        let good = build(&small());
        let directory =
            usize::try_from(i32::from_be_bytes(good[26..30].try_into().unwrap())).unwrap();
        let mut file = good.clone();
        // DATA 9 is entry 1; its offset field is 20 bytes in.
        let at = directory + 28 + 20;
        file[at..at + 4].copy_from_slice(&i32::try_from(good.len()).unwrap().to_be_bytes());
        let error = read(&file).unwrap_err();
        assert!(error.contains("DATA 9"), "{error}");
        assert!(error.contains("past its end"), "{error}");
    }

    #[test]
    fn counts_have_separators() {
        assert_eq!(count(0), "0");
        assert_eq!(count(999), "999");
        assert_eq!(count(1000), "1,000");
        assert_eq!(count(33_554_432), "33,554,432");
    }
}
