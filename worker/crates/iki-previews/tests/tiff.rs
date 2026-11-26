//! TIFF layouts and tiles: files written here (classic and BigTIFF, both byte orders,
//! each compression and pixel layout), samples written by tifffile and libtiff, and
//! broken and hostile files.
// Test code: indexes, casts and short names as the formats' specifications write them.
#![allow(
    clippy::unwrap_used,
    clippy::cast_possible_truncation,
    clippy::cast_possible_wrap,
    clippy::cast_precision_loss,
    clippy::cast_sign_loss,
    clippy::too_many_lines,
    clippy::many_single_char_names,
    clippy::format_push_string,
    clippy::format_collect,
    clippy::items_after_statements,
    clippy::identity_op,
    clippy::range_plus_one,
    clippy::manual_is_multiple_of,
    clippy::needless_pass_by_value
)]

use iki_previews::tiff::{self, Layout, Span, Step, Stored};

// --- A small TIFF writer --------------------------------------------------------------

/// A tag's values.
#[derive(Clone)]
enum V {
    Ascii(String),
    Short(Vec<u16>),
    Long(Vec<u32>),
    Long8(Vec<u64>),
    /// A count and offset written as given, pointing anywhere.
    Raw(u16, u64, u64),
}

#[derive(Clone, Default)]
struct Ifd {
    tags: Vec<(u16, V)>,
    chunks: Vec<Vec<u8>>,
    tiled: bool,
    sub: Vec<Ifd>,
}

struct Writer {
    big: bool,
    le: bool,
    buf: Vec<u8>,
}

impl Writer {
    fn put(&mut self, v: u64, size: usize) {
        let bytes = v.to_le_bytes();
        if self.le {
            self.buf.extend_from_slice(&bytes[..size]);
        } else {
            self.buf.extend(bytes[..size].iter().rev());
        }
    }

    fn patch(&mut self, at: usize, v: u64) {
        let size = if self.big { 8 } else { 4 };
        let bytes = v.to_le_bytes();
        for i in 0..size {
            self.buf[at + i] = if self.le {
                bytes[i]
            } else {
                bytes[size - 1 - i]
            };
        }
    }

    fn align(&mut self) {
        if self.buf.len() % 2 == 1 {
            self.buf.push(0);
        }
    }

    fn value_bytes(&self, v: &V) -> (u16, u64, Vec<u8>) {
        let mut w = Writer {
            big: self.big,
            le: self.le,
            buf: Vec::new(),
        };
        let (kind, count) = match v {
            V::Ascii(s) => {
                w.buf.extend_from_slice(s.as_bytes());
                w.buf.push(0);
                (2, s.len() as u64 + 1)
            }
            V::Short(values) => {
                for &x in values {
                    w.put(u64::from(x), 2);
                }
                (3, values.len() as u64)
            }
            V::Long(values) => {
                for &x in values {
                    w.put(u64::from(x), 4);
                }
                (4, values.len() as u64)
            }
            V::Long8(values) => {
                for &x in values {
                    w.put(x, 8);
                }
                (16, values.len() as u64)
            }
            V::Raw(kind, count, _) => (*kind, *count),
        };
        (kind, count, w.buf)
    }

    /// Writes an IFD (its sub-IFDs, chunks and values first); returns where it is and
    /// where its next-IFD pointer is.
    fn ifd(&mut self, ifd: &Ifd) -> (u64, usize) {
        let subs: Vec<u64> = ifd.sub.iter().map(|s| self.ifd(s).0).collect();
        let mut offsets = Vec::new();
        for chunk in &ifd.chunks {
            self.align();
            offsets.push(self.buf.len() as u64);
            self.buf.extend_from_slice(chunk);
        }
        let mut tags = ifd.tags.clone();
        if !ifd.chunks.is_empty() {
            let counts: Vec<u64> = ifd.chunks.iter().map(|c| c.len() as u64).collect();
            let (o, c) = if ifd.tiled { (324, 325) } else { (273, 279) };
            if self.big {
                tags.push((o, V::Long8(offsets)));
                tags.push((c, V::Long8(counts)));
            } else {
                tags.push((o, V::Long(offsets.iter().map(|&x| x as u32).collect())));
                tags.push((c, V::Long(counts.iter().map(|&x| x as u32).collect())));
            }
        }
        if !subs.is_empty() {
            tags.push((
                330,
                if self.big {
                    V::Long8(subs)
                } else {
                    V::Long(subs.iter().map(|&x| x as u32).collect())
                },
            ));
        }
        tags.sort_by_key(|t| t.0);
        let field = if self.big { 8 } else { 4 };
        let mut entries = Vec::new();
        for (tag, v) in &tags {
            let (kind, count, bytes) = self.value_bytes(v);
            let value = match v {
                V::Raw(_, _, offset) => {
                    let mut w = Writer {
                        big: self.big,
                        le: self.le,
                        buf: Vec::new(),
                    };
                    w.put(*offset, field);
                    w.buf
                }
                _ if bytes.len() <= field => {
                    let mut b = bytes.clone();
                    b.resize(field, 0);
                    b
                }
                _ => {
                    self.align();
                    let at = self.buf.len() as u64;
                    self.buf.extend_from_slice(&bytes);
                    let mut w = Writer {
                        big: self.big,
                        le: self.le,
                        buf: Vec::new(),
                    };
                    w.put(at, field);
                    w.buf
                }
            };
            entries.push((*tag, kind, count, value));
        }
        self.align();
        let at = self.buf.len() as u64;
        self.put(entries.len() as u64, if self.big { 8 } else { 2 });
        for (tag, kind, count, value) in entries {
            self.put(u64::from(tag), 2);
            self.put(u64::from(kind), 2);
            self.put(count, if self.big { 8 } else { 4 });
            self.buf.extend_from_slice(&value);
        }
        let next = self.buf.len();
        self.put(0, field);
        (at, next)
    }
}

fn write_tiff(ifds: &[Ifd], big: bool, le: bool) -> Vec<u8> {
    let mut w = Writer {
        big,
        le,
        buf: Vec::new(),
    };
    w.buf.extend_from_slice(if le { b"II" } else { b"MM" });
    w.put(if big { 43 } else { 42 }, 2);
    if big {
        w.put(8, 2);
        w.put(0, 2);
    }
    let mut pointer = w.buf.len();
    w.put(0, if big { 8 } else { 4 });
    for ifd in ifds {
        let (at, next) = w.ifd(ifd);
        w.patch(pointer, at);
        pointer = next;
    }
    w.buf
}

/// The tags of an image `width` × `height` stored in chunks of `chunk_w` × `chunk_h`.
fn image(
    width: u32,
    height: u32,
    tags: &[(u16, V)],
    chunks: Vec<Vec<u8>>,
    tiled: Option<(u32, u32)>,
    rows_per_strip: Option<u32>,
) -> Ifd {
    let mut all = vec![(256, V::Long(vec![width])), (257, V::Long(vec![height]))];
    if let Some((tw, th)) = tiled {
        all.push((322, V::Short(vec![tw as u16])));
        all.push((323, V::Short(vec![th as u16])));
    }
    if let Some(rows) = rows_per_strip {
        all.push((278, V::Long(vec![rows])));
    }
    all.extend_from_slice(tags);
    Ifd {
        tags: all,
        chunks,
        tiled: tiled.is_some(),
        sub: Vec::new(),
    }
}

