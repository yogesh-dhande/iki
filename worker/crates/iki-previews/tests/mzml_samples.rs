//! mzML files written by ProteoWizard, read as the page reads them: the tail, then the
//! index, then one element at a time. `tiny.pwiz.1.1.mzML` is checked against the arrays
//! ProteoWizard's example generator writes into it (`pwiz/data/msdata/Examples.cs`, a port
//! of `examples.cpp`: its m/z, intensity and time arrays are made up, and don't match the
//! made-up parameters beside them) and pyteomics' test of its precursor; pyteomics'
//! `test.mzML` against what the file says of itself (lowest and highest observed m/z) and
//! pyteomics' own tests: both spectra decode to the same arrays, one 64-bit uncompressed,
//! the other 32-bit zlib. Sources and licences: `server/tests/data/previews/README.md`.

// The helpers fail a test by panicking, which `allow-unwrap-in-tests` covers only in
// `#[test]` functions themselves; values compared exactly are ones a reader must give
// exactly (integers, and numbers the file writes out in full).
#![allow(clippy::unwrap_used, clippy::panic, clippy::float_cmp)]

use iki_previews::mzml::{self, Element, Index};

fn sample(name: &str) -> Vec<u8> {
    let path = format!(
        "{}/../../../server/tests/data/previews/{name}",
        env!("CARGO_MANIFEST_DIR")
    );
    std::fs::read(&path).unwrap_or_else(|error| panic!("{path}: {error}"))
}

/// The bytes the page would read for each entry: from its offset to the next's.
fn ranges(index: &Index, length: usize) -> Vec<(usize, usize)> {
    let mut starts: Vec<usize> = index
        .spectra
        .iter()
        .chain(&index.chromatograms)
        .map(|entry| usize::try_from(entry.offset).unwrap())
        .collect();
    starts.sort_unstable();
    let mut ends = starts[1..].to_vec();
    ends.push(length);
    starts.into_iter().zip(ends).collect()
}

fn spectrum(bytes: &[u8], at: u64, max: usize) -> mzml::Spectrum {
    let at = usize::try_from(at).unwrap();
    match mzml::read_element(&bytes[at..], max).unwrap() {
        Element::Spectrum(spectrum) => spectrum,
        Element::Chromatogram(chromatogram) => panic!("{} is a chromatogram", chromatogram.id),
    }
}

fn chromatogram(bytes: &[u8], at: u64, max: usize) -> mzml::Chromatogram {
    let at = usize::try_from(at).unwrap();
    match mzml::read_element(&bytes[at..], max).unwrap() {
        Element::Chromatogram(chromatogram) => chromatogram,
        Element::Spectrum(spectrum) => panic!("{} is a spectrum", spectrum.id),
    }
}

fn base_peak(spectrum: &mzml::Spectrum) -> (f64, f64) {
    let (at, intensity) =
        spectrum
            .intensity
            .iter()
            .enumerate()
            .fold((0, f64::NEG_INFINITY), |best, (at, value)| {
                if *value > best.1 { (at, *value) } else { best }
            });
    (spectrum.mz[at], intensity)
}

