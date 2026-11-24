//! SantaLucia's unified nearest-neighbour model for a DNA strand and its exact
//! complement: Iki's one implementation of it (ADR 0191).
//!
//! Everything that quotes such a temperature calls this, so they agree to the last digit:
//!
//! - the DNA sequence record's fixed-condition measurement, [`melting_temperature`],
//!   which the server, the browser and the DNA plugin's action all run;
//! - `thermodynamics::matched_melting_temperature`, which the primer checks and the
//!   plasmid plugin call under any buffer, and which reads the same sum;
//! - `thermodynamics::melting_temperature`, the duplex with mismatches, which takes its
//!   Watson-Crick stacks, initiation, salt term and rounding from here.
//!
//! The parameters are SantaLucia's unified set (*Proc Natl Acad Sci USA* 95:1460-1465,
//! 1998, <https://doi.org/10.1073/pnas.95.4.1460>), from Table 2 (stacks, initiation and symmetry).
//! Equation 3 gives the temperature
//!
//! ```text
//! Tm = 1000 * dH / (dS + R * ln(k)) - 273.15
//! ```
//!
//! with dH in kcal/mol, dS in cal/(mol*K), R = 1.987 cal/(mol*K) as the reference uses
//! it, and k the strands' total concentration divided by four for a strand and its
//! complement at equal concentrations. A self-complementary strand uses the sum of both
//! input amounts and the symmetry term. Equation 8 gives the sodium correction
//! `dS(Na+) = dS(1 M Na+) + 0.368 * (N - 1) * ln[Na+]` for a duplex of N bases.
//!
//! It needs nothing beyond `core`, so the record's measurement compiles to a module small
//! enough to translate for the browser.

/// The gas constant in cal/(mol*K), as the reference and Biopython use it.
pub const GAS_CONSTANT: f64 = 1.987;

/// The entropy, in cal/(mol*K), that a self-complementary duplex loses to its symmetry:
/// Table 2 of SantaLucia (1998).
pub const SYMMETRY_ENTROPY: f64 = -1.4;

/// The DNA sequence record's fixed conditions: 50 mM Na+, no Mg2+ or dNTPs, ...
const SODIUM: f64 = 50.0; // mM.
/// ... and 25 nM each of the strand and its complement: 50 nM of strands in all.
const STRAND: f64 = 25.0; // nM.

/// Initiation with this base pair at one end, per end: SantaLucia 1998's unified
/// parameters. Any case.
#[must_use]
pub fn initiation(base: u8) -> Option<(f64, f64)> {
    match base.to_ascii_uppercase() {
        b'A' | b'T' => Some((2.3, 4.1)),
        b'C' | b'G' => Some((0.1, -2.8)),
        _ => None,
    }
}

/// The Watson-Crick stack of `left` then `right` (5'->3') over their complements:
/// enthalpy in kcal/mol and entropy in cal/(mol*K), SantaLucia 1998's unified parameters. A doublet
/// and its reverse complement are one stack, read from either strand. Any case.
#[must_use]
pub fn stack(left: u8, right: u8) -> Option<(f64, f64)> {
    Some(
        match (left.to_ascii_uppercase(), right.to_ascii_uppercase()) {
            (b'A', b'A') | (b'T', b'T') => (-7.9, -22.2),
            (b'A', b'T') => (-7.2, -20.4),
            (b'T', b'A') => (-7.2, -21.3),
            (b'C', b'A') | (b'T', b'G') => (-8.5, -22.7),
            (b'G', b'T') | (b'A', b'C') => (-8.4, -22.4),
            (b'C', b'T') | (b'A', b'G') => (-7.8, -21.0),
            (b'G', b'A') | (b'T', b'C') => (-8.2, -22.2),
            (b'C', b'G') => (-10.6, -27.2),
            (b'G', b'C') => (-9.8, -24.4),
            (b'G', b'G') | (b'C', b'C') => (-8.0, -19.9),
            _ => return None,
        },
    )
}

/// Whether `bases` are their own reverse complement, as `GCGCGC` and every palindromic
/// restriction site are. Any case.
#[must_use]
pub fn self_complementary(bases: &[u8]) -> bool {
    bases.len().is_multiple_of(2)
        && bases
            .iter()
            .zip(bases.iter().rev())
            .take(bases.len() / 2)
            .all(|(&a, &b)| {
                matches!(
                    (a.to_ascii_uppercase(), b.to_ascii_uppercase()),
                    (b'A', b'T') | (b'T', b'A') | (b'C', b'G') | (b'G', b'C')
                )
            })
}

/// A duplex's sums at 1 M Na+, before any salt correction, and the strand concentration
/// the Tm equation's concentration term reads.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Duplex {
    /// Enthalpy, kcal/mol.
    pub enthalpy: f64,
    /// Entropy at 1 M Na+, cal/(mol*K).
    pub entropy: f64,
    /// Its length in base pairs.
    pub bases: usize,
    /// The natural logarithm of the Tm equation's concentration term k, in molar.
    pub ln_strands: f64,
}

