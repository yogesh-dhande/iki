//! Walking a TIFF's IFDs (TIFF 6.0, and BigTIFF) from the ranges the page has read,
//! asking for the ranges it hasn't.

use std::collections::BTreeSet;

use super::{
    Image, Layout, MAX_CHUNKS, MAX_DESCRIPTION, MAX_IFDS, MAX_TILE_PIXELS, Pixels, Span, Step,
    Stored, ome,
};

/// The least a range asked for is, where the file has that much: IFDs are small and
/// usually near each other, so one read often holds several.
const MIN_READ: u64 = 64 * 1024;
/// The most one range asked for may be.
const MAX_READ: u64 = 8 * 1024 * 1024;
/// The most ranges asked for at once.
const MAX_NEEDS: usize = 16;
/// The most tags an IFD may have: TIFF defines fewer than a hundred.
const MAX_ENTRIES: u64 = 4_096;
/// The widest or tallest image listed (2^31 pixels).
const MAX_SIDE: u64 = 1 << 31;
/// The most bytes an uncompressed strip listed in place of one too large may be.
const STRIP_BYTES: u64 = 4 * 1024 * 1024;

/// The ranges given, merged and in order.
struct File {
    size: u64,
    segments: Vec<(u64, Vec<u8>)>,
}

impl File {
    fn parse(input: &[u8]) -> Result<Self, String> {
        let bad = || "The ranges read from the file are cut short.".to_owned();
        let header = input.get(..12).ok_or_else(bad)?;
        let size = le_u64(&header[..8]);
        let mut ranges: Vec<(u64, &[u8])> = Vec::new();
        let mut at = 12usize;
        while at < input.len() {
            let head = input.get(at..at + 12).ok_or_else(bad)?;
            let offset = le_u64(&head[..8]);
            let length = le_u32(&head[8..12]) as usize;
            let bytes = input.get(at + 12..at + 12 + length).ok_or_else(bad)?;
            if offset
                .checked_add(length as u64)
                .is_none_or(|end| end > size)
            {
                return Err("A range read from the file is past its end.".to_owned());
            }
            if length > 0 {
                ranges.push((offset, bytes));
            }
            at += 12 + length;
        }
        ranges.sort_by_key(|r| r.0);
        let mut segments: Vec<(u64, Vec<u8>)> = Vec::new();
        for (offset, bytes) in ranges {
            if let Some((start, held)) = segments.last_mut() {
                let end = *start + held.len() as u64;
                if offset <= end {
                    let skip = usize::try_from(end - offset).unwrap_or(usize::MAX);
                    if skip < bytes.len() {
                        held.extend_from_slice(&bytes[skip..]);
                    }
                    continue;
                }
            }
            segments.push((offset, bytes.to_vec()));
        }
        Ok(Self { size, segments })
    }

    /// `length` bytes at `offset`, if a range held them all.
    fn get(&self, offset: u64, length: u64) -> Option<&[u8]> {
        let at = self.segments.partition_point(|(start, _)| *start <= offset);
        let (start, bytes) = self.segments.get(at.checked_sub(1)?)?;
        let from = usize::try_from(offset - start).ok()?;
        let to = from.checked_add(usize::try_from(length).ok()?)?;
        bytes.get(from..to)
    }
}

fn le_u32(b: &[u8]) -> u32 {
    let mut a = [0u8; 4];
    a.copy_from_slice(&b[..4]);
    u32::from_le_bytes(a)
}

fn le_u64(b: &[u8]) -> u64 {
    let mut a = [0u8; 8];
    a.copy_from_slice(&b[..8]);
    u64::from_le_bytes(a)
}

/// Reads integers in the file's byte order.
#[derive(Clone, Copy)]
struct Order {
    little: bool,
}

impl Order {
    fn u16(self, b: &[u8]) -> u16 {
        let a = [b[0], b[1]];
        if self.little {
            u16::from_le_bytes(a)
        } else {
            u16::from_be_bytes(a)
        }
    }

    fn u32(self, b: &[u8]) -> u32 {
        let a = [b[0], b[1], b[2], b[3]];
        if self.little {
            u32::from_le_bytes(a)
        } else {
            u32::from_be_bytes(a)
        }
    }