#[test]
#[allow(clippy::too_many_lines)]
fn reads_tiny_pwiz_through_its_index() {
    let bytes = sample("tiny.pwiz.1.1.mzML");
    let tail = &bytes[bytes.len().saturating_sub(mzml::TAIL)..];
    let offset = mzml::index_offset(tail).unwrap();
    assert_eq!(offset, 24498);
    let index = mzml::read_index(&bytes[usize::try_from(offset).unwrap()..]).unwrap();
    let ids: Vec<&str> = index
        .spectra
        .iter()
        .map(|entry| entry.id.as_str())
        .collect();
    assert_eq!(
        ids,
        [
            "scan=19",
            "scan=20",
            "scan=21",
            "sample=1 period=1 cycle=22 experiment=1"
        ]
    );
    let offsets: Vec<u64> = index.spectra.iter().map(|entry| entry.offset).collect();
    assert_eq!(offsets, [6883, 10424, 15411, 16940]);
    let chromatograms: Vec<(&str, u64)> = index
        .chromatograms
        .iter()
        .map(|e| (e.id.as_str(), e.offset))
        .collect();
    assert_eq!(chromatograms, [("tic", 20654), ("sic", 22253)]);
    assert!(!index.truncated);

    // Scanning the whole file finds the same elements at the same offsets.
    let scanned = mzml::scan(&bytes, true).unwrap();
    assert_eq!(scanned.spectra, index.spectra);
    assert_eq!(scanned.chromatograms, index.chromatograms);
    assert_eq!(scanned.declared_spectra, Some(4));
    assert!(!scanned.truncated);

    // Each element read from its offset to the next one's.
    for (start, end) in ranges(&index, usize::try_from(offset).unwrap()) {
        mzml::read_element(&bytes[start..end], 1000).unwrap();
    }

    let ms1 = spectrum(&bytes, 6883, 1000);
    assert_eq!(ms1.id, "scan=19");
    assert_eq!(ms1.index, Some(0));
    assert_eq!(ms1.ms_level, Some(1));
    assert_eq!(ms1.centroided, Some(true));
    assert!((ms1.scan_start_minutes.unwrap() - 5.8905).abs() < 1e-12);
    // Its polarity is in the CommonMS1SpectrumParams group, in the file's header.
    assert_eq!(ms1.polarity, None);
    assert_eq!(ms1.points, 15);
    // Examples.cs: m/z i and intensity 15 − i, for i from 0 to 14.
    assert_eq!(ms1.mz, (0..15).map(f64::from).collect::<Vec<_>>());
    assert_eq!(
        ms1.intensity,
        (0..15).map(|i| f64::from(15 - i)).collect::<Vec<_>>()
    );
    assert!(!ms1.truncated);
    assert_eq!(ms1.base_peak_mz, Some(445.347));
    assert_eq!(ms1.base_peak_intensity, Some(120_053.0));
    assert_eq!(ms1.total_ion_current, Some(16_675_500.0));
    assert!(ms1.precursor_mz.is_empty());

    let ms2 = spectrum(&bytes, 10424, 1000);
    assert_eq!(ms2.ms_level, Some(2));
    assert_eq!(ms2.centroided, Some(false));
    // pyteomics' test_pylab_aux.py: 445.34.
    assert_eq!(ms2.precursor_mz, vec![445.34]);
    // Examples.cs: m/z 2i and intensity (10 − i) × 2, for i from 0 to 9.
    assert_eq!(
        ms2.mz,
        (0..10).map(|i| f64::from(i * 2)).collect::<Vec<_>>()
    );
    assert_eq!(
        ms2.intensity,
        (0..10).map(|i| f64::from((10 - i) * 2)).collect::<Vec<_>>()
    );

    let empty = spectrum(&bytes, 15411, 1000);
    assert_eq!(empty.points, 0);
    assert!(empty.mz.is_empty());
    assert_eq!(empty.scan_start_minutes, None);

    // Scanned at 42.05 seconds.
    let maldi = spectrum(&bytes, 16940, 1000);
    assert!((maldi.scan_start_minutes.unwrap() - 42.05 / 60.0).abs() < 1e-12);
    // Examples.cs: s23 reuses s19's arrays.
    assert_eq!(maldi.mz, ms1.mz);
    assert_eq!(maldi.intensity, ms1.intensity);

    let tic = chromatogram(&bytes, 20654, 1000);
    assert_eq!(tic.id, "tic");
    assert_eq!(
        tic.chromatogram_type.as_deref(),
        Some("total ion current chromatogram")
    );
    assert_eq!(tic.points, 15);
    // Examples.cs: time i seconds and intensity 15 − i; the file says the times are seconds.
    let minutes: Vec<f64> = (0..15).map(|i| f64::from(i) / 60.0).collect();
    assert!(
        tic.time_minutes
            .iter()
            .zip(&minutes)
            .all(|(a, b)| (a - b).abs() < 1e-12)
    );
    assert_eq!(tic.time_minutes.len(), 15);
    assert_eq!(
        tic.intensity,
        (0..15).map(|i| f64::from(15 - i)).collect::<Vec<_>>()
    );
    let sic = chromatogram(&bytes, 22253, 1000);
    assert_eq!(
        sic.chromatogram_type.as_deref(),
        Some("selected ion current chromatogram")
    );
    assert_eq!(sic.time_minutes.len(), 10);
    assert_eq!(
        sic.intensity,
        (0..10).map(|i| f64::from(10 - i)).collect::<Vec<_>>()
    );
}

