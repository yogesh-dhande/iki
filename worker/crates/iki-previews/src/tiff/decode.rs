//! Decoding one tile or strip to RGBA: decompressing it no further than its size needs,
//! undoing its predictor, then mapping its samples to colours.

use base64::Engine as _;
use miniz_oxide::inflate::stream::InflateState;
use miniz_oxide::{DataFormat, MZError, MZFlush, MZStatus};

use super::{MAX_TILE_PIXELS, Tile, TileRequest, ifd};

/// The most bytes a tile may decompress to: its pixels' samples, at most this.
const MAX_TILE_BYTES: u64 = 512 * 1024 * 1024;

pub(super) fn tile(input: &[u8], request_len: usize) -> Result<(Tile, Vec<u8>), String> {
    let json = input
        .get(..request_len)
        .ok_or_else(|| "The tile request is cut short.".to_owned())?;
    let request: TileRequest = serde_json::from_slice(json)
        .map_err(|error| format!("The tile request isn't one: {error}."))?;
    let data = &input[request_len..];
    let p = &request.pixels;
    if let Some(why) = ifd::unsupported(p) {
        return Err(why);
    }
    let (width, height) = (request.width as usize, request.height as usize);
    if width == 0 || height == 0 {
        return Err("The tile has no width or height.".to_owned());
    }
    if width.saturating_mul(height) > MAX_TILE_PIXELS {
        return Err(format!(
            "The tile is {width} × {height} pixels, more than a preview decodes at once."
        ));
    }
    if p.compression == 7 {
        return jpeg(&request, data);
    }
    let planes = match (&request.plane_lengths, p.planar) {
        (Some(lengths), 2) => {
            if lengths.len() != usize::from(p.samples_per_pixel) {
                return Err(format!(
                    "The tile request gives {} planes' lengths, but the image has {} samples \
                     per pixel.",
                    lengths.len(),
                    p.samples_per_pixel
                ));
            }
            if lengths.iter().try_fold(0usize, |a, &b| a.checked_add(b)) != Some(data.len()) {
                return Err("The tile's planes' lengths don't add up to its bytes.".to_owned());
            }
            lengths.clone()
        }
        _ => vec![data.len()],
    };
    let one_plane = p.planar == 2 && planes.len() == 1;
    let samples = if p.planar == 2 {
        1
    } else {
        usize::from(p.samples_per_pixel)
    };
    let bits = usize::from(p.bits_per_sample);
    let row_bytes = (width as u64 * samples as u64 * bits as u64).div_ceil(8);
    let expected = row_bytes * height as u64;
    if expected.saturating_mul(planes.len() as u64) > MAX_TILE_BYTES {
        return Err("The tile's samples are more than a preview decodes at once.".to_owned());
    }
    let row_bytes = usize::try_from(row_bytes).map_err(|_| "The tile is too large.".to_owned())?;
    let expected = row_bytes * height;
    let mut little = p.little_endian;
    let mut decoded = Vec::with_capacity(planes.len());
    let mut at = 0;
    for length in &planes {
        let mut raw = decompress(p.compression, &data[at..at + length], expected)?;
        at += length;
        match p.predictor {
            2 => horizontal(&mut raw, row_bytes, samples, bits, p.little_endian),
            3 => {
                floating_point(&mut raw, row_bytes, samples, bits / 8);
                little = false;
            }
            _ => {}
        }
        decoded.push(raw);
    }
    let (raw, samples, row_bytes) = if decoded.len() > 1 {
        // Planes (each whole bytes per sample) interleaved, as a chunky tile.
        let size = bits / 8;
        let count = decoded.len();
        let mut chunky = vec![0u8; expected * count];
        for (s, plane) in decoded.iter().enumerate() {
            for (i, sample) in plane.chunks_exact(size).enumerate() {
                let to = (i * count + s) * size;
                chunky[to..to + size].copy_from_slice(sample);
            }
        }
        (chunky, count, row_bytes * count)
    } else {
        (decoded.pop().unwrap_or_default(), samples, row_bytes)
    };
    let samples_layout = Layout {
        samples,
        bits,
        row_bytes,
        little,
        format: p.sample_format,
    };
    colour(&request, &samples_layout, &raw, width, height, !one_plane)
}