    fn u64(self, b: &[u8]) -> u64 {
        let mut a = [0u8; 8];
        a.copy_from_slice(&b[..8]);
        if self.little {
            u64::from_le_bytes(a)
        } else {
            u64::from_be_bytes(a)
        }
    }
}

/// A field type's size in bytes, for the types TIFF and BigTIFF define.
fn type_size(kind: u16) -> Option<u64> {
    Some(match kind {
        1 | 2 | 6 | 7 => 1,
        3 | 8 => 2,
        4 | 9 | 11 | 13 => 4,
        5 | 10 | 12 | 16 | 17 | 18 => 8,
        _ => return None,
    })
}

/// One IFD entry.
#[derive(Clone)]
struct Entry {
    tag: u16,
    kind: u16,
    count: u64,
    /// Its value field (4 bytes, or 8 in BigTIFF), and where in the file it is.
    field: [u8; 8],
    field_at: u64,
}

/// One IFD's entries, and the next IFD's offset.
struct Ifd {
    entries: Vec<Entry>,
    next: u64,
}

impl Ifd {
    fn entry(&self, tag: u16) -> Option<&Entry> {
        self.entries.iter().find(|e| e.tag == tag)
    }
}

/// Walking the file, gathering the ranges it needs.
struct Walk {
    file: File,
    order: Order,
    big: bool,
    needs: Vec<Span>,
    walked: usize,
    truncated: bool,
}

/// One IFD's image, and what's needed to place it.
struct Found {
    index: usize,
    stored: Stored,
    subfile: u64,
    levels: Vec<Stored>,
}

/// Bytes at a place, as read so far.
enum Got<T> {
    Have(T),
    Wait,
}

impl Walk {
    /// Asks for `length` bytes at `offset`: at least [`MIN_READ`], to the file's end.
    fn need(&mut self, offset: u64, length: u64) {
        let length = length
            .max(MIN_READ)
            .min(self.file.size.saturating_sub(offset));
        self.needs.push(Span { offset, length });
    }

    /// The bytes at a place, or `Wait` (having asked for them) if they aren't read yet.
    fn bytes(&mut self, offset: u64, length: u64) -> Result<Got<Vec<u8>>, String> {
        if offset
            .checked_add(length)
            .is_none_or(|end| end > self.file.size)
        {
            return Err(format!(
                "Its structure points past the end of the file (to byte {}).",
                offset.saturating_add(length)
            ));
        }
        if let Some(bytes) = self.file.get(offset, length) {
            return Ok(Got::Have(bytes.to_vec()));
        }
        self.need(offset, length);
        Ok(Got::Wait)
    }

    fn header(&mut self) -> Result<Got<u64>, String> {
        let Got::Have(head) = self.bytes(0, 16.min(self.file.size))? else {
            return Ok(Got::Wait);
        };
        let little = match head.get(..2) {
            Some(b"II") => true,
            Some(b"MM") => false,
            _ => return Err("It isn't a TIFF: it doesn't start with II or MM.".to_owned()),
        };
        let order = Order { little };
        if head.len() < 8 {
            return Err("It isn't a TIFF: it's too short.".to_owned());
        }
        let first = match order.u16(&head[2..4]) {
            42 => u64::from(order.u32(&head[4..8])),
            43 => {
                if head.len() < 16 || order.u16(&head[4..6]) != 8 || order.u16(&head[6..8]) != 0 {
                    return Err("Its BigTIFF header is broken.".to_owned());
                }
                self.big = true;
                order.u64(&head[8..16])
            }
            _ => return Err("It isn't a TIFF: its header's version isn't 42 or 43.".to_owned()),
        };
        self.order = order;
        Ok(Got::Have(first))
    }