/// `bases` against their exact complement, with `strand` nM of them and `other_strand` nM
/// of the complement. `None` for fewer than two bases, a base other than A, C, G or T (in
/// any case), or no strand left to melt.
///
/// A strand that is its own complement is SantaLucia's self-complementary case: there is
/// no second species in the tube, so all the strands, `strand + other_strand`, are this
/// one, and the duplex carries the symmetry term. The two go together; either alone is
/// further from his model than neither. Otherwise the term is the more concentrated strand
/// less half the other, which is the total over four when they are equal.
#[must_use]
pub fn matched(bases: &[u8], strand: f64, other_strand: f64) -> Option<Duplex> {
    if bases.len() < 2 {
        return None;
    }
    let (left_h, left_s) = initiation(*bases.first()?)?;
    let (right_h, right_s) = initiation(*bases.last()?)?;
    let mut enthalpy = left_h + right_h;
    let mut entropy = left_s + right_s;
    for pair in bases.windows(2) {
        let (h, s) = stack(pair[0], pair[1])?;
        enthalpy += h;
        entropy += s;
    }
    // Each branch takes its own logarithm, so fixed conditions fold to a constant.
    let ln_strands = if self_complementary(bases) {
        entropy += SYMMETRY_ENTROPY;
        logarithm((strand + other_strand) * 1e-9)?
    } else {
        logarithm((strand - other_strand / 2.0) * 1e-9)?
    };
    Some(Duplex {
        enthalpy,
        entropy,
        bases: bases.len(),
        ln_strands,
    })
}

/// The natural logarithm of a concentration, where there is any.
fn logarithm(molar: f64) -> Option<f64> {
    (molar > 0.0).then(|| molar.ln())
}

/// Degrees Celsius, unrounded, with SantaLucia's own salt term for `monovalent` molar of
/// monovalent cation, on the entropy. `None` where there is no cation to correct for.
#[must_use]
pub fn santalucia(duplex: &Duplex, monovalent: f64) -> Option<f64> {
    if monovalent <= 0.0 {
        return None;
    }
    let pairs = u32::try_from(duplex.bases.checked_sub(1)?).ok()?;
    let entropy = duplex.entropy + 0.368 * f64::from(pairs) * monovalent.ln();
    let celsius = 1000.0 * duplex.enthalpy / (entropy + GAS_CONSTANT * duplex.ln_strands) - 273.15;
    celsius.is_finite().then_some(celsius)
}

/// A temperature to one decimal place, halves going to the even digit: the rounding
/// Iki's records hold.
#[must_use]
pub fn rounded(celsius: f64) -> f64 {
    (celsius * 10.0).round_ties_even() / 10.0
}

/// The DNA sequence record's measurement: `bases` with their exact complement at 50 mM
/// Na+ alone and 50 nM of strands in all, in degrees Celsius to one decimal place. ASCII
/// A, C, G and T, in any case; at least two of them.
#[must_use]
pub fn melting_temperature(bases: &[u8]) -> Option<f64> {
    let duplex = matched(bases, STRAND, STRAND)?;
    santalucia(&duplex, SODIUM * 1e-3).map(rounded)
}

#[cfg(test)]
mod tests {
    use super::{matched, melting_temperature, self_complementary};

    #[test]
    fn matches_the_published_record_convention_including_palindromes() {
        // Independent Biopython DNA_NN3 reference at the conditions above. See
        // SantaLucia (1998), https://doi.org/10.1073/pnas.95.4.1460, equations 3 and 8.
        for (bases, expected) in [
            (b"GTAAAACGACGGCCAGT".as_slice(), 49.2), // Existing M13/pUC forward record.
            (b"CAGGAAACAGCTATGAC".as_slice(), 43.6), // Existing M13/pUC reverse record.
            (b"gAaTtC".as_slice(), -20.4), // Self-complementary; count concentration once.
        ] {
            assert_eq!(melting_temperature(bases), Some(expected));
        }
    }

    #[test]
    fn reports_no_estimate_for_an_undefined_input() {
        for bases in [b"".as_slice(), b"A", b"ANG", b"ACU", b"C G", &[0xff, 0xfe]] {
            assert_eq!(melting_temperature(bases), None);
        }
    }

    #[test]
    fn a_palindrome_is_one_species_carrying_the_symmetry_term() {
        // GGATCC, a BamHI site, by hand from the unified parameters: dH = 2 x 0.1 (G-C ends) - 8.0 (GG)
        // - 8.2 (GA) - 7.2 (AT) - 8.2 (TC) - 8.0 (CC) = -39.4; dS = 2 x -2.8 - 19.9 - 22.2
        // - 20.4 - 22.2 - 19.9 - 1.4 (symmetry) = -111.6, at the whole 50 nM. With
        // 0.368 x 5 x ln 0.05 = -5.512, Tm = -39400 / (-117.112 + 1.987 ln 5e-8) - 273.15
        // = -11.38.
        assert!(self_complementary(b"GGATCC"));
        let duplex = matched(b"GGATCC", 25.0, 25.0);
        assert!(duplex.is_some_and(|it| {
            (it.enthalpy + 39.4).abs() < 1e-9
                && (it.entropy + 111.6).abs() < 1e-9
                && (it.ln_strands - 50e-9_f64.ln()).abs() < 1e-12
        }));
        assert_eq!(melting_temperature(b"GGATCC"), Some(-11.4));
        assert!(!self_complementary(b"GGATCA"));
        assert!(!self_complementary(b"GGTCC"));
    }

    #[test]
    fn no_strand_left_has_no_temperature() {
        assert_eq!(matched(b"ACGTAC", 10.0, 20.0), None);
    }
}
