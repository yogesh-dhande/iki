//! TIFF and BigTIFF, including OME-TIFF and pyramidal TIFF, read a tile at a time (ADR
//! 0227). A 2 GB image is never in memory: the page reads the header and each image file
//! directory (IFD) in ranges, as [`layout`] asks for them, then only the tiles (or strips)
//! it shows, each decoded by [`tile`] to RGBA.
//!
//! [`layout`] is stateless: it's given every range the page has read so far, packed as
//! [`Ranges`] describes, and walks the file from its header. Where it needs bytes it wasn't
//! given (the next IFD, a tag's values held elsewhere, a tile offsets array), it answers
//! [`Step::Need`]; the page reads those and asks again, until it answers [`Step::Layout`].
//!
//! Compression: none (1), LZW (5), JPEG (7, with `JPEGTables`), Deflate (8 and 32946) and
//! PackBits (32773), with horizontal (2) and floating-point (3) predictors. Pixels: 8-,
//! 16- and 32-bit unsigned and signed integers and 32- and 64-bit floats, grey (min-is-black
//! or min-is-white), palette, RGB and RGBA, chunky or planar. Others are listed in the
//! layout with why they can't be shown.

use serde::{Deserialize, Serialize};

/// The most IFDs walked, across the main chain and SubIFDs.
pub const MAX_IFDS: usize = 10_000;
/// The most tiles or strips an image may have to be listed.
pub const MAX_CHUNKS: usize = 1_000_000;
/// The most pixels one tile or strip may decode to.
pub const MAX_TILE_PIXELS: usize = 16 * 1024 * 1024;
/// The most bytes of an `ImageDescription` (OME-XML) kept.
pub const MAX_DESCRIPTION: usize = 4 * 1024 * 1024;

/// How the page packs the ranges it has read into one input: each range is its offset in
/// the file (u64, little-endian), its length (u32, little-endian), then its bytes. The first
/// range starts with the file's size instead: `[size: u64][0: u32]`, no bytes.
pub struct Ranges;

/// What [`layout`] answers.
#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Step {
    /// The ranges it needs next, at most a few at a time.
    Need(Vec<Span>),
    /// The whole layout.
    Layout(Layout),
}

/// A range of the file.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Span {
    /// Its first byte's offset.
    pub offset: u64,
    /// How many bytes.
    pub length: u64,
}

/// A TIFF's images.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Layout {
    /// Whether it's BigTIFF.
    pub bigtiff: bool,
    /// Its full-resolution images, in the main IFD chain's order. A pyramid's reduced
    /// levels are each image's `levels`, not images of their own.
    pub images: Vec<Image>,
    /// The first IFD's `ImageDescription`, up to [`MAX_DESCRIPTION`].
    pub description: Option<String>,
    /// What its OME-XML says, if its description is OME-XML.
    pub ome: Option<Ome>,
    /// Whether IFDs or chunks were left out past the limits.
    pub truncated: bool,
}

/// One image: an IFD, and its reduced-resolution levels, largest first.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Image {
    /// Its place in the main IFD chain, from 0.
    pub index: usize,
    /// How it's stored.
    #[serde(flatten)]
    pub stored: Stored,
    /// Its reduced-resolution levels: its SubIFDs, or the reduced images (`NewSubfileType`
    /// bit 0) that follow it in the main chain, as whole-slide TIFFs keep them. Largest first.
    pub levels: Vec<Stored>,
}

/// How one IFD's pixels are stored.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Stored {
    /// Its width and height, in pixels.
    pub width: u32,
    /// Height.
    pub height: u32,
    /// Whether it's in tiles; otherwise in strips, each `tile_width` (its width) wide.
    pub tiled: bool,
    /// A tile's width, or the image's width for strips.
    pub tile_width: u32,
    /// A tile's height, or `RowsPerStrip` (at most the image's height) for strips.
    pub tile_height: u32,
    /// Each tile's or strip's offset, row by row, then plane by plane where `planar` is 2.
    pub offsets: Vec<u64>,
    /// Each one's byte count.
    pub byte_counts: Vec<u64>,
    /// How its pixels are to be decoded.
    #[serde(flatten)]
    pub pixels: Pixels,
    /// `JPEGTables`, where the file keeps them, for JPEG compression.
    pub jpeg_tables: Option<Span>,
    /// Why it can't be shown, if it can't (an unsupported compression, say).
    pub unsupported: Option<String>,
}

/// What a tile's bytes are, for [`tile`] to decode.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Pixels {
    /// `Compression`.
    pub compression: u16,
    /// `Predictor`: 1 none, 2 horizontal, 3 floating-point.
    pub predictor: u16,
    /// `PhotometricInterpretation`.
    pub photometric: u16,
    /// `BitsPerSample` (the same for each sample).
    pub bits_per_sample: u16,
    /// `SamplesPerPixel`.
    pub samples_per_pixel: u16,
    /// `SampleFormat`: 1 unsigned, 2 signed, 3 float.
    pub sample_format: u16,
    /// `PlanarConfiguration`: 1 chunky, 2 planar.
    pub planar: u16,
    /// `ColorMap`, for palette images: red, then green, then blue, 2^bits each.
    pub colormap: Option<Vec<u16>>,
    /// Whether the file is little-endian (`II`), which samples of more than 8 bits are.
    pub little_endian: bool,
}