    fn ifd(&mut self, offset: u64) -> Result<Got<Ifd>, String> {
        let (count_size, entry_size, next_size) = if self.big { (8, 20, 8) } else { (2, 12, 4) };
        let order = self.order;
        let Got::Have(head) = self.bytes(offset, count_size)? else {
            return Ok(Got::Wait);
        };
        let count = if self.big {
            order.u64(&head)
        } else {
            u64::from(order.u16(&head))
        };
        if count > MAX_ENTRIES {
            return Err(format!(
                "Its IFD at byte {offset} says it has {count} tags, far more than a TIFF has."
            ));
        }
        let length = count_size + count * entry_size + next_size;
        let big = self.big;
        let Got::Have(all) = self.bytes(offset, length)? else {
            return Ok(Got::Wait);
        };
        let mut entries = Vec::with_capacity(usize::try_from(count).unwrap_or(0));
        for i in 0..count {
            let at = usize::try_from(count_size + i * entry_size).unwrap_or(usize::MAX);
            let e = &all[at..at + usize::try_from(entry_size).unwrap_or(0)];
            let mut field = [0u8; 8];
            let (count, value) = if big {
                (order.u64(&e[4..12]), &e[12..20])
            } else {
                (u64::from(order.u32(&e[4..8])), &e[8..12])
            };
            field[..value.len()].copy_from_slice(value);
            entries.push(Entry {
                tag: order.u16(&e[0..2]),
                kind: order.u16(&e[2..4]),
                count,
                field,
                field_at: offset + count_size + i * entry_size + if big { 12 } else { 8 },
            });
        }
        let at = usize::try_from(count_size + count * entry_size).unwrap_or(usize::MAX);
        let next = if big {
            order.u64(&all[at..at + 8])
        } else {
            u64::from(order.u32(&all[at..at + 4]))
        };
        Ok(Got::Have(Ifd { entries, next }))
    }

    /// Where an entry's value is, and how many bytes: in its field, or at its offset.
    fn value_span(&self, e: &Entry) -> Result<Option<(u64, u64)>, String> {
        let Some(size) = type_size(e.kind) else {
            return Ok(None);
        };
        let total = size.checked_mul(e.count).ok_or_else(|| too_many(e))?;
        let field = if self.big { 8 } else { 4 };
        if total <= field {
            return Ok(Some((e.field_at, total)));
        }
        let offset = if self.big {
            self.order.u64(&e.field)
        } else {
            u64::from(self.order.u32(&e.field[..4]))
        };
        Ok(Some((offset, total)))
    }

    /// An entry's values as integers, or `None` (and a reason) if they aren't integers.
    fn integers(&mut self, e: &Entry, most: u64) -> Result<Got<Vec<u64>>, String> {
        if e.count > most {
            return Err(too_many(e));
        }
        let Some((offset, length)) = self.value_span(e)? else {
            return Err(format!(
                "Its tag {} has a type ({}) TIFF doesn't define.",
                e.tag, e.kind
            ));
        };
        let order = self.order;
        let kind = e.kind;
        let tag = e.tag;
        let field_at = e.field_at;
        let field = e.field;
        let bytes: Vec<u8> = if offset == field_at {
            field[..usize::try_from(length).unwrap_or(0)].to_vec()
        } else {
            match self.bytes(offset, length)? {
                Got::Have(b) => b,
                Got::Wait => return Ok(Got::Wait),
            }
        };
        let values = match kind {
            1 | 7 => bytes.iter().map(|&b| u64::from(b)).collect(),
            3 => bytes
                .chunks_exact(2)
                .map(|b| u64::from(order.u16(b)))
                .collect(),
            4 | 13 => bytes
                .chunks_exact(4)
                .map(|b| u64::from(order.u32(b)))
                .collect(),
            16 | 18 => bytes.chunks_exact(8).map(|b| order.u64(b)).collect(),
            _ => {
                return Err(format!(
                    "Its tag {tag} should hold whole numbers, but its type ({kind}) doesn't."
                ));
            }
        };
        Ok(Got::Have(values))
    }

    fn integer(&mut self, ifd: &Ifd, tag: u16) -> Result<Got<Option<u64>>, String> {
        let Some(e) = ifd.entry(tag).cloned() else {
            return Ok(Got::Have(None));
        };
        Ok(match self.integers(&e, 64)? {
            Got::Have(v) => Got::Have(v.first().copied()),
            Got::Wait => Got::Wait,
        })
    }
}

fn too_many(e: &Entry) -> String {
    format!(
        "Its tag {} says it holds {} values, more than a preview reads.",
        e.tag, e.count
    )
}