// --- Encoders for the chunks ----------------------------------------------------------

fn lzw(data: &[u8]) -> Vec<u8> {
    weezl::encode::Encoder::with_tiff_size_switch(weezl::BitOrder::Msb, 8)
        .encode(data)
        .unwrap()
}

fn deflate(data: &[u8]) -> Vec<u8> {
    miniz_oxide::deflate::compress_to_vec_zlib(data, 6)
}

fn packbits(data: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < data.len() {
        let mut run = 1;
        while i + run < data.len() && data[i + run] == data[i] && run < 128 {
            run += 1;
        }
        if run >= 2 {
            out.push((1i16 - run as i16) as u8);
            out.push(data[i]);
            i += run;
        } else {
            let start = i;
            while i < data.len()
                && i - start < 128
                && (i + 1 >= data.len() || data[i + 1] != data[i])
            {
                i += 1;
            }
            if i == start {
                i += 1;
            }
            out.push((i - start - 1) as u8);
            out.extend_from_slice(&data[start..i]);
        }
    }
    out
}

/// Horizontal differencing of `samples`-sample pixels of `size`-byte samples.
fn predict(data: &[u8], row_bytes: usize, samples: usize, size: usize, le: bool) -> Vec<u8> {
    let mut out = data.to_vec();
    let read = |b: &[u8]| -> u64 {
        let mut v = 0u64;
        for i in 0..size {
            v = v << 8 | u64::from(if le { b[size - 1 - i] } else { b[i] });
        }
        v
    };
    for (row, original) in out
        .chunks_exact_mut(row_bytes)
        .zip(data.chunks_exact(row_bytes))
    {
        let stride = samples * size;
        let mut at = stride;
        while at + size <= row.len() {
            let here = read(&original[at..at + size]);
            let left = read(&original[at - stride..at - stride + size]);
            let d = here.wrapping_sub(left);
            for i in 0..size {
                let byte = (d >> (8 * i)) as u8;
                if le {
                    row[at + i] = byte;
                } else {
                    row[at + size - 1 - i] = byte;
                }
            }
            at += size;
        }
    }
    out
}

/// The floating-point predictor (3) of big-endian `size`-byte samples.
fn predict_float(values: &[f32], width: usize, samples: usize) -> Vec<u8> {
    let size = 4;
    let mut out = Vec::new();
    for row in values.chunks_exact(width * samples) {
        let n = row.len();
        let mut planes = vec![0u8; n * size];
        for (i, v) in row.iter().enumerate() {
            for (b, byte) in v.to_be_bytes().iter().enumerate() {
                planes[b * n + i] = *byte;
            }
        }
        for i in (samples..planes.len()).rev() {
            planes[i] = planes[i].wrapping_sub(planes[i - samples]);
        }
        out.extend_from_slice(&planes);
    }
    out
}

fn u16s(values: &[u16], le: bool) -> Vec<u8> {
    values
        .iter()
        .flat_map(|v| if le { v.to_le_bytes() } else { v.to_be_bytes() })
        .collect()
}

// --- Reading as the page does ---------------------------------------------------------

/// Packs ranges as `iki_tiff_layout` takes them.
fn pack(size: u64, ranges: &[(u64, Vec<u8>)]) -> Vec<u8> {
    let mut input = Vec::new();
    input.extend_from_slice(&size.to_le_bytes());
    input.extend_from_slice(&0u32.to_le_bytes());
    for (offset, bytes) in ranges {
        input.extend_from_slice(&offset.to_le_bytes());
        input.extend_from_slice(&(bytes.len() as u32).to_le_bytes());
        input.extend_from_slice(bytes);
    }
    input
}

/// Walks the file as the page does, reading each range it asks for, and how many rounds
/// it took.
fn walk(file: &[u8]) -> Result<(Layout, usize), String> {
    let mut ranges: Vec<(u64, Vec<u8>)> = Vec::new();
    for round in 1..=64 {
        match tiff::layout(&pack(file.len() as u64, &ranges))? {
            Step::Layout(layout) => return Ok((layout, round)),
            Step::Need(spans) => {
                assert!(!spans.is_empty() && spans.len() <= 16);
                for Span { offset, length } in spans {
                    assert!(length <= 8 * 1024 * 1024, "asked for {length} bytes");
                    assert!(offset + length <= file.len() as u64);
                    let (o, l) = (offset as usize, length as usize);
                    ranges.push((offset, file[o..o + l].to_vec()));
                }
            }
        }
    }
    Err("The layout never finished.".to_owned())
}

fn read_layout(file: &[u8]) -> Layout {
    walk(file).unwrap().0
}

/// Decodes chunk `i` of `stored`, `height` rows of it, with a window if given.
fn decode(
    file: &[u8],
    stored: &Stored,
    i: usize,
    height: u32,
    extra: serde_json::Value,
) -> Result<(tiff::Tile, Vec<u8>), String> {
    let mut request = serde_json::to_value(&stored.pixels).unwrap();
    let object = request.as_object_mut().unwrap();
    object.insert("width".into(), stored.tile_width.into());
    object.insert("height".into(), height.into());
    if let Some(Span { offset, length }) = stored.jpeg_tables {
        let tables = &file[offset as usize..(offset + length) as usize];
        use base64::Engine as _;
        object.insert(
            "jpeg_tables".into(),
            base64::engine::general_purpose::STANDARD
                .encode(tables)
                .into(),
        );
    }
    for (k, v) in extra.as_object().unwrap() {
        object.insert(k.clone(), v.clone());
    }
    let json = serde_json::to_vec(&request).unwrap();
    let (offset, count) = (stored.offsets[i] as usize, stored.byte_counts[i] as usize);
    let mut input = json.clone();
    input.extend_from_slice(&file[offset..offset + count]);
    tiff::tile(&input, json.len())
}

fn tile(file: &[u8], stored: &Stored, i: usize) -> (tiff::Tile, Vec<u8>) {
    decode(file, stored, i, stored.tile_height, serde_json::json!({})).unwrap()
}

fn grey(pixels: &[u8]) -> Vec<u8> {
    pixels
        .chunks_exact(4)
        .map(|p| {
            assert_eq!(p[0], p[1]);
            assert_eq!(p[1], p[2]);
            p[0]
        })
        .collect()
}

fn samples_path(name: &str) -> Vec<u8> {
    let path = format!(
        "{}/../../../server/tests/data/previews/{name}",
        env!("CARGO_MANIFEST_DIR")
    );
    std::fs::read(path).unwrap()
}

// --- Files written here ---------------------------------------------------------------