/// What OME-XML says about the planes (its first `Image`'s `Pixels`).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Ome {
    /// `SizeX`.
    pub size_x: u32,
    /// `SizeY`.
    pub size_y: u32,
    /// `SizeC`: channels.
    pub size_c: u32,
    /// `SizeZ`: focal planes.
    pub size_z: u32,
    /// `SizeT`: time points.
    pub size_t: u32,
    /// `DimensionOrder`, such as `XYZCT`: how IFDs follow each other.
    pub dimension_order: String,
    /// Each channel's `Name`, and its `Color` as signed 32-bit RGBA, if given.
    pub channels: Vec<Channel>,
    /// The planes' IFDs from `TiffData` (`IFD`, `FirstC`, `FirstZ`, `FirstT`,
    /// `PlaneCount`), when it gives them; otherwise planes follow `dimension_order`.
    pub planes: Vec<Plane>,
    /// `PhysicalSizeX` and its unit (µm by default).
    pub physical_size_x: Option<f64>,
    /// `PhysicalSizeY`.
    pub physical_size_y: Option<f64>,
    /// `PhysicalSizeXUnit`.
    pub physical_size_unit: Option<String>,
}

/// One channel.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Channel {
    /// Its name.
    pub name: Option<String>,
    /// Its colour, as OME's signed 32-bit RGBA.
    pub color: Option<i32>,
}

/// Which IFD holds the plane at (c, z, t).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Plane {
    /// The channel.
    pub c: u32,
    /// The focal plane.
    pub z: u32,
    /// The time point.
    pub t: u32,
    /// The IFD, in the main chain, from 0.
    pub ifd: usize,
}

/// What [`tile`] is asked to decode: one tile's or strip's bytes follow this JSON.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct TileRequest {
    /// How its pixels are stored.
    #[serde(flatten)]
    pub pixels: Pixels,
    /// Its width and height as stored (a whole tile, even at the image's edge).
    pub width: u32,
    /// Height (a strip's rows: the last strip may have fewer).
    pub height: u32,
    /// `JPEGTables`' bytes, base64, for JPEG compression.
    pub jpeg_tables: Option<String>,
    /// Which sample to show, for grey images of several samples per pixel that aren't RGB.
    pub sample: Option<u16>,
    /// The values shown as black and white, clamped. Without one, 8-bit samples are shown
    /// as they are (signed ones shifted by 128), and wider ones and floats from the tile's
    /// own minimum to its maximum.
    pub window: Option<[f64; 2]>,
    /// Where `planar` is 2: each plane's bytes' length, in sample order, when the bytes
    /// after the request are every plane's chunk one after another; they're composed into
    /// one tile (RGB, or `sample`'s plane). Without it, the bytes are one plane's, shown
    /// as grey.
    #[serde(default)]
    pub plane_lengths: Option<Vec<usize>>,
}

/// A decoded tile: its pixels follow as RGBA, `width` × `height` × 4 bytes.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Tile {
    /// Width.
    pub width: u32,
    /// Height.
    pub height: u32,
    /// The least and greatest sample values in it, before the window: for choosing one.
    pub min: f64,
    /// Greatest.
    pub max: f64,
}

/// Walks the file's IFDs from the ranges given.
///
/// Reduced-resolution images are levels of the full image before them: its SubIFDs, and
/// the images after it in the main chain marked reduced (`NewSubfileType` bit 0), or tiled
/// like it (as Aperio's whole-slide files keep them), whose aspect ratio is its own and
/// which are smaller than its last level. Others, such as a slide's label and macro
/// images, are images of their own. An uncompressed image in one strip too large to
/// decode at once is listed as strips of fewer rows, each a range of that strip.
///
/// # Errors
///
/// A message for a person if it isn't a TIFF, or its structure is broken.
pub fn layout(input: &[u8]) -> Result<Step, String> {
    ifd::layout(input)
}

/// Decodes one tile: `input` is a [`TileRequest`]'s JSON, `request_len` bytes, then the
/// tile's bytes. Returns it and its RGBA pixels.
///
/// Samples are mapped through the request's window; without one, 8-bit samples are shown
/// as they are (signed ones shifted by 128), and wider ones and floats from the tile's own
/// minimum to its maximum. NaN is drawn black, and left out of the minimum and maximum.
/// Min-is-white grey is inverted; palette images are coloured by their `ColorMap`. Alpha
/// (`ExtraSamples`) is ignored: RGBA is drawn opaque. Where `planar` is 2, the bytes are
/// every plane's chunk with `plane_lengths`, composed into one tile, or else one plane's,
/// shown as grey. Decompressed bytes never go past what the tile's size needs, whatever
/// the data holds. The pixels are always `width` × `height` × 4 bytes.
///
/// # Errors
///
/// A message for a person if its bytes don't decode as the request says.
pub fn tile(input: &[u8], request_len: usize) -> Result<(Tile, Vec<u8>), String> {
    decode::tile(input, request_len)
}

mod decode;
mod ifd;
mod ome;
