//! JCAMP-DX files as instruments and their software wrote them, checked against what
//! each file declares of itself (`##NPOINTS`, `##FIRSTX`, `##LASTX`, `##FIRSTY`, `##MAXY`,
//! `##MINY`, or `##FIRST`, `##LAST` and `##VAR_DIM` in `##NTUPLES`), and against the same
//! spectrum written uncompressed. Sources and licences: `server/tests/data/previews/README.md`.

// The helpers fail a test by panicking, which `allow-unwrap-in-tests` covers only in
// `#[test]` functions themselves; values compared exactly are ones a reader must give
// exactly (integers, and numbers the file writes out in full).
#![allow(clippy::unwrap_used, clippy::panic, clippy::float_cmp)]

use iki_previews::jcamp::{self, BlockKind, Spectra};

fn sample(name: &str) -> Vec<u8> {
    let path = format!(
        "{}/../../../server/tests/data/previews/{name}",
        env!("CARGO_MANIFEST_DIR")
    );
    std::fs::read(&path).unwrap_or_else(|error| panic!("{path}: {error}"))
}

fn read(name: &str) -> Spectra {
    jcamp::read(&sample(name)).unwrap_or_else(|error| panic!("{name}: {error}"))
}

fn close(got: f64, want: f64, relative: f64) -> bool {
    (got - want).abs() <= relative * want.abs().max(1.0)
}

fn extremes(values: &[f64]) -> (f64, f64) {
    values
        .iter()
        .fold((f64::INFINITY, f64::NEG_INFINITY), |(low, high), v| {
            (low.min(*v), high.max(*v))
        })
}

#[test]
fn reads_a_dif_dup_infrared_spectrum() {
    // Perkin-Elmer 1605 FT-IR, written in DIF form with DUP: every line after the first
    // starts with a Y check value.
    let spectra = read("dupdec1.jdx");
    assert!(spectra.problems.is_empty(), "{:?}", spectra.problems);
    assert!(!spectra.truncated);
    let [block] = &spectra.blocks[..] else {
        panic!("{} blocks", spectra.blocks.len())
    };
    assert_eq!(block.data_type.as_deref(), Some("INFRARED SPECTRUM"));
    assert_eq!(block.x_units.as_deref(), Some("1/CM"));
    assert_eq!(block.y_units.as_deref(), Some("TRANSMITTANCE"));
    assert_eq!(block.kind, BlockKind::Xy);
    // ##NPOINTS= 3951, ##FIRSTX= 4400., ##LASTX= 450., ##FIRSTY= 82.25.
    assert_eq!(block.declared_points, Some(3951));
    assert_eq!(block.x.len(), 3951);
    assert!(close(block.x[0], 4400.0, 1e-12));
    assert!(close(*block.x.last().unwrap(), 450.0, 1e-9));
    assert!(close(block.y[0], 82.25, 1e-12));
    // The last line, "450G858", is the check value of the last point: 78.58.
    assert!(close(*block.y.last().unwrap(), 78.58, 1e-12));
    // ##MAXY= 87.1 and ##MINY= 0.01: the instrument's range (as ##INSTRUMENT PARAMETERS
    // says, "0.01,87.100 %T"). The data's lowest is 0.02, at 694 cm⁻¹, where line 140's
    // check value agrees.
    let (low, high) = extremes(&block.y);
    assert!(close(high, 87.1, 1e-12), "{high}");
    assert!(close(low, 0.02, 1e-12) && low >= 0.01, "{low}");
    // Evenly spaced, 1 cm⁻¹ apart, descending.
    assert!(
        block
            .x
            .windows(2)
            .all(|pair| close(pair[0] - pair[1], 1.0, 1e-9))
    );
}

#[test]
fn reads_a_mass_spectrum_peak_table() {
    let spectra = read("pktab1.jdx");
    assert!(spectra.problems.is_empty(), "{:?}", spectra.problems);
    let block = &spectra.blocks[0];
    assert_eq!(block.title.as_deref(), Some("Cholesterol (pktab1.jdx)"));
    assert_eq!(block.data_type.as_deref(), Some("MASS SPECTRUM"));
    assert_eq!(block.kind, BlockKind::Peaks);
    assert_eq!(block.x.len(), 46);
    assert_eq!((block.x[0], block.y[0]), (0.0, 0.0));
    assert_eq!((block.x[2], block.y[2]), (43.0, 1000.0));
    assert_eq!((block.x[45], block.y[45]), (386.0, 324.0));
    // m/z 386 is cholesterol's molecular ion.
    assert_eq!(*block.x.last().unwrap(), 386.0);
}