#[test]
fn grey_strips_uncompressed_little_endian() {
    let (w, h) = (7u32, 5u32);
    let value = |x: u32, y: u32| ((x * 7 + y * 13) % 256) as u8;
    let rows: Vec<Vec<u8>> = (0..h)
        .map(|y| (0..w).map(|x| value(x, y)).collect())
        .collect();
    let strips = vec![
        rows[0..2].concat(),
        rows[2..4].concat(),
        rows[4..5].concat(),
    ];
    let ifd = image(
        w,
        h,
        &[(258, V::Short(vec![8])), (262, V::Short(vec![1]))],
        strips,
        None,
        Some(2),
    );
    let file = write_tiff(&[ifd], false, true);
    let layout = read_layout(&file);
    assert!(!layout.bigtiff);
    assert_eq!(layout.images.len(), 1);
    let s = &layout.images[0].stored;
    assert_eq!(
        (s.width, s.height, s.tiled, s.tile_width, s.tile_height),
        (7, 5, false, 7, 2)
    );
    assert_eq!(s.offsets.len(), 3);
    assert!(s.pixels.little_endian);
    assert_eq!(s.unsupported, None);
    let (t, px) = tile(&file, s, 1);
    assert_eq!((t.width, t.height, px.len()), (7, 2, 7 * 2 * 4));
    assert_eq!(grey(&px), rows[2..4].concat());
    assert!(px.chunks_exact(4).all(|p| p[3] == 255));
    // The last strip has one row.
    let (_, px) = decode(&file, s, 2, 1, serde_json::json!({})).unwrap();
    assert_eq!(grey(&px), rows[4]);
}

#[test]
fn tiled_lzw_16_bit_with_predictor_big_endian() {
    let (w, h, tw, th) = (20u32, 18u32, 16u32, 16u32);
    let value = |x: u32, y: u32| ((x + y * 12) * 100) as u16;
    let mut tiles = Vec::new();
    for ty in 0..2 {
        for tx in 0..2 {
            // Edge tiles are whole, padded with zeros.
            let mut samples = Vec::new();
            for y in 0..th {
                for x in 0..tw {
                    let (gx, gy) = (tx * tw + x, ty * th + y);
                    samples.push(if gx < w && gy < h { value(gx, gy) } else { 0 });
                }
            }
            let raw = u16s(&samples, false);
            tiles.push(lzw(&predict(&raw, tw as usize * 2, 1, 2, false)));
        }
    }
    let ifd = image(
        w,
        h,
        &[
            (258, V::Short(vec![16])),
            (259, V::Short(vec![5])),
            (262, V::Short(vec![1])),
            (317, V::Short(vec![2])),
        ],
        tiles,
        Some((tw, th)),
        None,
    );
    let file = write_tiff(&[ifd], false, false);
    let layout = read_layout(&file);
    let s = &layout.images[0].stored;
    assert!(s.tiled && !s.pixels.little_endian);
    assert_eq!((s.tile_width, s.tile_height, s.offsets.len()), (16, 16, 4));
    assert_eq!(s.pixels.predictor, 2);
    // The second tile across: x from 16 to 19 are the image's, then padding.
    let (t, px) = decode(
        &file,
        s,
        1,
        16,
        serde_json::json!({"window": [0.0, 25_500.0]}),
    )
    .unwrap();
    assert_eq!((t.min, t.max), (0.0, f64::from(value(19, 15))));
    let bytes = grey(&px);
    for y in 0..16usize {
        for x in 0..4usize {
            // value / 25500 × 255 = (x + 12y) exactly.
            assert_eq!(
                u32::from(bytes[y * 16 + x]),
                (16 + x as u32) + y as u32 * 12,
                "{x},{y}"
            );
        }
        assert_eq!(bytes[y * 16 + 5], 0);
    }
    // Without a window, the tile's own range.
    let (_, px) = tile(&file, s, 0);
    let bytes = grey(&px);
    assert_eq!((bytes[0], bytes[255]), (0, 255));
}

#[test]
fn bigtiff_rgb_deflate_tiles() {
    for le in [true, false] {
        let (w, h) = (32u32, 16u32);
        let pixel = |x: u32, y: u32| [(x * 8) as u8, (y * 16) as u8, ((x + y) * 3) as u8];
        let tile_bytes = |tx: u32| -> Vec<u8> {
            (0..16)
                .flat_map(|y| (0..16).flat_map(move |x| pixel(tx * 16 + x, y)))
                .collect()
        };
        let tiles = vec![deflate(&tile_bytes(0)), deflate(&tile_bytes(1))];
        let ifd = image(
            w,
            h,
            &[
                (258, V::Short(vec![8, 8, 8])),
                (259, V::Short(vec![8])),
                (262, V::Short(vec![2])),
                (277, V::Short(vec![3])),
            ],
            tiles,
            Some((16, 16)),
            None,
        );
        let file = write_tiff(&[ifd], true, le);
        let (layout, rounds) = walk(&file).unwrap();
        assert!(layout.bigtiff);
        assert!(rounds <= 2, "{rounds} rounds");
        let s = &layout.images[0].stored;
        assert_eq!(s.pixels.samples_per_pixel, 3);
        let (t, px) = tile(&file, s, 1);
        assert_eq!((t.min, t.max), (0.0, 248.0));
        for (i, p) in px.chunks_exact(4).enumerate() {
            let (x, y) = (16 + (i % 16) as u32, (i / 16) as u32);
            let [r, g, b] = pixel(x, y);
            assert_eq!(p, [r, g, b, 255]);
        }
    }
}

#[test]
fn palette_packbits_big_endian_bigtiff() {
    let (w, h) = (12u32, 3u32);
    let indices: Vec<u8> = (0..w * h).map(|i| (i % 4) as u8).collect();
    let mut colormap = vec![0u16; 3 * 256];
    for (i, rgb) in [
        [65535u16, 0, 0],
        [0, 65535, 0],
        [0, 0, 65535],
        [32896, 32896, 32896],
    ]
    .iter()
    .enumerate()
    {
        colormap[i] = rgb[0];
        colormap[256 + i] = rgb[1];
        colormap[512 + i] = rgb[2];
    }
    let ifd = image(
        w,
        h,
        &[
            (258, V::Short(vec![8])),
            (259, V::Short(vec![32773])),
            (262, V::Short(vec![3])),
            (320, V::Short(colormap)),
        ],
        vec![packbits(&indices)],
        None,
        None,
    );
    let file = write_tiff(&[ifd], true, false);
    let layout = read_layout(&file);
    let s = &layout.images[0].stored;
    assert_eq!(s.pixels.colormap.as_ref().map(Vec::len), Some(768));
    let (t, px) = tile(&file, s, 0);
    assert_eq!((t.min, t.max), (0.0, 3.0));
    let expect = [
        [255u8, 0, 0, 255],
        [0, 255, 0, 255],
        [0, 0, 255, 255],
        [128, 128, 128, 255],
    ];
    for (i, p) in px.chunks_exact(4).enumerate() {
        assert_eq!(p, expect[i % 4]);
    }
}