#[test]
fn reads_pyteomics_test_file_whose_index_is_stale() {
    let bytes = sample("pyteomics-test.mzML");
    // Its index was left from a larger file: it says it starts past the end, and its first
    // offset is a byte after the spectrum's "<".
    let offset = mzml::index_offset(&bytes).unwrap();
    assert_eq!(offset, 5_098_921);
    assert!(usize::try_from(offset).unwrap() > bytes.len());
    let error = mzml::read_element(&bytes[4363..], 1000).unwrap_err();
    assert!(error.contains("may be wrong"), "{error}");

    // So it's scanned instead.
    let index = mzml::scan(&bytes, true).unwrap();
    assert_eq!(index.declared_spectra, Some(2));
    assert_eq!(index.spectra.len(), 2);
    assert_eq!(index.chromatograms.len(), 1);
    assert_eq!(index.spectra[0].offset, 4362);
    for entry in index.spectra.iter().chain(&index.chromatograms) {
        let at = usize::try_from(entry.offset).unwrap();
        assert!(
            bytes[at..].starts_with(b"<spectrum ") || bytes[at..].starts_with(b"<chromatogram ")
        );
    }

    let plain = spectrum(&bytes, index.spectra[0].offset, 1_000_000);
    let packed = spectrum(&bytes, index.spectra[1].offset, 1_000_000);
    assert_eq!(plain.id, "controllerType=0 controllerNumber=1 scan=1");
    assert_eq!(plain.points, 19_914);
    assert_eq!(plain.mz.len(), 19_914);
    assert_eq!(packed.mz.len(), 19_914);
    assert!(!plain.truncated && !packed.truncated);
    // The same arrays: 64-bit m/z against the same rounded to 32 bits.
    assert!(
        plain
            .mz
            .iter()
            .zip(&packed.mz)
            .all(|(a, b)| (a - b).abs() <= a.abs() * f64::from(f32::EPSILON))
    );
    assert_eq!(plain.intensity, packed.intensity);
    // "lowest observed m/z" 200.00018816645022, "highest observed m/z" 2000.0099466203771.
    assert_eq!(plain.mz[0], 200.000_188_166_450_22);
    assert_eq!(*plain.mz.last().unwrap(), 2_000.009_946_620_377_1);
    // "base peak m/z" 810.415283203125 at 1471973.875 is the instrument's, from its own
    // peak picking; the profile's tallest point is beside it.
    let (mz, intensity) = base_peak(&plain);
    assert!((mz - 810.415_283_203_125).abs() < 0.001, "{mz}");
    assert!(
        (intensity - 1_471_973.875).abs() < 1_471_973.875 * 0.001,
        "{intensity}"
    );
    assert_eq!(plain.base_peak_mz, Some(810.415_283_203_125));
    assert_eq!(plain.ms_level, Some(1));
    assert_eq!(plain.polarity.as_deref(), Some("positive"));
    assert_eq!(plain.centroided, Some(false));
    assert_eq!(plain.scan_start_minutes, Some(0.004_935));

    // Downsampled to 2,000 points, a profile keeps its base peak.
    let preview = spectrum(&bytes, index.spectra[0].offset, 2000);
    assert!(preview.truncated);
    assert!(preview.mz.len() <= 2000);
    assert_eq!(base_peak(&preview), base_peak(&plain));
    assert!(preview.mz.windows(2).all(|pair| pair[0] < pair[1]));

    let tic = chromatogram(&bytes, index.chromatograms[0].offset, 1000);
    assert_eq!(tic.id, "TIC");
    assert_eq!(tic.points, 48);
    assert_eq!(tic.time_minutes.len(), 48);
    assert!(tic.time_minutes.windows(2).all(|pair| pair[0] < pair[1]));
}

#[test]
fn scanning_any_prefix_of_a_real_file_is_handled() {
    let bytes = sample("tiny.pwiz.1.1.mzML");
    let whole = mzml::scan(&bytes, true).unwrap();
    assert!(
        mzml::scan(&bytes[..30], false)
            .unwrap_err()
            .contains("before the file's <mzML>")
    );
    // From just past <indexedmzML ...>, the root, on.
    for length in (380..bytes.len()).step_by(61) {
        let prefix = &bytes[..length];
        let index = mzml::scan(prefix, false).unwrap_or_else(|error| panic!("{length}: {error}"));
        let ended = prefix.windows(7).any(|window| window == b"</mzML>");
        assert_eq!(index.truncated, !ended, "{length}");
        // What a prefix finds is the start of what the whole file has.
        assert!(whole.spectra.starts_with(&index.spectra), "{length}");
    }
}

#[test]
fn every_cut_of_a_real_element_is_an_error_not_a_panic() {
    let bytes = sample("pyteomics-test.mzML");
    let index = mzml::scan(&bytes, true).unwrap();
    let start = usize::try_from(index.spectra[0].offset).unwrap();
    let end = usize::try_from(index.spectra[1].offset).unwrap();
    let element = bytes[start..end].trim_ascii_end();
    assert!(mzml::read_element(element, 100).is_ok());
    for length in (0..element.len()).step_by(4999) {
        assert!(mzml::read_element(&bytes[start..start + length], 100).is_err());
    }
}
