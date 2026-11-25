//! ABIF traces written by Applied Biosystems instruments, from Biopython's test suite,
//! checked against what Biopython's own tests expect of them
//! (`server/tests/data/previews/biopython-abi-expected.json`, copied from
//! `Tests/test_SeqIO_AbiIO.py`).

// The helpers fail a test by panicking, which `allow-unwrap-in-tests` covers only in
// `#[test]` functions themselves; values compared exactly are ones a reader must give
// exactly (integers, and numbers the file writes out in full).
#![allow(clippy::unwrap_used, clippy::panic, clippy::float_cmp)]

use iki_previews::abif;

fn sample(name: &str) -> Vec<u8> {
    let path = format!(
        "{}/../../../server/tests/data/previews/{name}",
        env!("CARGO_MANIFEST_DIR")
    );
    std::fs::read(&path).unwrap_or_else(|error| panic!("{path}: {error}"))
}

fn expected(name: &str) -> serde_json::Value {
    let all: serde_json::Value =
        serde_json::from_slice(&sample("biopython-abi-expected.json")).unwrap();
    all[name].clone()
}

fn check_against_biopython(name: &str) -> abif::Chromatogram {
    let reading = abif::read(&sample(name)).unwrap();
    let expected = expected(name);
    assert_eq!(
        reading.bases,
        expected["sequence"].as_str().unwrap(),
        "{name}"
    );
    let quality: Vec<u8> = expected["quality"]
        .as_array()
        .unwrap()
        .iter()
        .map(|value| u8::try_from(value.as_u64().unwrap()).unwrap())
        .collect();
    assert_eq!(reading.quality.as_ref(), Some(&quality), "{name}");
    assert_eq!(
        reading.sample.as_deref(),
        expected["sample"].as_str(),
        "{name}"
    );
    let model = expected["machine_model"].as_str().unwrap().trim();
    assert!(
        reading.instrument.as_deref().unwrap().starts_with(model),
        "{name}: {:?}",
        reading.instrument
    );
    assert_eq!(reading.peaks.len(), reading.bases.len(), "{name}");
    assert!(
        reading.problems.is_empty(),
        "{name}: {:?}",
        reading.problems
    );
    for trace in [
        &reading.traces.a,
        &reading.traces.c,
        &reading.traces.g,
        &reading.traces.t,
    ] {
        assert_eq!(trace.len(), reading.points, "{name}");
    }
    // Peaks are in order along the trace.
    assert!(
        reading.peaks.windows(2).all(|pair| pair[0] <= pair[1]),
        "{name}"
    );
    reading
}

/// Where a call is confident, its base's trace is the tallest of the four at its peak: so
/// the traces are matched to the right bases (`FWO_`).
fn called_bases_sit_on_their_traces(reading: &abif::Chromatogram, least_quality: u8) {
    let quality = reading.quality.as_ref().unwrap();
    let (mut confident, mut tallest) = (0, 0);
    for ((base, peak), quality) in reading.bases.chars().zip(&reading.peaks).zip(quality) {
        if *quality < least_quality {
            continue;
        }
        let at = usize::try_from(*peak).unwrap();
        let heights = [
            ('A', reading.traces.a[at]),
            ('C', reading.traces.c[at]),
            ('G', reading.traces.g[at]),
            ('T', reading.traces.t[at]),
        ];
        let top = heights.iter().max_by_key(|(_, height)| *height).unwrap().0;
        confident += 1;
        if top == base {
            tallest += 1;
        }
    }
    assert!(confident > 100, "{confident} confident calls");
    assert!(
        tallest * 100 >= confident * 95,
        "{tallest} of {confident} confident calls sit on their own trace"
    );
}

#[test]
fn reads_a_3730_trace_as_biopython_does() {
    let reading = check_against_biopython("3730.ab1");
    assert_eq!(reading.bases.len(), 1165);
    called_bases_sit_on_their_traces(&reading, 30);
}

#[test]
fn reads_a_310_trace_as_biopython_does() {
    // An old 310: every quality value is 0, and many calls are N.
    let reading = check_against_biopython("310.ab1");
    assert_eq!(reading.bases.len(), 868);
    assert_eq!(reading.sample.as_deref(), Some("D11F"));
    assert!(reading.quality.as_ref().unwrap().iter().all(|q| *q == 0));
}

#[test]
fn every_truncation_of_a_real_file_is_an_error_or_a_reading() {
    let bytes = sample("3730.ab1");
    for length in (0..bytes.len()).step_by(997) {
        let _ = abif::read(&bytes[..length]);
    }
}

#[test]
fn every_flipped_byte_of_a_real_directory_is_handled() {
    let bytes = sample("3730.ab1");
    let directory = usize::try_from(i32::from_be_bytes(bytes[26..30].try_into().unwrap())).unwrap();
    let mut damaged = bytes.clone();
    for at in (directory..bytes.len().min(directory + 28 * 140)).step_by(5) {
        for value in [0x80, 0xff] {
            damaged[at] = value;
            let _ = abif::read(&damaged);
        }
        damaged[at] = bytes[at];
    }
}