#[test]
fn float32_with_the_floating_point_predictor() {
    let (w, h) = (16u32, 16u32);
    let values: Vec<f32> = (0..h)
        .flat_map(|y| (0..w).map(move |x| x as f32 * 0.5 - y as f32 * 0.25))
        .collect();
    let chunk = deflate(&predict_float(&values, w as usize, 1));
    let ifd = image(
        w,
        h,
        &[
            (258, V::Short(vec![32])),
            (259, V::Short(vec![8])),
            (262, V::Short(vec![1])),
            (317, V::Short(vec![3])),
            (339, V::Short(vec![3])),
        ],
        vec![chunk],
        Some((16, 16)),
        None,
    );
    for le in [true, false] {
        let file = write_tiff(std::slice::from_ref(&ifd), false, le);
        let layout = read_layout(&file);
        let s = &layout.images[0].stored;
        let (t, px) = decode(&file, s, 0, 16, serde_json::json!({"window": [-3.75, 7.5]})).unwrap();
        assert_eq!((t.min, t.max), (-3.75, 7.5));
        let bytes = grey(&px);
        // (v + 3.75) / 11.25 × 255.
        for (i, v) in values.iter().enumerate() {
            let expected = ((f64::from(*v) + 3.75) / 11.25 * 255.0).round() as u8;
            assert_eq!(bytes[i], expected);
        }
    }
}

#[test]
fn float_nan_is_black_and_min_is_white_inverts() {
    let mut values = vec![1.0f32, 2.0, f32::NAN, 3.0];
    let raw: Vec<u8> = values.iter().flat_map(|v| v.to_be_bytes()).collect();
    let ifd = image(
        4,
        1,
        &[
            (258, V::Short(vec![32])),
            (262, V::Short(vec![1])),
            (339, V::Short(vec![3])),
        ],
        vec![raw],
        None,
        None,
    );
    let file = write_tiff(&[ifd], false, false);
    let layout = read_layout(&file);
    let (t, px) = tile(&file, &layout.images[0].stored, 0);
    assert_eq!((t.min, t.max), (1.0, 3.0));
    assert_eq!(
        px,
        [
            0, 0, 0, 255, 128, 128, 128, 255, 0, 0, 0, 255, 255, 255, 255, 255
        ]
    );
    values.clear();

    let ifd = image(
        3,
        1,
        &[(258, V::Short(vec![8])), (262, V::Short(vec![0]))],
        vec![vec![0, 100, 255]],
        None,
        None,
    );
    let file = write_tiff(&[ifd], false, true);
    let layout = read_layout(&file);
    let (_, px) = tile(&file, &layout.images[0].stored, 0);
    assert_eq!(grey(&px), [255, 155, 0]);
}

#[test]
fn signed_samples() {
    let raw: Vec<u8> = [-32768i16, 0, 32767]
        .iter()
        .flat_map(|v| v.to_le_bytes())
        .collect();
    let ifd = image(
        3,
        1,
        &[
            (258, V::Short(vec![16])),
            (262, V::Short(vec![1])),
            (339, V::Short(vec![2])),
        ],
        vec![raw],
        None,
        None,
    );
    let file = write_tiff(&[ifd], false, true);
    let layout = read_layout(&file);
    let (t, px) = tile(&file, &layout.images[0].stored, 0);
    assert_eq!((t.min, t.max), (-32768.0, 32767.0));
    assert_eq!(grey(&px), [0, 128, 255]);

    let ifd = image(
        3,
        1,
        &[
            (258, V::Short(vec![8])),
            (262, V::Short(vec![1])),
            (339, V::Short(vec![2])),
        ],
        vec![vec![0x80, 0, 0x7f]],
        None,
        None,
    );
    let file = write_tiff(&[ifd], false, true);
    let layout = read_layout(&file);
    let (t, px) = tile(&file, &layout.images[0].stored, 0);
    assert_eq!((t.min, t.max), (-128.0, 127.0));
    assert_eq!(grey(&px), [0, 128, 255]);
}

#[test]
fn bilevel_and_4_bit_palette() {
    // 1 bit, min-is-black: rows padded to whole bytes.
    let ifd = image(
        10,
        2,
        &[(258, V::Short(vec![1])), (262, V::Short(vec![1]))],
        vec![vec![0b1010_0000, 0b1100_0000, 0b0000_0001, 0b0100_0000]],
        None,
        None,
    );
    let file = write_tiff(&[ifd], false, true);
    let layout = read_layout(&file);
    let (_, px) = tile(&file, &layout.images[0].stored, 0);
    assert_eq!(
        grey(&px),
        [
            255, 0, 255, 0, 0, 0, 0, 0, 255, 255, 0, 0, 0, 0, 0, 0, 0, 255, 0, 255
        ]
    );

    let mut colormap = vec![0u16; 48];
    colormap[15] = 65535; // index 15 is red
    colormap[16 + 1] = 65535; // index 1 is green
    let ifd = image(
        3,
        1,
        &[
            (258, V::Short(vec![4])),
            (262, V::Short(vec![3])),
            (320, V::Short(colormap)),
        ],
        vec![vec![0xf1, 0x00]],
        None,
        None,
    );
    let file = write_tiff(&[ifd], false, true);
    let layout = read_layout(&file);
    let (t, px) = tile(&file, &layout.images[0].stored, 0);
    assert_eq!((t.min, t.max), (0.0, 15.0));
    assert_eq!(px, [255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 0, 255]);
}

#[test]
fn planar_rgb_decodes_a_plane_as_grey() {
    let planes: Vec<Vec<u8>> = (0..3u8)
        .map(|p| vec![p * 10, p * 10 + 1, p * 10 + 2, p * 10 + 3])
        .collect();
    let ifd = image(
        2,
        2,
        &[
            (258, V::Short(vec![8, 8, 8])),
            (262, V::Short(vec![2])),
            (277, V::Short(vec![3])),
            (284, V::Short(vec![2])),
        ],
        planes,
        None,
        None,
    );
    let file = write_tiff(&[ifd], false, true);
    let layout = read_layout(&file);
    let s = &layout.images[0].stored;
    assert_eq!((s.pixels.planar, s.offsets.len()), (2, 3));
    let (_, px) = tile(&file, s, 2);
    assert_eq!(grey(&px), [20, 21, 22, 23]);
    // Every plane's chunk, with their lengths: composed into RGB.
    let mut request = serde_json::to_value(&s.pixels).unwrap();
    let object = request.as_object_mut().unwrap();
    object.insert("width".into(), 2.into());
    object.insert("height".into(), 2.into());
    object.insert("plane_lengths".into(), serde_json::json!([4, 4, 4]));
    let json = serde_json::to_vec(&request).unwrap();
    let mut input = json.clone();
    for i in 0..3 {
        let (o, n) = (s.offsets[i] as usize, s.byte_counts[i] as usize);
        input.extend_from_slice(&file[o..o + n]);
    }
    let (t, px) = tiff::tile(&input, json.len()).unwrap();
    assert_eq!((t.min, t.max), (0.0, 23.0));
    assert_eq!(px[..8], [0, 10, 20, 255, 1, 11, 21, 255]);
    // Lengths that don't add up to the bytes given.
    let mut bad = request.clone();
    bad["plane_lengths"] = serde_json::json!([4, 4, 5]);
    let json = serde_json::to_vec(&bad).unwrap();
    let mut short = json.clone();
    short.extend_from_slice(&input[input.len() - 12..]);
    assert!(
        tiff::tile(&short, json.len())
            .unwrap_err()
            .contains("don't add up")
    );
}