/// Decompresses `data` to exactly `expected` bytes, never more.
fn decompress(compression: u16, data: &[u8], expected: usize) -> Result<Vec<u8>, String> {
    let short = |name: &str| format!("The tile's {name} data ends before its pixels do.");
    match compression {
        1 => data
            .get(..expected)
            .map(<[u8]>::to_vec)
            .ok_or_else(|| "The tile's bytes end before its byte count says.".to_owned()),
        5 => lzw(data, expected),
        8 | 32946 => inflate(data, expected),
        32773 => {
            let out = packbits(data, expected);
            if out.len() < expected {
                return Err(short("PackBits"));
            }
            Ok(out)
        }
        n => Err(format!("Compression {n} isn't one previews decode.")),
    }
}

fn lzw(data: &[u8], expected: usize) -> Result<Vec<u8>, String> {
    // Files from before TIFF 6 wrote LZW's codes least significant bit first.
    let mut decoder = if data.len() >= 2 && data[0] == 0 && data[1] & 1 == 1 {
        weezl::decode::Decoder::new(weezl::BitOrder::Lsb, 8)
    } else {
        weezl::decode::Decoder::with_tiff_size_switch(weezl::BitOrder::Msb, 8)
    };
    let mut out = vec![0u8; expected];
    let (mut read, mut written) = (0usize, 0usize);
    while written < expected {
        let result = decoder.decode_bytes(&data[read..], &mut out[written..]);
        read += result.consumed_in;
        written += result.consumed_out;
        match result.status {
            Ok(weezl::LzwStatus::Ok) if result.consumed_in + result.consumed_out > 0 => {}
            Ok(_) => break,
            Err(_) if written == expected => break,
            Err(_) => return Err("The tile's LZW data is broken.".to_owned()),
        }
    }
    if written < expected {
        return Err("The tile's LZW data ends before its pixels do.".to_owned());
    }
    Ok(out)
}

fn inflate(data: &[u8], expected: usize) -> Result<Vec<u8>, String> {
    // Deflate in TIFF is zlib-wrapped; a few writers leave the wrapper out.
    let zlib = data.len() >= 2
        && data[0] & 0x0f == 8
        && (u16::from(data[0]) << 8 | u16::from(data[1])).is_multiple_of(31);
    let mut state = InflateState::new_boxed(if zlib {
        DataFormat::ZLibIgnoreChecksum
    } else {
        DataFormat::Raw
    });
    let mut out = vec![0u8; expected];
    let (mut read, mut written) = (0usize, 0usize);
    while written < expected {
        let result = miniz_oxide::inflate::stream::inflate(
            &mut state,
            &data[read..],
            &mut out[written..],
            MZFlush::None,
        );
        read += result.bytes_consumed;
        written += result.bytes_written;
        match result.status {
            Ok(MZStatus::StreamEnd) => break,
            Ok(_) | Err(MZError::Buf) if result.bytes_consumed + result.bytes_written > 0 => {}
            Ok(_) | Err(MZError::Buf) => break,
            Err(_) => return Err("The tile's Deflate data is broken.".to_owned()),
        }
    }
    if written < expected {
        return Err("The tile's Deflate data ends before its pixels do.".to_owned());
    }
    Ok(out)
}

/// PackBits (Apple's run-length encoding), up to `expected` bytes.
fn packbits(data: &[u8], expected: usize) -> Vec<u8> {
    let mut out = Vec::with_capacity(expected);
    let mut i = 0;
    while out.len() < expected && i < data.len() {
        let n = data[i].cast_signed();
        i += 1;
        if n >= 0 {
            let len = (usize::from(n.cast_unsigned()) + 1)
                .min(data.len() - i)
                .min(expected - out.len());
            out.extend_from_slice(&data[i..i + len]);
            i += usize::from(n.cast_unsigned()) + 1;
        } else if n != -128 {
            let Some(&b) = data.get(i) else { break };
            i += 1;
            let len = (1 + usize::from(n.unsigned_abs())).min(expected - out.len());
            out.resize(out.len() + len, b);
        }
    }
    out
}

/// Undoes horizontal differencing (predictor 2), sample by sample along each row.
fn horizontal(raw: &mut [u8], row_bytes: usize, samples: usize, bits: usize, little: bool) {
    let size = bits / 8;
    let read = |b: &[u8]| -> u64 {
        let mut v = 0u64;
        for i in 0..size {
            let byte = if little { b[size - 1 - i] } else { b[i] };
            v = v << 8 | u64::from(byte);
        }
        v
    };
    let write = |b: &mut [u8], v: u64| {
        for i in 0..size {
            let byte = v.to_le_bytes()[i];
            if little {
                b[i] = byte;
            } else {
                b[size - 1 - i] = byte;
            }
        }
    };
    let mask = if size >= 8 {
        u64::MAX
    } else {
        (1u64 << (8 * size)) - 1
    };
    for row in raw.chunks_exact_mut(row_bytes) {
        let stride = samples * size;
        let mut at = stride;
        while at + size <= row.len() {
            let left = read(&row[at - stride..at - stride + size]);
            let here = read(&row[at..at + size]);
            write(&mut row[at..at + size], here.wrapping_add(left) & mask);
            at += size;
        }
    }
}