/// The ranges needed, merged where near each other and never over [`MAX_READ`].
fn merge(mut needs: Vec<Span>) -> Vec<Span> {
    needs.sort_by_key(|s| s.offset);
    let mut merged: Vec<Span> = Vec::new();
    for span in needs {
        if let Some(last) = merged.last_mut() {
            let end = last.offset + last.length;
            let new_end = (span.offset + span.length).max(end);
            if span.offset <= end + MIN_READ && new_end - last.offset <= MAX_READ {
                last.length = new_end - last.offset;
                continue;
            }
        }
        merged.push(span);
    }
    merged.truncate(MAX_NEEDS);
    merged
}

pub(super) fn layout(input: &[u8]) -> Result<Step, String> {
    let file = File::parse(input)?;
    if file.size < 8 {
        return Err("It isn't a TIFF: it's too short.".to_owned());
    }
    let mut walk = Walk {
        file,
        order: Order { little: true },
        big: false,
        needs: Vec::new(),
        walked: 0,
        truncated: false,
    };
    let Got::Have(first) = walk.header()? else {
        return Ok(Step::Need(merge(walk.needs)));
    };
    let mut visited = BTreeSet::new();
    let mut chain = Vec::new();
    let mut offset = first;
    while offset != 0 {
        if chain.is_empty() && offset >= walk.file.size {
            return Err("Its first IFD is past the end of the file.".to_owned());
        }
        if offset >= walk.file.size || !visited.insert(offset) || walk.walked >= MAX_IFDS {
            // A pointer past the end, back to an IFD already read, or past the limit.
            walk.truncated = true;
            break;
        }
        walk.walked += 1;
        match walk.ifd(offset) {
            Ok(Got::Have(ifd)) => {
                offset = ifd.next;
                chain.push(ifd);
            }
            Ok(Got::Wait) => break,
            Err(message) if chain.is_empty() => return Err(message),
            Err(_) => {
                walk.truncated = true;
                break;
            }
        }
    }
    let mut found = Vec::new();
    let mut description = None;
    for (index, ifd) in chain.iter().enumerate() {
        if index == 0 {
            description = read_description(&mut walk, ifd)?;
        }
        let Got::Have(stored) = stored(&mut walk, ifd)? else {
            continue;
        };
        let levels = sub_ifds(&mut walk, ifd, &mut visited)?;
        let subfile = match walk.integer(ifd, 254) {
            Ok(Got::Have(v)) => v.unwrap_or(0),
            _ => 0,
        };
        found.push(Found {
            index,
            stored,
            subfile,
            levels,
        });
    }
    if !walk.needs.is_empty() {
        return Ok(Step::Need(merge(walk.needs)));
    }
    let ome = description
        .as_deref()
        .filter(|d| d.trim_start().starts_with("<?xml") || d.contains("<OME"))
        .and_then(ome::parse);
    Ok(Step::Layout(Layout {
        bigtiff: walk.big,
        images: group(found),
        description,
        ome,
        truncated: walk.truncated,
    }))
}

fn read_description(walk: &mut Walk, ifd: &Ifd) -> Result<Option<String>, String> {
    let Some(e) = ifd.entry(270).cloned() else {
        return Ok(None);
    };
    if e.kind != 2 && e.kind != 1 && e.kind != 7 {
        return Ok(None);
    }
    let Some((offset, length)) = walk.value_span(&e)? else {
        return Ok(None);
    };
    let length = length.min(MAX_DESCRIPTION as u64);
    let bytes = if offset == e.field_at {
        e.field[..usize::try_from(length).unwrap_or(0)].to_vec()
    } else {
        match walk.bytes(offset, length) {
            Ok(Got::Have(b)) => b,
            // A description past the end isn't worth refusing the image for.
            Ok(Got::Wait) | Err(_) => return Ok(None),
        }
    };
    let text = String::from_utf8_lossy(&bytes);
    Ok(Some(text.trim_end_matches('\0').to_owned()))
}