#[test]
fn several_samples_show_the_one_asked_for() {
    let raw: Vec<u8> = u16s(&[1, 1000, 2, 2000], true);
    let ifd = image(
        2,
        1,
        &[
            (258, V::Short(vec![16, 16])),
            (262, V::Short(vec![1])),
            (277, V::Short(vec![2])),
        ],
        vec![raw],
        None,
        None,
    );
    let file = write_tiff(&[ifd], false, true);
    let layout = read_layout(&file);
    let s = &layout.images[0].stored;
    let (t, _) = decode(&file, s, 0, 1, serde_json::json!({"sample": 1})).unwrap();
    assert_eq!((t.min, t.max), (1000.0, 2000.0));
    let error = decode(&file, s, 0, 1, serde_json::json!({"sample": 2})).unwrap_err();
    assert!(error.contains("no sample 2"), "{error}");
}

#[test]
fn subifd_pyramid_levels_largest_first() {
    let level = |size: u32, fill: u8| {
        image(
            size,
            size,
            &[
                (258, V::Short(vec![8])),
                (262, V::Short(vec![1])),
                (254, V::Long(vec![1])),
            ],
            vec![vec![fill; 16 * 16]; ((size.div_ceil(16)) * size.div_ceil(16)) as usize],
            Some((16, 16)),
            None,
        )
    };
    for big in [false, true] {
        let mut full = image(
            64,
            64,
            &[(258, V::Short(vec![8])), (262, V::Short(vec![1]))],
            vec![vec![9; 256]; 16],
            Some((16, 16)),
            None,
        );
        // Written smallest first: the layout sorts them.
        full.sub = vec![level(16, 3), level(32, 7)];
        let file = write_tiff(&[full], big, !big);
        let layout = read_layout(&file);
        assert_eq!(layout.images.len(), 1);
        let image = &layout.images[0];
        assert_eq!(
            image.levels.iter().map(|l| l.width).collect::<Vec<_>>(),
            [32, 16]
        );
        assert_eq!(image.levels[0].offsets.len(), 4);
        let (_, px) = tile(&file, &image.levels[1], 0);
        assert_eq!(grey(&px), vec![3; 256]);
    }
}

#[test]
fn reduced_images_in_the_main_chain_are_levels_and_a_label_is_not() {
    let tiled = |w: u32, h: u32, subfile: u32| {
        let mut tags = vec![(258, V::Short(vec![8])), (262, V::Short(vec![1]))];
        if subfile > 0 {
            tags.push((254, V::Long(vec![subfile])));
        }
        image(
            w,
            h,
            &tags,
            vec![vec![0; 256]; (w.div_ceil(16) * h.div_ceil(16)) as usize],
            Some((16, 16)),
            None,
        )
    };
    let stripped = |w: u32, h: u32, subfile: u32| {
        image(
            w,
            h,
            &[
                (258, V::Short(vec![8])),
                (262, V::Short(vec![1])),
                (254, V::Long(vec![subfile])),
            ],
            vec![vec![0; (w * h) as usize]],
            None,
            None,
        )
    };
    // As Aperio writes a slide: the full image, a thumbnail, levels (not marked reduced),
    // a label, then a macro image.
    let ifds = [
        tiled(400, 300, 0),
        stripped(100, 75, 0),
        tiled(200, 150, 0),
        tiled(100, 75, 0),
        stripped(60, 60, 1),
        stripped(300, 100, 9),
    ];
    let file = write_tiff(&ifds, false, true);
    let layout = read_layout(&file);
    let summary: Vec<(usize, u32, Vec<u32>)> = layout
        .images
        .iter()
        .map(|i| {
            (
                i.index,
                i.stored.width,
                i.levels.iter().map(|l| l.width).collect(),
            )
        })
        .collect();
    assert_eq!(
        summary,
        [
            (0, 400, vec![200, 100]),
            (1, 100, vec![]),
            (4, 60, vec![]),
            (5, 300, vec![])
        ]
    );
    // Marked reduced, in strips too.
    let ifds = [
        tiled(400, 300, 0),
        stripped(200, 150, 1),
        stripped(100, 75, 1),
        tiled(400, 300, 0),
    ];
    let file = write_tiff(&ifds, false, true);
    let layout = read_layout(&file);
    let summary: Vec<(usize, Vec<u32>)> = layout
        .images
        .iter()
        .map(|i| (i.index, i.levels.iter().map(|l| l.width).collect()))
        .collect();
    assert_eq!(summary, [(0, vec![200, 100]), (3, vec![])]);
}

#[test]
fn ome_xml_two_channels_two_planes() {
    let xml = r#"<?xml version="1.0" encoding="UTF-8"?>
<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">
<Image ID="Image:0"><Pixels ID="Pixels:0" DimensionOrder="XYZCT" Type="uint8" SizeX="4" SizeY="2" SizeC="2" SizeZ="2" SizeT="1" PhysicalSizeX="0.5" PhysicalSizeXUnit="nm" PhysicalSizeY="0.5">
<Channel ID="Channel:0:0" Name="Hoechst" Color="-16776961"/><Channel ID="Channel:0:1" Name="Alexa 488" Color="16711935"/>
<TiffData IFD="0" PlaneCount="4"/></Pixels></Image></OME>"#;
    let plane = |fill: u8, first: bool| {
        let mut tags = vec![(258, V::Short(vec![8])), (262, V::Short(vec![1]))];
        if first {
            tags.push((270, V::Ascii(xml.to_owned())));
        }
        image(4, 2, &tags, vec![vec![fill; 8]], None, None)
    };
    let file = write_tiff(
        &[
            plane(10, true),
            plane(20, false),
            plane(30, false),
            plane(40, false),
        ],
        false,
        true,
    );
    let layout = read_layout(&file);
    assert_eq!(layout.images.len(), 4);
    assert!(layout.description.as_deref().unwrap().starts_with("<?xml"));
    let ome = layout.ome.unwrap();
    assert_eq!(
        (ome.size_x, ome.size_y, ome.size_c, ome.size_z, ome.size_t),
        (4, 2, 2, 2, 1)
    );
    assert_eq!(ome.dimension_order, "XYZCT");
    assert_eq!(
        ome.channels
            .iter()
            .map(|c| c.name.clone().unwrap())
            .collect::<Vec<_>>(),
        ["Hoechst", "Alexa 488"]
    );
    assert_eq!(ome.channels[0].color, Some(-16_776_961));
    assert_eq!(
        (ome.physical_size_x, ome.physical_size_unit.as_deref()),
        (Some(0.5), Some("nm"))
    );
    let planes: Vec<(u32, u32, usize)> = ome.planes.iter().map(|p| (p.c, p.z, p.ifd)).collect();
    // XYZCT: z varies fastest.
    assert_eq!(planes, [(0, 0, 0), (0, 1, 1), (1, 0, 2), (1, 1, 3)]);
    let (_, px) = tile(&file, &layout.images[2].stored, 0);
    assert_eq!(grey(&px), [30; 8]);
}