/// Undoes the floating-point predictor (3): bytes differenced along the row, each
/// sample's bytes stored as planes, most significant first. Leaves samples big-endian.
fn floating_point(raw: &mut [u8], row_bytes: usize, samples: usize, size: usize) {
    let mut planes = vec![0u8; row_bytes];
    for row in raw.chunks_exact_mut(row_bytes) {
        for i in samples..row.len() {
            row[i] = row[i].wrapping_add(row[i - samples]);
        }
        planes.copy_from_slice(row);
        let count = row.len() / size.max(1);
        for n in 0..count {
            for b in 0..size {
                row[n * size + b] = planes[b * count + n];
            }
        }
    }
}

/// How a tile's decompressed samples are laid out.
struct Layout {
    samples: usize,
    bits: usize,
    row_bytes: usize,
    little: bool,
    format: u16,
}

impl Layout {
    /// Sample `s` of pixel `x` in row `y`, as a number.
    fn sample(&self, raw: &[u8], column: usize, line: usize, which: usize) -> f64 {
        let row = &raw[line * self.row_bytes..(line + 1) * self.row_bytes];
        let index = column * self.samples + which;
        if self.bits < 8 {
            let bit = index * self.bits;
            let byte = row[bit / 8];
            let shift = 8 - self.bits - bit % 8;
            return f64::from((byte >> shift) & ((1u8 << self.bits) - 1));
        }
        let size = self.bits / 8;
        let bytes = &row[index * size..(index + 1) * size];
        let mut v = 0u64;
        for i in 0..size {
            let byte = if self.little {
                bytes[size - 1 - i]
            } else {
                bytes[i]
            };
            v = v << 8 | u64::from(byte);
        }
        #[allow(clippy::cast_possible_truncation, clippy::cast_precision_loss)]
        match (self.format, size) {
            (2, 1) => f64::from((v as u8).cast_signed()),
            (2, 2) => f64::from((v as u16).cast_signed()),
            (2, 4) => f64::from((v as u32).cast_signed()),
            (3, 2) => f64::from(half(v as u16)),
            (3, 4) => f64::from(f32::from_bits(v as u32)),
            (3, 8) => f64::from_bits(v),
            _ => v as f64,
        }
    }
}

/// A 16-bit (IEEE 754 half) float.
fn half(h: u16) -> f32 {
    let sign = if h & 0x8000 == 0 { 1.0 } else { -1.0 };
    let exponent = i32::from((h >> 10) & 0x1f);
    let fraction = f32::from(h & 0x3ff);
    match exponent {
        0 => sign * fraction * 2f32.powi(-24),
        31 if fraction == 0.0 => sign * f32::INFINITY,
        31 => f32::NAN,
        e => sign * (1.0 + fraction / 1024.0) * 2f32.powi(e - 15),
    }
}

/// Samples mapped to bytes: as they are for 8 bits, through the window for more.
struct Mapping {
    lo: f64,
    hi: f64,
    direct: Option<f64>,
}

impl Mapping {
    /// The byte for a value, or `None` for NaN (drawn black).
    fn map(&self, v: f64) -> Option<u8> {
        if v.is_nan() {
            return None;
        }
        #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
        if let Some(scale) = self.direct {
            return Some((v * scale).round().clamp(0.0, 255.0) as u8);
        }
        let span = self.hi - self.lo;
        #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
        Some(if span > 0.0 && span.is_finite() {
            ((v - self.lo) / span * 255.0).round().clamp(0.0, 255.0) as u8
        } else if v > self.lo {
            255
        } else {
            0
        })
    }
}