/// The SubIFDs of an IFD (and any chained from them), as levels, largest first.
fn sub_ifds(
    walk: &mut Walk,
    ifd: &Ifd,
    visited: &mut BTreeSet<u64>,
) -> Result<Vec<Stored>, String> {
    let Some(e) = ifd.entry(330).cloned() else {
        return Ok(Vec::new());
    };
    let offsets = match walk.integers(&e, MAX_IFDS as u64) {
        Ok(Got::Have(v)) => v,
        Ok(Got::Wait) | Err(_) => return Ok(Vec::new()),
    };
    let mut levels = Vec::new();
    let mut queue: Vec<u64> = offsets.into_iter().rev().collect();
    while let Some(offset) = queue.pop() {
        if offset == 0 || offset >= walk.file.size || !visited.insert(offset) {
            continue;
        }
        if walk.walked >= MAX_IFDS {
            walk.truncated = true;
            break;
        }
        walk.walked += 1;
        let sub = match walk.ifd(offset) {
            Ok(Got::Have(sub)) => sub,
            Ok(Got::Wait) => continue,
            Err(_) => {
                walk.truncated = true;
                continue;
            }
        };
        queue.push(sub.next);
        if let Got::Have(stored) = stored(walk, &sub)? {
            levels.push(stored);
        }
    }
    levels.sort_by(|a, b| (b.width, b.height).cmp(&(a.width, a.height)));
    Ok(levels)
}

/// Whether `level` looks like a reduction of `full`: the same aspect ratio, to a pixel or
/// two of rounding and 2%.
fn same_aspect(level: &Stored, full: &Stored) -> bool {
    let (w, h) = (f64::from(level.width), f64::from(level.height));
    let (fw, fh) = (f64::from(full.width), f64::from(full.height));
    if w < 1.0 || h < 1.0 || fw < 1.0 || fh < 1.0 {
        return false;
    }
    let a = w * fh;
    let b = h * fw;
    (a - b).abs() <= 0.02 * a.max(b) + 2.0 * (fw + fh)
}

/// The main chain's images, each with the reduced images that follow it as its levels.
fn group(found: Vec<Found>) -> Vec<Image> {
    let mut images: Vec<(Image, bool)> = Vec::new();
    for f in found {
        let reduced = f.subfile & 1 == 1;
        let target = images
            .iter()
            .enumerate()
            .rev()
            .take(2)
            .find(|(_, (image, from_chain))| {
                let smallest = image.levels.last().unwrap_or(&image.stored);
                (*from_chain || image.levels.is_empty())
                    && (reduced || (f.stored.tiled && image.stored.tiled))
                    && f.stored.width < smallest.width
                    && f.stored.height < smallest.height
                    && same_aspect(&f.stored, &image.stored)
            })
            .map(|(at, _)| at);
        match target {
            Some(at) => {
                images[at].0.levels.push(f.stored);
                images[at].1 = true;
            }
            None => images.push((
                Image {
                    index: f.index,
                    stored: f.stored,
                    levels: f.levels,
                },
                false,
            )),
        }
    }
    images.into_iter().map(|(image, _)| image).collect()
}