#[test]
fn one_large_uncompressed_strip_is_listed_as_smaller_strips() {
    let (w, h) = (4_200u32, 4_100u32);
    let data: Vec<u8> = (0..w * h).map(|i| (i / w % 251) as u8).collect();
    let ifd = image(
        w,
        h,
        &[(258, V::Short(vec![8])), (262, V::Short(vec![1]))],
        vec![data],
        None,
        None,
    );
    let file = write_tiff(&[ifd], false, true);
    let layout = read_layout(&file);
    let s = &layout.images[0].stored;
    assert_eq!(s.unsupported, None);
    let rows = (4 * 1024 * 1024) / 4_200;
    assert_eq!(s.tile_height, rows);
    assert_eq!(s.offsets.len(), h.div_ceil(rows) as usize);
    assert_eq!(s.byte_counts[0], u64::from(rows * w));
    let last = s.offsets.len() - 1;
    assert_eq!(s.byte_counts[last], u64::from((h - last as u32 * rows) * w));
    let (_, px) = tile(&file, s, 1);
    // Strip 1 starts at row `rows`.
    assert_eq!(px[0], (rows % 251) as u8);
    assert_eq!(px[(w as usize) * 4], ((rows + 1) % 251) as u8);
}

// --- Samples written by tifffile and libtiff ------------------------------------------

#[test]
fn tifffile_ome_pyramid() {
    let file = samples_path("ome_pyramid.ome.tif");
    let (layout, rounds) = walk(&file).unwrap();
    assert!(rounds <= 3, "{rounds}");
    assert_eq!(layout.images.len(), 4);
    let ome = layout.ome.as_ref().unwrap();
    assert_eq!(
        (ome.size_x, ome.size_y, ome.size_c, ome.size_z),
        (128, 96, 2, 2)
    );
    assert_eq!(
        ome.channels
            .iter()
            .map(|c| c.name.clone().unwrap())
            .collect::<Vec<_>>(),
        ["DAPI", "GFP"]
    );
    assert_eq!(ome.physical_size_unit.as_deref(), Some("\u{b5}m"));
    assert_eq!(ome.planes.len(), 4);
    // tifffile writes CZYX as XYZCT: plane 3 is c 1, z 1.
    assert_eq!(
        (ome.planes[3].c, ome.planes[3].z, ome.planes[3].ifd),
        (1, 1, 3)
    );
    for (index, image) in layout.images.iter().enumerate() {
        let (c, z) = (index / 2, index % 2);
        let base = (c * 10_000 + z * 1_000) as f64;
        assert_eq!(
            image
                .levels
                .iter()
                .map(|l| (l.width, l.height))
                .collect::<Vec<_>>(),
            [(64, 48), (32, 24)]
        );
        let s = &image.stored;
        assert_eq!((s.tile_width, s.tile_height, s.offsets.len()), (64, 64, 4));
        // Tile 1: x from 64, y from 0. Values are base + (2y + x) mod 256.
        let (t, px) = decode(
            &file,
            s,
            1,
            64,
            serde_json::json!({"window": [base, base + 255.0]}),
        )
        .unwrap();
        // 2y + x is at most 63 × 2 + 127 = 253 in it.
        assert_eq!((t.min, t.max), (base + 64.0, base + 253.0));
        let bytes = grey(&px);
        for (y, x) in [(0usize, 0usize), (5, 3), (47, 63)] {
            assert_eq!(
                usize::from(bytes[y * 64 + x]),
                (2 * y + 64 + x) % 256,
                "{index} {x},{y}"
            );
        }
        // The half-size level: each value is the full image's at twice its place.
        let (_, px) = decode(
            &file,
            &image.levels[0],
            0,
            64,
            serde_json::json!({"window": [base, base + 255.0]}),
        )
        .unwrap();
        let bytes = grey(&px);
        assert_eq!(usize::from(bytes[10 * 64 + 20]), (2 * 20 + 40) % 256);
    }
}

#[test]
fn tifffile_float_bigtiff_with_predictor() {
    let file = samples_path("float_predictor3_bigtiff.tif");
    let layout = read_layout(&file);
    assert!(layout.bigtiff);
    let s = &layout.images[0].stored;
    assert!(!s.pixels.little_endian);
    assert_eq!(
        (s.pixels.predictor, s.pixels.sample_format, s.offsets.len()),
        (3, 3, 4)
    );
    let (t, px) = decode(
        &file,
        s,
        0,
        32,
        serde_json::json!({"window": [-10.0, 15.5]}),
    )
    .unwrap();
    assert_eq!((t.min, t.max), (-7.75, 15.5));
    // x * 0.5 - y * 0.25; NaN at row 3, column 4.
    for (x, y) in [(0usize, 0usize), (31, 0), (10, 31), (7, 2)] {
        let v = x as f64 * 0.5 - y as f64 * 0.25;
        let expected = ((v + 10.0) / 25.5 * 255.0).round() as u8;
        assert_eq!(px[(y * 32 + x) * 4], expected, "{x},{y}");
    }
    assert_eq!(px[(3 * 32 + 4) * 4..][..4], [0, 0, 0, 255]);
}

fn assert_close(
    px: &[u8],
    width: usize,
    expected: impl Fn(usize, usize) -> [u8; 3],
    w: usize,
    h: usize,
) {
    let mut total = 0u64;
    for y in 0..h {
        for x in 0..w {
            let p = &px[(y * width + x) * 4..][..4];
            let e = expected(x, y);
            for c in 0..3 {
                total += u64::from(p[c].abs_diff(e[c]));
            }
            assert_eq!(p[3], 255);
        }
    }
    let mean = total as f64 / (w * h * 3) as f64;
    assert!(mean < 3.0, "mean error {mean}");
}

#[test]
fn jpeg_tiles_without_tables_ycbcr() {
    let file = samples_path("jpeg_ycbcr_tiles.tif");
    let layout = read_layout(&file);
    let s = &layout.images[0].stored;
    assert_eq!(
        (s.pixels.compression, s.pixels.photometric, s.jpeg_tables),
        (7, 6, None)
    );
    // Tile 4: x from 32, y from 32, of a 96 × 64 image in 32 × 32 tiles.
    let (_, px) = tile(&file, s, 4);
    let rgb = |x: usize, y: usize| -> [u8; 3] {
        let (x, y) = (x + 32, y + 32);
        [(x * 2) as u8, (y * 3) as u8, (x + y) as u8]
    };
    assert_close(&px, 32, rgb, 32, 32);
}