fn colour(
    request: &TileRequest,
    layout: &Layout,
    raw: &[u8],
    width: usize,
    height: usize,
    chunky: bool,
) -> Result<(Tile, Vec<u8>), String> {
    let p = &request.pixels;
    let rgb = p.photometric == 2 && chunky;
    let palette = p.photometric == 3;
    let shown: Vec<usize> = if rgb {
        vec![0, 1, 2]
    } else if layout.samples == 1 {
        vec![0]
    } else {
        let s = usize::from(request.sample.unwrap_or(0));
        if s >= layout.samples {
            return Err(format!(
                "The image has {} samples per pixel; there's no sample {s}.",
                layout.samples
            ));
        }
        vec![s]
    };
    let (mut min, mut max) = (f64::INFINITY, f64::NEG_INFINITY);
    for y in 0..height {
        for x in 0..width {
            for &s in &shown {
                let v = layout.sample(raw, x, y, s);
                if v.is_finite() {
                    min = min.min(v);
                    max = max.max(v);
                }
            }
        }
    }
    if min > max {
        (min, max) = (0.0, 0.0);
    }
    let direct = match (layout.bits, p.sample_format) {
        _ if request.window.is_some() => None,
        (8, 1) => Some(1.0),
        (b, 1) if b < 8 => Some(255.0 / f64::from((1u16 << b) - 1)),
        _ => None,
    };
    let [lo, hi] = request.window.unwrap_or([min, max]);
    let mapping = Mapping {
        // Signed 8-bit samples are shifted to 0–255.
        lo: if layout.bits == 8 && p.sample_format == 2 && request.window.is_none() {
            -128.0
        } else {
            lo
        },
        hi: if layout.bits == 8 && p.sample_format == 2 && request.window.is_none() {
            127.0
        } else {
            hi
        },
        direct,
    };
    let invert = p.photometric == 0;
    let colormap = p.colormap.as_deref().unwrap_or(&[]);
    let entries = colormap.len() / 3;
    let mut pixels = Vec::with_capacity(width * height * 4);
    for y in 0..height {
        for x in 0..width {
            if palette {
                #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
                let i = layout.sample(raw, x, y, 0) as usize;
                let colour = |c: usize| {
                    colormap
                        .get(c * entries + i)
                        .map_or(0, |&v| u8::try_from(v >> 8).unwrap_or(u8::MAX))
                };
                pixels.extend_from_slice(&[colour(0), colour(1), colour(2), 255]);
            } else if rgb {
                for s in 0..3 {
                    if let Some(b) = mapping.map(layout.sample(raw, x, y, s)) {
                        pixels.push(b);
                    } else {
                        pixels.push(0);
                    }
                }
                pixels.push(255);
            } else {
                match mapping.map(layout.sample(raw, x, y, shown[0])) {
                    Some(b) => {
                        let b = if invert { 255 - b } else { b };
                        pixels.extend_from_slice(&[b, b, b, 255]);
                    }
                    None => pixels.extend_from_slice(&[0, 0, 0, 255]),
                }
            }
        }
    }
    Ok((
        Tile {
            width: request.width,
            height: request.height,
            min,
            max,
        },
        pixels,
    ))
}

/// A JPEG tile: its file's `JPEGTables` (less their end) before its own data (less its
/// start), decoded to RGB or grey and placed in a tile of the request's size.
fn jpeg(request: &TileRequest, data: &[u8]) -> Result<(Tile, Vec<u8>), String> {
    let mut stream = Vec::with_capacity(data.len() + 1024);
    match &request.jpeg_tables {
        Some(tables) => {
            let tables = base64::engine::general_purpose::STANDARD
                .decode(tables)
                .map_err(|_| "The tile request's JPEGTables aren't base64.".to_owned())?;
            let tables = tables.strip_suffix(&[0xff, 0xd9]).unwrap_or(&tables);
            stream.extend_from_slice(tables);
            stream.extend_from_slice(data.strip_prefix(&[0xff, 0xd8]).unwrap_or(data));
        }
        None => stream.extend_from_slice(data),
    }
    if request.pixels.photometric == 2 {
        name_components_rgb(&mut stream);
    }
    let grey = request.pixels.photometric == 1;
    let options = zune_jpeg::zune_core::options::DecoderOptions::default()
        .set_max_width(65_535)
        .set_max_height(65_535)
        .set_strict_mode(false)
        .jpeg_set_out_colorspace(if grey {
            zune_jpeg::zune_core::colorspace::ColorSpace::Luma
        } else {
            zune_jpeg::zune_core::colorspace::ColorSpace::RGB
        });
    let mut decoder = zune_jpeg::JpegDecoder::new_with_options(
        zune_jpeg::zune_core::bytestream::ZCursor::new(&stream[..]),
        options,
    );
    let rgb = decoder
        .decode()
        .map_err(|error| format!("The tile's JPEG data doesn't decode: {error:?}."))?;
    let info = decoder
        .info()
        .ok_or_else(|| "The tile's JPEG data has no image.".to_owned())?;
    let (jw, jh) = (usize::from(info.width), usize::from(info.height));
    if jw == 0 || jh == 0 {
        return Err("The tile's JPEG data has no image.".to_owned());
    }
    let channels = rgb.len() / (jw * jh);
    if channels == 0 {
        return Err("The tile's JPEG data ends before its pixels do.".to_owned());
    }
    let (width, height) = (request.width as usize, request.height as usize);
    let mut pixels = vec![0u8; width * height * 4];
    let (mut min, mut max) = (255u8, 0u8);
    for y in 0..height.min(jh) {
        for x in 0..width.min(jw) {
            let from = (y * jw + x) * channels;
            let (r, g, b) = if channels >= 3 {
                (rgb[from], rgb[from + 1], rgb[from + 2])
            } else {
                (rgb[from], rgb[from], rgb[from])
            };
            min = min.min(r).min(g).min(b);
            max = max.max(r).max(g).max(b);
            let to = (y * width + x) * 4;
            pixels[to..to + 4].copy_from_slice(&[r, g, b, 255]);
        }
    }
    if min > max {
        (min, max) = (0, 0);
    }
    Ok((
        Tile {
            width: request.width,
            height: request.height,
            min: f64::from(min),
            max: f64::from(max),
        },
        pixels,
    ))
}