/// What can't be shown of pixels stored this way, if anything.
#[allow(clippy::too_many_lines)] // Each way of storing pixels, and why it isn't shown.
pub(super) fn unsupported(p: &Pixels) -> Option<String> {
    let bits = p.bits_per_sample;
    let compression = match p.compression {
        2 => Some("CCITT Group 3 (modified Huffman) compression"),
        3 | 4 => Some("CCITT fax compression"),
        6 => Some("old-style JPEG compression (6), which TIFF no longer defines"),
        33003 | 33005 | 34712 => Some("JPEG 2000 compression"),
        34887 => Some("LERC compression"),
        34925 => Some("LZMA compression"),
        50000 => Some("Zstandard compression"),
        50001 | 34927 => Some("WebP compression"),
        50002 => Some("JPEG XL compression"),
        32809 => Some("ThunderScan compression"),
        34676 | 34677 => Some("SGI Log compression"),
        _ => None,
    };
    if let Some(name) = compression {
        return Some(format!(
            "It's stored with {name}, which previews don't decode."
        ));
    }
    if !matches!(p.compression, 1 | 5 | 7 | 8 | 32946 | 32773) {
        return Some(format!(
            "It's stored with a compression (number {}) previews don't decode.",
            p.compression
        ));
    }
    let samples = p.samples_per_pixel;
    if samples == 0 {
        return Some("It says it has no samples per pixel.".to_owned());
    }
    let format_ok = match p.sample_format {
        1 => matches!(bits, 1 | 2 | 4 | 8 | 16 | 32),
        2 => matches!(bits, 8 | 16 | 32),
        3 => matches!(bits, 16 | 32 | 64),
        _ => false,
    };
    if !format_ok {
        let kind = match p.sample_format {
            1 => "unsigned whole numbers",
            2 => "signed whole numbers",
            3 => "floating-point numbers",
            _ => "a sample format TIFF doesn't define",
        };
        return Some(format!(
            "Its samples are {bits}-bit {kind}, which previews don't decode."
        ));
    }
    let chunk_samples = if p.planar == 2 { 1 } else { samples };
    if bits < 8 && (chunk_samples != 1 || p.predictor != 1) {
        return Some(format!(
            "Its samples are {bits}-bit, packed with others or with a predictor, which previews \
             don't decode."
        ));
    }
    match p.predictor {
        1 => {}
        2 if p.sample_format != 3 && bits >= 8 => {}
        3 if p.sample_format == 3 => {}
        n => {
            return Some(format!(
                "It uses predictor {n} for its samples, which previews don't decode."
            ));
        }
    }
    if p.planar != 1 && p.planar != 2 {
        return Some(format!(
            "Its planar configuration ({}) isn't one TIFF defines.",
            p.planar
        ));
    }
    if p.compression == 7 {
        if bits != 8 || p.sample_format != 1 || !matches!(p.photometric, 1 | 2 | 6) {
            return Some(
                "Its JPEG tiles aren't 8-bit grey, RGB or YCbCr, which previews decode.".to_owned(),
            );
        }
        if p.planar == 2 && samples > 1 {
            return Some(
                "Its JPEG tiles are stored plane by plane, which previews don't decode.".to_owned(),
            );
        }
        return None;
    }
    match p.photometric {
        0 | 1 => None,
        2 if samples >= 3 => None,
        2 => Some("It says it's RGB, but has fewer than 3 samples per pixel.".to_owned()),
        3 => {
            let entries = 1usize << bits.min(16);
            if p.sample_format != 1 || bits > 16 {
                Some("Its palette indices aren't 16 bits or fewer.".to_owned())
            } else if p.colormap.as_ref().is_none_or(|m| m.len() != 3 * entries) {
                Some("It's a palette image without a whole ColorMap.".to_owned())
            } else {
                None
            }
        }
        4 => Some("It's a transparency mask, which previews don't show.".to_owned()),
        5 => Some("It's CMYK (separated), which previews don't show.".to_owned()),
        6 => Some("It's YCbCr without JPEG compression, which previews don't decode.".to_owned()),
        8..=10 => Some("It's CIE L*a*b*, which previews don't show.".to_owned()),
        32844 | 32845 => Some("It's SGI LogL or LogLuv, which previews don't show.".to_owned()),
        n => Some(format!(
            "Its photometric interpretation ({n}) isn't one previews show."
        )),
    }
}