#[test]
fn jpeg_strips_with_tables_rgb() {
    let file = samples_path("jpeg_rgb_strips.tif");
    let layout = read_layout(&file);
    let s = &layout.images[0].stored;
    assert_eq!((s.pixels.compression, s.pixels.photometric), (7, 2));
    assert!(s.jpeg_tables.is_some());
    let (_, px) = tile(&file, s, 0);
    let rgb = |x: usize, y: usize| -> [u8; 3] { [(x * 2) as u8, (y * 3) as u8, (x + y) as u8] };
    assert_close(&px, 96, rgb, 96, s.tile_height.min(64) as usize);
}

#[test]
fn libtiff_palette_lzw() {
    let file = samples_path("palette_lzw.tif");
    let layout = read_layout(&file);
    let s = &layout.images[0].stored;
    assert_eq!((s.pixels.compression, s.pixels.photometric), (5, 3));
    let colours = [[255u8, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 255]];
    let mut row = 0usize;
    for i in 0..s.offsets.len() {
        let rows = (s.tile_height as usize).min(64 - row);
        let (_, px) = decode(&file, s, i, rows as u32, serde_json::json!({})).unwrap();
        for y in 0..rows {
            for x in 0..96usize {
                let index = (x / 12 + (row + y) / 16) % 4;
                assert_eq!(
                    px[(y * 96 + x) * 4..][..3],
                    colours[index],
                    "{x},{}",
                    row + y
                );
            }
        }
        row += rows;
    }
}

#[test]
fn libtiff_bilevel_packbits() {
    let file = samples_path("bilevel_packbits.tif");
    let layout = read_layout(&file);
    let s = &layout.images[0].stored;
    assert_eq!((s.pixels.bits_per_sample, s.pixels.compression), (1, 32773));
    let rows = (s.tile_height as usize).min(64);
    let (_, px) = decode(&file, s, 0, rows as u32, serde_json::json!({})).unwrap();
    let bytes = grey(&px);
    for y in 0..rows {
        for x in 0..96 {
            let on = (x + y) % 3 == 0;
            assert_eq!(bytes[y * 96 + x], if on { 255 } else { 0 }, "{x},{y}");
        }
    }
}

// --- Broken and hostile files ---------------------------------------------------------

fn grey8(w: u32, h: u32) -> Ifd {
    image(
        w,
        h,
        &[(258, V::Short(vec![8])), (262, V::Short(vec![1]))],
        vec![vec![1; (w * h) as usize]],
        None,
        None,
    )
}

#[test]
fn an_ifd_loop_stops() {
    let mut file = write_tiff(&[grey8(2, 2), grey8(3, 3)], false, true);
    // Point the second IFD's next pointer back at the first.
    let first = u32::from_le_bytes(file[4..8].try_into().unwrap());
    let len = file.len();
    file[len - 4..].copy_from_slice(&first.to_le_bytes());
    let layout = read_layout(&file);
    assert_eq!(layout.images.len(), 2);
    assert!(layout.truncated);
}

#[test]
fn not_a_tiff() {
    assert!(
        walk(b"GIF89a\0\0\0\0")
            .unwrap_err()
            .contains("isn't a TIFF")
    );
    assert!(
        walk(b"II\x2b\x00\x04\x00\x00\x00\0\0\0\0\0\0\0\0")
            .unwrap_err()
            .contains("BigTIFF")
    );
    assert!(
        walk(b"II\x2a\x00\xff\xff\x00\x00")
            .unwrap_err()
            .contains("past the end")
    );
    assert!(tiff::layout(b"short").is_err());
    // A range longer than the input holds.
    let mut input = pack(100, &[]);
    input.extend_from_slice(&0u64.to_le_bytes());
    input.extend_from_slice(&50u32.to_le_bytes());
    assert!(tiff::layout(&input).unwrap_err().contains("cut short"));
}

#[test]
fn huge_counts_and_dimensions_are_refused() {
    // An IFD claiming 65,535 tags.
    let mut file = b"II\x2a\x00\x08\x00\x00\x00\xff\xff".to_vec();
    file.resize(64, 0);
    assert!(walk(&file).unwrap_err().contains("65535 tags"));
    // StripOffsets claiming 2^31 values.
    let mut ifd = grey8(2, 2);
    ifd.chunks.clear();
    ifd.tags.push((273, V::Raw(4, 1 << 31, 8)));
    ifd.tags.push((279, V::Long(vec![4])));
    let file = write_tiff(&[ifd], false, true);
    let s = &read_layout(&file).images[0].stored;
    assert!(
        s.unsupported
            .as_deref()
            .unwrap()
            .contains("more than a preview reads"),
        "{:?}",
        s.unsupported
    );
    // 2^32 - 1 pixels wide.
    let ifd = image(
        u32::MAX,
        1,
        &[(258, V::Short(vec![8]))],
        vec![vec![0]],
        None,
        None,
    );
    let file = write_tiff(&[ifd], false, true);
    assert!(
        read_layout(&file).images[0]
            .stored
            .unsupported
            .as_deref()
            .unwrap()
            .contains("larger than")
    );
    // A million and more 1 × 1 tiles.
    let mut ifd = image(
        2_000,
        1_000,
        &[
            (258, V::Short(vec![8])),
            (322, V::Short(vec![1])),
            (323, V::Short(vec![1])),
        ],
        Vec::new(),
        None,
        None,
    );
    ifd.tags.push((324, V::Long(vec![8])));
    let file = write_tiff(&[ifd], false, true);
    let layout = read_layout(&file);
    assert!(layout.truncated);
    assert!(
        layout.images[0]
            .stored
            .unsupported
            .as_deref()
            .unwrap()
            .contains("2000000 tiles")
    );
    // Tiles of no size.
    let ifd = image(
        16,
        16,
        &[(258, V::Short(vec![8]))],
        vec![vec![0; 256]],
        Some((0, 16)),
        None,
    );
    let file = write_tiff(&[ifd], false, true);
    assert!(
        walk(&file).unwrap().0.images[0]
            .stored
            .unsupported
            .is_some()
    );
}

#[test]
fn mismatched_and_missing_offsets() {
    let mut ifd = grey8(4, 4);
    ifd.chunks.clear();
    ifd.tags.push((273, V::Long(vec![8, 12])));
    ifd.tags.push((279, V::Long(vec![4])));
    ifd.tags.push((278, V::Long(vec![2])));
    let file = write_tiff(&[ifd], false, true);
    assert!(
        read_layout(&file).images[0]
            .stored
            .unsupported
            .as_deref()
            .unwrap()
            .contains("don't match")
    );
    let mut ifd = grey8(4, 4);
    ifd.chunks.clear();
    ifd.tags.push((273, V::Long(vec![8])));
    ifd.tags.push((279, V::Long(vec![8])));
    ifd.tags.push((278, V::Long(vec![2])));
    let file = write_tiff(&[ifd], false, true);
    assert!(
        read_layout(&file).images[0]
            .stored
            .unsupported
            .as_deref()
            .unwrap()
            .contains("needs 2")
    );
}

