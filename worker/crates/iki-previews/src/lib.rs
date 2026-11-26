//! Readers for Iki's file previews (ADR 0226), compiled to WebAssembly and run in a Web
//! Worker of the web app's own, apart from the page.
//!
//! Files are hostile: each reader takes bytes it was handed, never panics, bounds what it
//! reads (bytes, rows, points, tiles) and says what it left out. The page reads only what a
//! preview needs from the store, in ranges where a format allows it (ADR 0227), and hands
//! those bytes in here; the module has no imports, so it reaches nothing itself.
//!
//! | Export | Input buffer | Output JSON |
//! |---|---|---|
//! | `iki_abif()` | an AB1 file | [`abif::Chromatogram`] |
//! | `iki_jcamp()` | a JCAMP-DX file | [`jcamp::Spectra`] |
//! | `iki_mzml_offset()` | an mzML file's last bytes | `{"index_offset": n \| null}` |
//! | `iki_mzml_index()` | from the index's offset to the end | [`mzml::Index`] |
//! | `iki_mzml_scan(complete)` | the start of a file (all of it if `complete`) | [`mzml::Index`] |
//! | `iki_mzml_element(max_points)` | from a spectrum's or chromatogram's offset | [`mzml::Element`] |
//! | `iki_tiff_layout()` | ranges of a TIFF, as [`tiff::Ranges`] packs them | [`tiff::Step`] |
//! | `iki_tiff_tile(request_len)` | a [`tiff::TileRequest`]'s JSON, then the tile's bytes | [`tiff::Tile`], and its RGBA pixels |
//! | `iki_csv(delimiter, max_rows, max_columns, complete)` | a CSV or TSV file, or its start | [`table::Table`] |
//! | `iki_xlsx(sheet, max_rows, max_columns)` | an XLSX workbook | [`table::Table`] |
//!
//! Every output is `{"error": "…"}` instead when the bytes can't be read: the message is
//! for a person, and says what in the file is wrong where it can.

use serde::Serialize;

pub mod abif;
pub mod jcamp;
pub mod mzml;
pub mod table;
pub mod tiff;

/// The most any one input may be. The page asks for less for each format.
pub const MAX_INPUT: usize = 128 * 1024 * 1024;

/// `{"error": message}`.
#[must_use]
pub fn failed(message: &str) -> Vec<u8> {
    serde_json::to_vec(&serde_json::json!({ "error": message }))
        .unwrap_or_else(|_| br#"{"error":"The reader failed."}"#.to_vec())
}

/// A reading as JSON, or why it isn't one.
#[must_use]
pub fn to_json<T: Serialize>(reading: Result<T, String>) -> Vec<u8> {
    match reading {
        Ok(value) => serde_json::to_vec(&value)
            .unwrap_or_else(|_| failed("The reading couldn't be written out.")),
        Err(message) => failed(&message),
    }
}

/// The module's exports, for the previews' worker. It asks for a buffer of the input's
/// size, writes the input into it, calls one reader, then reads the JSON from `iki_output`
/// and `iki_output_len` (and a tile's pixels from `iki_pixels` and `iki_pixels_len`). The
/// buffers live in this thread's state, so no export takes a pointer from JavaScript.
#[allow(unsafe_code)] // `no_mangle` alone: the exports' names. No unsafe blocks.
pub mod exports {
    use std::cell::RefCell;

    thread_local! {
        static INPUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
        static OUTPUT: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
        static PIXELS: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
    }

    fn take() -> Vec<u8> {
        INPUT.with_borrow_mut(std::mem::take)
    }

    fn answer(json: Vec<u8>) {
        OUTPUT.with_borrow_mut(|output| *output = json);
    }

    /// A buffer of `len` bytes for the input, and where it is in the module's memory. An
    /// input larger than [`super::MAX_INPUT`] has no buffer.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_input(len: usize) -> *mut u8 {
        INPUT.with_borrow_mut(|input| {
            input.clear();
            if len > super::MAX_INPUT {
                return std::ptr::null_mut();
            }
            input.resize(len, 0);
            input.as_mut_ptr()
        })
    }

    /// Reads an ABIF (AB1) trace.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_abif() {
        answer(super::to_json(super::abif::read(&take())));
    }

    /// Reads a JCAMP-DX file's spectra.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_jcamp() {
        answer(super::to_json(super::jcamp::read(&take())));
    }

    /// Where an indexed mzML file's index starts, from its last bytes.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_mzml_offset() {
        let offset = super::mzml::index_offset(&take());
        answer(super::to_json(Ok(
            serde_json::json!({ "index_offset": offset }),
        )));
    }

    /// Reads an indexed mzML file's index.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_mzml_index() {
        answer(super::to_json(super::mzml::read_index(&take())));
    }

    /// Finds the spectra and chromatograms in the start of an mzML file without an index.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_mzml_scan(complete: u32) {
        answer(super::to_json(super::mzml::scan(&take(), complete != 0)));
    }

    /// Reads one spectrum or chromatogram, from its offset.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_mzml_element(max_points: u32) {
        answer(super::to_json(super::mzml::read_element(
            &take(),
            max_points as usize,
        )));
    }

    /// Reads as much of a TIFF's layout as the ranges hold, or says which it needs next.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_tiff_layout() {
        answer(super::to_json(super::tiff::layout(&take())));
    }

    /// Decodes one tile or strip to RGBA pixels.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_tiff_tile(request_len: u32) {
        let input = take();
        match super::tiff::tile(&input, request_len as usize) {
            Ok((tile, pixels)) => {
                PIXELS.with_borrow_mut(|out| *out = pixels);
                answer(super::to_json(Ok(tile)));
            }
            Err(message) => {
                PIXELS.with_borrow_mut(Vec::clear);
                answer(super::failed(&message));
            }
        }
    }

    /// Reads a CSV or TSV file's first rows.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_csv(delimiter: u32, max_rows: u32, max_columns: u32, complete: u32) {
        let delimiter = u8::try_from(delimiter).unwrap_or(b',');
        answer(super::to_json(super::table::csv(
            &take(),
            delimiter,
            max_rows as usize,
            max_columns as usize,
            complete != 0,
        )));
    }

    /// Reads one sheet of an XLSX workbook: its first rows, and the workbook's sheets.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_xlsx(sheet: u32, max_rows: u32, max_columns: u32) {
        answer(super::to_json(super::table::xlsx(
            &take(),
            sheet as usize,
            max_rows as usize,
            max_columns as usize,
        )));
    }

    /// Where the last answer's JSON is.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_output() -> *const u8 {
        OUTPUT.with_borrow(Vec::as_ptr)
    }

    /// How long it is.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_output_len() -> usize {
        OUTPUT.with_borrow(Vec::len)
    }

    /// Where the last tile's RGBA pixels are.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_pixels() -> *const u8 {
        PIXELS.with_borrow(Vec::as_ptr)
    }

    /// How many bytes they are: its width × height × 4.
    #[unsafe(no_mangle)]
    pub extern "C" fn iki_pixels_len() -> usize {
        PIXELS.with_borrow(Vec::len)
    }
}