/// An IFD's image: how its pixels are stored and where its tiles or strips are.
#[allow(clippy::too_many_lines)] // Each tag the layout needs, read and checked.
fn stored(walk: &mut Walk, ifd: &Ifd) -> Result<Got<Stored>, String> {
    let mut waiting = false;
    let mut scalar = |walk: &mut Walk, tag: u16, default: u64| -> u64 {
        match walk.integer(ifd, tag) {
            Ok(Got::Have(v)) => v.unwrap_or(default),
            Ok(Got::Wait) => {
                waiting = true;
                default
            }
            Err(_) => default,
        }
    };
    let width = scalar(walk, 256, 0);
    let height = scalar(walk, 257, 0);
    let samples = scalar(walk, 277, 1);
    let compression = scalar(walk, 259, 1);
    let photometric = scalar(walk, 262, if samples >= 3 { 2 } else { 1 });
    let planar = scalar(walk, 284, 1);
    let predictor = scalar(walk, 317, 1);
    let rows_per_strip = scalar(walk, 278, u64::from(u32::MAX));
    let tile_w = scalar(walk, 322, 0);
    let tile_h = scalar(walk, 323, 0);
    let mut problems: Vec<String> = Vec::new();
    // BitsPerSample and SampleFormat may list one value per sample: all must agree.
    let mut per_sample = |walk: &mut Walk, tag: u16, default: u64, name: &str| -> u64 {
        let Some(e) = ifd.entry(tag).cloned() else {
            return default;
        };
        match walk.integers(&e, 65_536) {
            Ok(Got::Have(values)) => {
                let first = values.first().copied().unwrap_or(default);
                if values.iter().any(|&v| v != first) {
                    problems.push(format!(
                        "Its samples' {name} differ, which previews don't decode."
                    ));
                }
                first
            }
            Ok(Got::Wait) => {
                waiting = true;
                default
            }
            Err(message) => {
                problems.push(message);
                default
            }
        }
    };
    let bits = per_sample(walk, 258, 1, "bit depths");
    let sample_format = per_sample(walk, 339, 1, "sample formats");
    let narrow = |v: u64| u16::try_from(v).unwrap_or(u16::MAX);
    let mut pixels = Pixels {
        compression: narrow(compression),
        predictor: narrow(predictor),
        photometric: narrow(photometric),
        bits_per_sample: narrow(bits),
        samples_per_pixel: narrow(samples),
        sample_format: narrow(sample_format),
        planar: narrow(planar),
        colormap: None,
        little_endian: walk.order.little,
    };
    if pixels.photometric == 3
        && bits <= 16
        && let Some(e) = ifd.entry(320).cloned()
    {
        match walk.integers(&e, 3 << 16) {
            Ok(Got::Have(values)) => {
                pixels.colormap = Some(
                    values
                        .into_iter()
                        .map(|v| u16::try_from(v).unwrap_or(u16::MAX))
                        .collect(),
                );
            }
            Ok(Got::Wait) => waiting = true,
            Err(message) => problems.push(message),
        }
    }
    let jpeg_tables = ifd.entry(347).cloned().and_then(|e| {
        walk.value_span(&e)
            .ok()
            .flatten()
            .filter(|(offset, length)| offset.saturating_add(*length) <= walk.file.size)
            .map(|(offset, length)| Span { offset, length })
    });
    let tiled = ifd.entry(322).is_some() || ifd.entry(324).is_some();
    let mut stored = Stored {
        width: u32::try_from(width).unwrap_or(u32::MAX),
        height: u32::try_from(height).unwrap_or(u32::MAX),
        tiled,
        tile_width: 0,
        tile_height: 0,
        offsets: Vec::new(),
        byte_counts: Vec::new(),
        pixels,
        jpeg_tables,
        unsupported: None,
    };
    if waiting {
        return Ok(Got::Wait);
    }
    let refuse = |mut stored: Stored, why: String| {
        stored.unsupported = Some(why);
        Ok(Got::Have(stored))
    };
    if width == 0 || height == 0 {
        return refuse(
            stored,
            "It has no width or height (ImageWidth, ImageLength).".to_owned(),
        );
    }
    if width > MAX_SIDE || height > MAX_SIDE {
        return refuse(
            stored,
            format!("It says it's {width} × {height} pixels, larger than an image can be shown."),
        );
    }
    if let Some(problem) = problems.into_iter().next() {
        return refuse(stored, problem);
    }
    if let Some(why) = unsupported(&stored.pixels) {
        return refuse(stored, why);
    }
    let planes = if planar == 2 { samples } else { 1 };
    let (chunk_w, chunk_h) = if tiled {
        (tile_w, tile_h)
    } else {
        (width, rows_per_strip.clamp(1, height))
    };
    if chunk_w == 0 || chunk_h == 0 || chunk_w > MAX_SIDE || chunk_h > MAX_SIDE {
        return refuse(stored, "Its tiles have no width or height.".to_owned());
    }
    stored.tile_width = u32::try_from(chunk_w).unwrap_or(u32::MAX);
    stored.tile_height = u32::try_from(chunk_h).unwrap_or(u32::MAX);
    let across = width.div_ceil(chunk_w);
    let down = height.div_ceil(chunk_h);
    let expected = across * down * planes;
    let noun = if tiled { "tiles" } else { "strips" };
    if expected > MAX_CHUNKS as u64 {
        walk.truncated = true;
        return refuse(
            stored,
            format!("It has {expected} {noun}, more than the {MAX_CHUNKS} a preview lists."),
        );
    }
    let (offsets_tag, counts_tag) = if tiled { (324, 325) } else { (273, 279) };
    let Some(offsets_entry) = ifd.entry(offsets_tag).cloned() else {
        return refuse(stored, format!("It doesn't say where its {noun} are."));
    };
    let offsets = match walk.integers(&offsets_entry, MAX_CHUNKS as u64) {
        Ok(Got::Have(v)) => v,
        Ok(Got::Wait) => return Ok(Got::Wait),
        Err(message) => return refuse(stored, message),
    };
    let chunk_samples = if planar == 2 { 1 } else { samples };
    let row_bytes = (chunk_w * chunk_samples * bits).div_ceil(8);
    let counts = match ifd.entry(counts_tag).cloned() {
        Some(e) => match walk.integers(&e, MAX_CHUNKS as u64) {
            Ok(Got::Have(v)) => v,
            Ok(Got::Wait) => return Ok(Got::Wait),
            Err(message) => return refuse(stored, message),
        },
        // Old files leave out an uncompressed image's byte counts: its size says them.
        None if compression == 1 => vec![row_bytes * chunk_h; offsets.len()],
        None => return refuse(stored, format!("It doesn't say how long its {noun} are.")),
    };
    if offsets.len() != counts.len() {
        return refuse(
            stored,
            format!(
                "Its {noun}' offsets and byte counts don't match: {} and {}.",
                offsets.len(),
                counts.len()
            ),
        );
    }
    if (offsets.len() as u64) < expected {
        return refuse(
            stored,
            format!(
                "It lists {} {noun}, but its size needs {expected}.",
                offsets.len()
            ),
        );
    }
    let expected = usize::try_from(expected).unwrap_or(usize::MAX);
    let size = walk.file.size;
    let mut offsets: Vec<u64> = offsets.into_iter().take(expected).collect();
    // A tile or strip past the end of the file (a file cut short) reads as far as it goes.
    let mut counts: Vec<u64> = counts
        .into_iter()
        .take(expected)
        .zip(&offsets)
        .map(|(count, &offset)| count.min(size.saturating_sub(offset)))
        .collect();
    for offset in &mut offsets {
        *offset = (*offset).min(size);
    }
    let chunk_pixels = chunk_w * chunk_h;
    if chunk_pixels > MAX_TILE_PIXELS as u64 {
        let one_strip = !tiled && down == 1 && compression == 1 && bits >= 8;
        let rows = (STRIP_BYTES / row_bytes.max(1))
            .min(MAX_TILE_PIXELS as u64 / chunk_w)
            .min(height);
        if !one_strip || rows == 0 {
            return refuse(
                stored,
                format!(
                    "Its {noun} are {chunk_w} × {chunk_h} pixels, more than a preview decodes at \
                     once."
                ),
            );
        }
        // Uncompressed rows are at known places: list strips of fewer rows within it.
        let strips = height.div_ceil(rows);
        if strips * planes > MAX_CHUNKS as u64 {
            walk.truncated = true;
            return refuse(
                stored,
                format!("It has more than {MAX_CHUNKS} rows of strips to list."),
            );
        }
        let mut split_offsets = Vec::new();
        let mut split_counts = Vec::new();
        for (&start, &count) in offsets.iter().zip(&counts) {
            for s in 0..strips {
                let first = s * rows * row_bytes;
                let length = (rows.min(height - s * rows)) * row_bytes;
                let at = start.saturating_add(first).min(size);
                split_offsets.push(at);
                split_counts.push(length.min(count.saturating_sub(first)).min(size - at));
            }
        }
        offsets = split_offsets;
        counts = split_counts;
        stored.tile_height = u32::try_from(rows).unwrap_or(u32::MAX);
    }
    stored.offsets = offsets;
    stored.byte_counts = counts;
    Ok(Got::Have(stored))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merges_needs() {
        let spans = merge(vec![
            Span {
                offset: 100_000,
                length: 10,
            },
            Span {
                offset: 0,
                length: 65_536,
            },
            Span {
                offset: 150_000,
                length: 65_536,
            },
        ]);
        assert_eq!(
            spans,
            vec![Span {
                offset: 0,
                length: 215_536
            }]
        );
        let far = merge(vec![
            Span {
                offset: 0,
                length: 10,
            },
            Span {
                offset: 10_000_000,
                length: 10,
            },
        ]);
        assert_eq!(far.len(), 2);
    }
}