#[test]
fn offsets_past_the_end_are_cut_to_it() {
    let mut ifd = grey8(4, 1);
    ifd.chunks.clear();
    ifd.tags.push((273, V::Long(vec![1_000_000])));
    ifd.tags.push((279, V::Long(vec![4])));
    let file = write_tiff(&[ifd], false, true);
    let s = &read_layout(&file).images[0].stored;
    assert_eq!((s.offsets[0], s.byte_counts[0]), (file.len() as u64, 0));
    let error = decode(&file, s, 0, 1, serde_json::json!({})).unwrap_err();
    assert_eq!(error, "The tile's bytes end before its byte count says.");
}

#[test]
fn unsupported_compressions_say_why() {
    for (compression, words) in [
        (33003u16, "JPEG 2000"),
        (50000, "Zstandard"),
        (6, "old-style JPEG"),
        (4, "CCITT"),
        (34887, "LERC"),
        (50001, "WebP"),
        (9, "number 9"),
    ] {
        let ifd = image(
            2,
            2,
            &[(258, V::Short(vec![8])), (259, V::Short(vec![compression]))],
            vec![vec![0; 4]],
            None,
            None,
        );
        let file = write_tiff(&[ifd], false, true);
        let why = read_layout(&file).images[0]
            .stored
            .unsupported
            .clone()
            .unwrap();
        assert!(why.contains(words), "{why}");
    }
    let ifd = image(
        2,
        2,
        &[(258, V::Short(vec![12]))],
        vec![vec![0; 6]],
        None,
        None,
    );
    let file = write_tiff(&[ifd], false, true);
    assert!(
        read_layout(&file).images[0]
            .stored
            .unsupported
            .clone()
            .unwrap()
            .contains("12-bit")
    );
    let ifd = image(
        2,
        2,
        &[
            (258, V::Short(vec![8, 8, 8, 8])),
            (262, V::Short(vec![5])),
            (277, V::Short(vec![4])),
        ],
        vec![vec![0; 16]],
        None,
        None,
    );
    let file = write_tiff(&[ifd], false, true);
    assert!(
        read_layout(&file).images[0]
            .stored
            .unsupported
            .clone()
            .unwrap()
            .contains("CMYK")
    );
}

fn request(width: u32, height: u32, compression: u16, bits: u16) -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({
        "compression": compression, "predictor": 1, "photometric": 1, "bits_per_sample": bits,
        "samples_per_pixel": 1, "sample_format": 1, "planar": 1, "colormap": null,
        "little_endian": true, "width": width, "height": height, "jpeg_tables": null,
        "sample": null, "window": null,
    }))
    .unwrap()
}

fn tile_of(json: &[u8], data: &[u8]) -> Result<(tiff::Tile, Vec<u8>), String> {
    let mut input = json.to_vec();
    input.extend_from_slice(data);
    tiff::tile(&input, json.len())
}

#[test]
fn compression_bombs_stop_at_the_tiles_size() {
    let zeros = vec![0u8; 64 * 1024 * 1024];
    let bomb = deflate(&zeros);
    assert!(bomb.len() < 100_000);
    let (_, px) = tile_of(&request(8, 8, 8, 8), &bomb).unwrap();
    assert_eq!(px.len(), 8 * 8 * 4);
    let bomb = lzw(&zeros[..8 * 1024 * 1024]);
    let (_, px) = tile_of(&request(8, 8, 5, 8), &bomb).unwrap();
    assert_eq!(px.len(), 8 * 8 * 4);
    // PackBits runs of 128, over and over.
    let bomb: Vec<u8> = std::iter::repeat_n([0x81u8, 0x00], 100_000)
        .flatten()
        .collect();
    let (_, px) = tile_of(&request(8, 8, 32773, 8), &bomb).unwrap();
    assert_eq!(px.len(), 8 * 8 * 4);
}

#[test]
fn truncated_and_broken_tiles() {
    let json = request(16, 16, 1, 8);
    assert_eq!(
        tile_of(&json, &[0; 100]).unwrap_err(),
        "The tile's bytes end before its byte count says."
    );
    let data = lzw(&[7u8; 256]);
    assert!(
        tile_of(&request(16, 16, 5, 8), &data[..data.len() / 2])
            .unwrap_err()
            .contains("LZW")
    );
    let data = deflate(&[7u8; 256]);
    assert!(
        tile_of(&request(16, 16, 8, 8), &data[..data.len() / 2])
            .unwrap_err()
            .contains("Deflate")
    );
    assert!(
        tile_of(&request(16, 16, 8, 8), b"\x78\x9cgarbage!")
            .unwrap_err()
            .contains("Deflate")
    );
    assert!(
        tile_of(&request(16, 16, 32773, 8), &[0x05, 1, 2])
            .unwrap_err()
            .contains("PackBits")
    );
    assert!(
        tile_of(&request(16, 16, 7, 8), b"\xff\xd8\xff\xd9")
            .unwrap_err()
            .contains("JPEG")
    );
    assert!(
        tile_of(&request(1 << 15, 1 << 15, 1, 8), &[])
            .unwrap_err()
            .contains("more than a preview")
    );
    assert!(tile_of(&request(0, 4, 1, 8), &[]).is_err());
    assert!(
        tiff::tile(b"{\"nope\":1}", 10)
            .unwrap_err()
            .contains("isn't one")
    );
    assert!(tiff::tile(b"{}", 10).unwrap_err().contains("cut short"));
}

/// A small deterministic generator, for mutating files.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
}

#[test]
fn mutated_files_never_panic() {
    let mut originals = vec![
        samples_path("ome_pyramid.ome.tif"),
        samples_path("jpeg_rgb_strips.tif"),
        samples_path("jpeg_ycbcr_tiles.tif"),
        samples_path("float_predictor3_bigtiff.tif"),
        samples_path("palette_lzw.tif"),
    ];
    originals.push(write_tiff(&[grey8(5, 5)], true, false));
    let mut rng = Rng(0x2545_f491_4f6c_dd1d);
    for original in &originals {
        for _ in 0..300 {
            let mut file = original.clone();
            for _ in 0..1 + rng.next() % 8 {
                let at = (rng.next() % file.len() as u64) as usize;
                file[at] = rng.next() as u8;
            }
            if rng.next() % 5 == 0 {
                file.truncate((rng.next() % file.len() as u64) as usize + 1);
            }
            let Ok((layout, _)) = walk(&file) else {
                continue;
            };
            for image in &layout.images {
                for stored in std::iter::once(&image.stored).chain(&image.levels) {
                    if stored.unsupported.is_some() || stored.offsets.is_empty() {
                        continue;
                    }
                    for i in [0, stored.offsets.len() - 1] {
                        let _ = decode(
                            &file,
                            stored,
                            i,
                            stored.tile_height.min(stored.height),
                            serde_json::json!({}),
                        );
                    }
                }
            }
        }
    }
}