/// Names a three-component JPEG's components R, G and B (in its frame header and scan
/// headers), so it decodes as RGB, which a TIFF whose photometric interpretation is RGB
/// says it is, rather than as YCbCr, which a JPEG decoder otherwise assumes.
fn name_components_rgb(stream: &mut [u8]) {
    let mut ids: Vec<u8> = Vec::new();
    let mut at = 2usize;
    while at + 4 <= stream.len() {
        if stream[at] != 0xff {
            return;
        }
        let marker = stream[at + 1];
        if marker == 0xff {
            at += 1;
            continue;
        }
        if marker == 0x01 || (0xd0..=0xd8).contains(&marker) {
            at += 2;
            continue;
        }
        let length = usize::from(u16::from_be_bytes([stream[at + 2], stream[at + 3]]));
        let body = at + 4;
        let end = at + 2 + length;
        if length < 2 || end > stream.len() {
            return;
        }
        let frame = (0xc0..=0xcf).contains(&marker) && !matches!(marker, 0xc4 | 0xc8 | 0xcc);
        if frame && length >= 8 {
            let count = usize::from(stream[body + 5]);
            if count != 3 || body + 6 + count * 3 > end {
                return;
            }
            for c in 0..3 {
                ids.push(stream[body + 6 + c * 3]);
                stream[body + 6 + c * 3] = b"RGB"[c];
            }
        } else if marker == 0xda {
            let count = usize::from(stream[body]);
            for c in 0..count {
                let Some(slot) = stream.get_mut(body + 1 + c * 2) else {
                    return;
                };
                if let Some(i) = ids.iter().position(|id| id == slot) {
                    *slot = b"RGB"[i];
                }
            }
            return;
        }
        at = end;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn packbits_as_apple_describes_it() {
        // TIFF 6.0's own example.
        let packed = [
            0xfe, 0xaa, 0x02, 0x80, 0x00, 0x2a, 0xfd, 0xaa, 0x03, 0x80, 0x00, 0x2a, 0x22, 0xf7,
            0xaa,
        ];
        let unpacked = packbits(&packed, 24);
        assert_eq!(
            unpacked,
            [
                0xaa, 0xaa, 0xaa, 0x80, 0x00, 0x2a, 0xaa, 0xaa, 0xaa, 0xaa, 0x80, 0x00, 0x2a, 0x22,
                0xaa, 0xaa, 0xaa, 0xaa, 0xaa, 0xaa, 0xaa, 0xaa, 0xaa, 0xaa
            ]
        );
        // Never past what's expected, whatever the runs say.
        assert_eq!(packbits(&[0x81, 0x00, 0x81, 0x00], 10).len(), 10);
    }

    #[test]
    fn halves() {
        assert!((half(0x3c00) - 1.0).abs() < f32::EPSILON);
        assert!((half(0xc000) + 2.0).abs() < f32::EPSILON);
        assert!(half(0x7e00).is_nan());
    }
}