#[test]
fn dif_dup_and_fixed_forms_of_one_spectrum_agree_point_for_point() {
    // The same ¹H NMR spectrum, written by Bruker XWIN-NMR in DIF/DUP and in fixed AFFN;
    // jcampconverter's tests read both and expect the same result.
    let compressed = read("jcamp-difdup.dx");
    let fixed = read("jcamp-fix.dx");
    assert!(compressed.problems.is_empty(), "{:?}", compressed.problems);
    assert!(fixed.problems.is_empty(), "{:?}", fixed.problems);
    let (a, b) = (&compressed.blocks[0], &fixed.blocks[0]);
    assert_eq!(a.x.len(), 16_384);
    assert_eq!(a.x.len(), b.x.len());
    assert_eq!(
        a.y, b.y,
        "the ordinates are integers, so they agree exactly"
    );
    assert!(a.x.iter().zip(&b.x).all(|(p, q)| close(*p, *q, 1e-12)));
    // ##FIRSTX= 5592.84116331095, ##LASTX= 0, ##FIRSTY= -10247.
    assert!(close(a.x[0], 5_592.841_163_310_95, 1e-12), "{}", a.x[0]);
    assert!(a.x.last().unwrap().abs() < 1e-9);
    assert_eq!(a.y[0], -10247.0);
    // ##MAXY= 408687150, ##MINY= -156586.
    assert_eq!(extremes(&a.y), (-156_586.0, 408_687_150.0));
}

#[test]
fn reads_the_real_part_of_an_ntuples_fid() {
    let spectra = read("aspirin-1h.fid.dx");
    assert_eq!(spectra.title.as_deref(), Some("1H BBI"));
    let [block] = &spectra.blocks[..] else {
        panic!("{} blocks", spectra.blocks.len())
    };
    assert_eq!(block.title.as_deref(), Some("1H BBI (N=1)"));
    assert_eq!(block.data_type.as_deref(), Some("NMR FID"));
    assert_eq!(block.x_units.as_deref(), Some("SECONDS"));
    // ##VAR_DIM= 8192; ##FIRST= 0, 0; ##LAST= 1.7102808, 4422.
    assert_eq!(block.declared_points, Some(8192));
    assert_eq!(block.x.len(), 8192);
    assert_eq!(block.x[0], 0.0);
    assert!(close(*block.x.last().unwrap(), 1.710_280_8, 1e-9));
    assert_eq!(block.y[0], 0.0);
    assert_eq!(*block.y.last().unwrap(), 4422.0);
    // ##MIN= -593436 and ##MAX= 1007953 are over both parts; the real part lies within.
    let (low, high) = extremes(&block.y);
    assert!(low >= -593_436.0 && high <= 1_007_953.0, "{low} to {high}");
    assert_eq!(spectra.problems.len(), 1, "{:?}", spectra.problems);
    assert!(
        spectra.problems[0].contains("FID/IMAG"),
        "{:?}",
        spectra.problems
    );
}

#[test]
fn follows_the_data_where_firstx_and_lastx_are_swapped() {
    // TopSpin wrote ##FIRSTX=0 and ##LASTX=36231.88 but listed the points from 36231.88
    // down: the data's abscissas and ##FIRSTY agree with each other, not with ##FIRSTX.
    let spectra = read("broad-carbon-bruker.jdx");
    let block = &spectra.blocks[0];
    assert_eq!(block.x.len(), 1024);
    assert!(
        close(block.x[0], 36_231.884_057_970_99, 1e-9),
        "{}",
        block.x[0]
    );
    assert!(block.x.last().unwrap().abs() < 1e-6);
    // The ordinates are integers times ##YFACTOR (8.44), so they agree with ##FIRSTY,
    // ##MAXY and ##MINY to within half of it.
    let factor = 8.439_203_740_239_13;
    assert!(
        (block.y[0] - 86_717_423.783_261_94).abs() <= factor / 2.0,
        "{}",
        block.y[0]
    );
    let (low, high) = extremes(&block.y);
    assert!(
        (high - 18_123_052_025.864_765).abs() <= factor / 2.0,
        "{high}"
    );
    assert!((low - 5_347_876.593_017_418).abs() <= factor / 2.0, "{low}");
    assert_eq!(spectra.problems.len(), 1, "{:?}", spectra.problems);
    assert!(spectra.problems[0].contains("run the other way"));
}

#[test]
fn every_truncation_of_a_compressed_file_is_a_reading_or_an_error() {
    let bytes = sample("dupdec1.jdx");
    for length in (0..bytes.len()).step_by(13) {
        let _ = jcamp::read(&bytes[..length]);
    }
}

#[test]
fn damaged_bytes_in_compressed_data_are_handled() {
    let bytes = sample("dupdec1.jdx");
    let mut damaged = bytes.clone();
    for at in (bytes.len() / 2..bytes.len()).step_by(17) {
        for value in [b'#', b'$', b'?', b's', b'%', 0xff] {
            damaged[at] = value;
            let _ = jcamp::read(&damaged);
        }
        damaged[at] = bytes[at];
    }
}
