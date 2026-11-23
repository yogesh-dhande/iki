//! The melting temperature of a short DNA duplex, mismatches and all, by the unified
//! nearest-neighbour model.
//!
//! A duplex is read as a run of neighbouring base pairs, each contributing an enthalpy
//! and an entropy, plus a term for each end. The parameters are SantaLucia's unified set
//! (SantaLucia, *Proc Natl Acad Sci USA* 95:1460-1465, 1998,
//! <https://doi.org/10.1073/pnas.95.4.1460>), whose Watson-Crick stacks are those of
//! Allawi & SantaLucia (*Biochemistry* 36:10581-10594, 1997,
//! <https://doi.org/10.1021/bi962590c>). A pair that isn't Watson-Crick takes an internal
//! mismatch parameter (Allawi & SantaLucia 1997-1998 and Peyret et al., *Biochemistry*
//! 38:3468-3477, 1999, <https://doi.org/10.1021/bi9825091>) or, at either end of the
//! duplex, a terminal mismatch parameter (SantaLucia & Peyret, 2001; Bommarito et al.,
//! *Nucleic Acids Res* 28:1929-1934, 2000, <https://doi.org/10.1093/nar/28.9.1929>).
//!
//! Then
//!
//! ```text
//! Tm = 1000 * dH / (dS + R * ln(k)) - 273.15
//! ```
//!
//! with dH in kcal/mol, dS in cal/(mol*K), R = 1.987 cal/(mol*K) the gas constant as the
//! reference uses it, and k the concentration term of SantaLucia 1998 equation 3: the
//! strands' concentration divided by four when a strand pairs with its complement, or the
//! whole of it when it pairs with itself, where the symmetry term also applies.
//!
//! Monovalent salt enters through the entropy, by equation 7 of the same paper:
//!
//! ```text
//! dS(Na+) = dS(1 M Na+) + 0.368 * (N - 1) * ln[Na+]
//! ```
//!
//! for a duplex of N bases.
//!
//! **Why it is written this way.** Iki's plasmid plugin computed these temperatures with
//! Biopython's `Bio.SeqUtils.MeltingTemp.Tm_NN`, with its `DNA_NN3` table and
//! `saltcorr=5`. This module is that calculation, so the server, the worker and the web
//! packages share one implementation and give one answer; Biopython stays in the tests as
//! the oracle it is cross-checked against. That is why the steps below follow Biopython's
//! order exactly, including where it reads a term from the untrimmed strand.

/// Watson-Crick nearest-neighbour stacks, 5'->3' on the top strand over 3'->5' on the
/// bottom: enthalpy in kcal/mol and entropy in cal/(mol*K). SantaLucia 1998, Table 1,
/// unified parameters. A stack not listed here is the same duplex read from its other
/// strand, which is the key reversed.
const STACK: [(&[u8; 5], f64, f64); 10] = [
    (b"AA/TT", -7.9, -22.2),
    (b"AT/TA", -7.2, -20.4),
    (b"CA/GT", -8.5, -22.7),
    (b"CG/GC", -10.6, -27.2),
    (b"CT/GA", -7.8, -21.0),
    (b"GA/CT", -8.2, -22.2),
    (b"GC/CG", -9.8, -24.4),
    (b"GG/CC", -8.0, -19.9),
    (b"GT/CA", -8.4, -22.4),
    (b"TA/AT", -7.2, -21.3),
];

/// A pair that isn't Watson-Crick, inside the duplex. Allawi & SantaLucia 1997-1998 and
/// Peyret et al. 1999, as Biopython's `DNA_IMM1` collects them. The inosine entries of
/// that table (Watkins & SantaLucia 2005) are left out: Iki's templates and primers are
/// A, C, G and T.
const INTERNAL_MISMATCH: [(&[u8; 5], f64, f64); 51] = [
    (b"AA/TA", 1.2, 1.7),
    (b"AA/TC", 2.3, 4.6),
    (b"AA/TG", -0.6, -2.3),
    (b"AC/TA", 5.3, 14.6),
    (b"AC/TC", 0.0, -4.4),
    (b"AC/TT", 0.7, 0.2),
    (b"AG/TA", -0.7, -2.3),
    (b"AG/TG", -3.1, -9.5),
    (b"AG/TT", 1.0, 0.9),
    (b"AT/TC", -1.2, -6.2),
    (b"AT/TG", -2.5, -8.3),
    (b"AT/TT", -2.7, -10.8),
    (b"CA/GA", -0.9, -4.2),
    (b"CA/GC", 1.9, 3.7),
    (b"CA/GG", -0.7, -2.3),
    (b"CC/GA", 0.6, -0.6),
    (b"CC/GC", -1.5, -7.2),
    (b"CC/GT", -0.8, -4.5),
    (b"CG/GA", -4.0, -13.2),
    (b"CG/GG", -4.9, -15.3),
    (b"CG/GT", -4.1, -11.7),
    (b"CT/GC", -1.5, -6.1),
    (b"CT/GG", -2.8, -8.0),
    (b"CT/GT", -5.0, -15.8),
    (b"GA/CA", -2.9, -9.8),
    (b"GA/CC", 5.2, 14.2),
    (b"GA/CG", -0.6, -1.0),
    (b"GC/CA", -0.7, -3.8),
    (b"GC/CC", 3.6, 8.9),
    (b"GC/CT", 2.3, 5.4),
    (b"GG/CA", 0.5, 3.2),
    (b"GG/CG", -6.0, -15.8),
    (b"GG/CT", 3.3, 10.4),
    (b"GG/TT", 5.8, 16.3),
    (b"GT/CC", 5.2, 13.5),
    (b"GT/CG", -4.4, -12.3),
    (b"GT/CT", -2.2, -8.4),
    (b"GT/TG", 4.1, 9.5),
    (b"TA/AA", 4.7, 12.9),
    (b"TA/AC", 3.4, 8.0),
    (b"TA/AG", 0.7, 0.7),
    (b"TC/AA", 7.6, 20.2),
    (b"TC/AC", 6.1, 16.4),
    (b"TC/AT", 1.2, 0.7),
    (b"TG/AA", 3.0, 7.4),
    (b"TG/AG", 1.6, 3.6),
    (b"TG/AT", -0.1, -1.7),
    (b"TG/GT", -1.4, -6.2),
    (b"TT/AC", 1.0, 0.7),
    (b"TT/AG", -1.3, -5.3),
    (b"TT/AT", 0.2, -1.5),
];

/// A pair that isn't Watson-Crick at one end of the duplex, where the neighbouring stack
/// is its only support. SantaLucia & Peyret 2001, as Biopython's `DNA_TMM1` collects
/// them, from Bommarito et al. 2000.
const TERMINAL_MISMATCH: [(&[u8; 5], f64, f64); 48] = [
    (b"AA/TA", -3.1, -7.8),
    (b"AA/TC", -1.6, -4.0),
    (b"AA/TG", -1.9, -4.4),
    (b"AC/TA", -1.8, -3.8),
    (b"AC/TC", -0.1, 0.5),
    (b"AC/TT", -0.9, -1.7),
    (b"AG/TA", -2.5, -5.9),
    (b"AG/TG", -1.1, -2.1),
    (b"AG/TT", -3.2, -8.7),
    (b"AT/TC", -2.3, -6.3),
    (b"AT/TG", -3.5, -9.4),
    (b"AT/TT", -2.4, -6.5),
    (b"CA/GA", -4.3, -10.7),
    (b"CA/GC", -2.6, -5.9),
    (b"CA/GG", -3.9, -9.6),
    (b"CC/GA", -2.7, -6.0),
    (b"CC/GC", -2.1, -5.1),
    (b"CC/GT", -3.2, -8.0),
    (b"CG/GA", -6.0, -15.5),
    (b"CG/GG", -3.8, -9.5),
    (b"CG/GT", -3.8, -9.0),
    (b"CT/GC", -3.9, -10.6),
    (b"CT/GG", -6.6, -18.7),
    (b"CT/GT", -6.1, -16.9),
    (b"GA/CA", -8.0, -22.5),
    (b"GA/CC", -5.0, -13.8),
    (b"GA/CG", -4.3, -11.1),
    (b"GC/CA", -3.2, -7.1),
    (b"GC/CC", -3.9, -10.6),
    (b"GC/CT", -4.9, -13.5),
    (b"GG/CA", -4.6, -11.4),
    (b"GG/CG", -0.7, -19.2),
    (b"GG/CT", -5.7, -15.9),
    (b"GT/CC", -3.0, -7.8),
    (b"GT/CG", -5.9, -16.1),
    (b"GT/CT", -7.4, -21.2),
    (b"TA/AA", -2.5, -6.3),
    (b"TA/AC", -2.3, -5.9),
    (b"TA/AG", -2.0, -4.7),
    (b"TC/AA", -2.7, -7.0),
    (b"TC/AC", -0.7, -1.3),
    (b"TC/AT", -2.5, -6.3),
    (b"TG/AA", -2.4, -5.8),
    (b"TG/AG", -1.1, -2.7),
    (b"TG/AT", -3.9, -10.5),
    (b"TT/AC", -0.7, -1.2),
    (b"TT/AG", -3.6, -9.8),
    (b"TT/AT", -3.2, -8.9),
];

/// Initiation with a terminal A or T base pair, per end: SantaLucia 1998, Table 1.
const INITIATION_AT: (f64, f64) = (2.3, 4.1);
/// Initiation with a terminal G or C base pair, per end.
const INITIATION_GC: (f64, f64) = (0.1, -2.8);
/// The entropy a self-complementary strand loses to symmetry: SantaLucia 1998, Table 1.
const SYMMETRY: (f64, f64) = (0.0, -1.4);
/// The gas constant in cal/(mol*K), as the reference and Biopython use it.
const GAS_CONSTANT: f64 = 1.987;

/// What a duplex sits in and how much of it there is.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Conditions {
    /// Sodium, in millimolar.
    pub sodium: f64,
    /// Potassium, in millimolar.
    pub potassium: f64,
    /// Tris buffer, in millimolar; half of it counts as monovalent cation.
    pub tris: f64,
    /// Magnesium, in millimolar.
    pub magnesium: f64,
    /// dNTPs, in millimolar: they chelate magnesium.
    pub dntp: f64,
    /// The more concentrated strand (the primer or probe), in nanomolar.
    pub strand: f64,
    /// The other strand (the template), in nanomolar.
    pub other_strand: f64,
}

impl Conditions {
    /// The conditions a primer's own melting temperature is quoted under across Iki:
    /// 50 mM Na+, no Mg2+ or dNTPs, 25 nM of each strand.
    pub const PRIMER: Self = Self {
        sodium: 50.0,
        potassium: 0.0,
        tris: 0.0,
        magnesium: 0.0,
        dntp: 0.0,
        strand: 25.0,
        other_strand: 25.0,
    };

    /// Total monovalent cation, in molar, by von Ahsen et al. (*Clin Chem*
    /// 47:1956-1961, 2001, <https://doi.org/10.1093/clinchem/47.11.1956>): sodium,
    /// potassium and half the Tris, plus a sodium equivalent for whatever magnesium the
    /// dNTPs leave free. Magnesium counts only where there is more of it than of dNTPs,
    /// which bind it strongly.
    #[must_use]
    fn monovalent(&self) -> f64 {
        let mut millimolar = self.sodium + self.potassium + self.tris / 2.0;
        let others = self.potassium + self.magnesium + self.tris + self.dntp;
        if others > 0.0 && self.dntp < self.magnesium {
            millimolar += 120.0 * (self.magnesium - self.dntp).sqrt();
        }
        millimolar * 1e-3
    }
}

/// A key looked up in one of the tables above exactly as written.
fn listed(table: &[(&[u8; 5], f64, f64)], key: [u8; 5]) -> Option<(f64, f64)> {
    table
        .iter()
        .find(|(listed, _, _)| *listed == &key)
        .map(|&(_, enthalpy, entropy)| (enthalpy, entropy))
}

/// A key looked up in one of the tables above, or in that table read from the duplex's
/// other strand, which is the key reversed. The stacks and the internal mismatches are
/// listed once per pair of neighbours and found either way round; the terminal mismatches
/// are not, and are looked up with [`listed`] alone, which is how Biopython reads them.
fn parameter(table: &[(&[u8; 5], f64, f64)], key: [u8; 5]) -> Option<(f64, f64)> {
    let mut flipped = key;
    flipped.reverse();
    listed(table, key).or_else(|| listed(table, flipped))
}

/// A four-base key `top/bottom`, as the tables are written: the first two bases of each
/// strand, in order.
fn key(top: &[u8], bottom: &[u8]) -> Option<[u8; 5]> {
    Some([
        *top.first()?,
        *top.get(1)?,
        b'/',
        *bottom.first()?,
        *bottom.get(1)?,
    ])
}

/// The key for the pair of neighbours at the left end of a duplex, which the terminal
/// mismatch table lists from the other side: each strand's first two bases, each read
/// backwards, with the bottom strand's first. So a duplex beginning `CA` over `GT` is
/// looked up as `TG/AC`.
fn left_key(top: &[u8], bottom: &[u8]) -> Option<[u8; 5]> {
    Some([
        *bottom.get(1)?,
        *bottom.first()?,
        b'/',
        *top.get(1)?,
        *top.first()?,
    ])
}

/// Whether every base is one the parameters are known for.
fn only_dna(bases: &[u8]) -> bool {
    bases
        .iter()
        .all(|base| matches!(base, b'A' | b'C' | b'G' | b'T'))
}

/// Degrees Celsius for `top` (5'->3') against `bottom`, the other strand base for base
/// under it and so read 3'->5'. The two must be the same length, with no gap: this is a
/// duplex of paired and mispaired bases, not an alignment.
///
/// `None` where a base isn't A, C, G or T, where there are fewer than two of them, or
/// where a pair of neighbours has no published parameter -- rather than an answer that
/// looks like a measurement and isn't.
#[must_use]
pub fn melting_temperature(top: &[u8], bottom: &[u8], conditions: &Conditions) -> Option<f64> {
    if top.len() != bottom.len() || top.len() < 2 || !only_dna(top) || !only_dna(bottom) {
        return None;
    }
    let bases = top.len();
    let mut enthalpy = 0.0;
    let mut entropy = 0.0;

    // Each end, where a mispaired base takes a terminal parameter instead of the stack,
    // and the pair it replaces leaves the run that is zipped up below. The left end's key
    // is read from the duplex's other side, as the table is written.
    let (mut inner_top, mut inner_bottom) = (top, bottom);
    let left = left_key(top, bottom);
    if let Some((h, s)) = left.and_then(|it| listed(&TERMINAL_MISMATCH, it)) {
        enthalpy += h;
        entropy += s;
        inner_top = inner_top.get(1..).unwrap_or_default();
        inner_bottom = inner_bottom.get(1..).unwrap_or_default();
    }
    let right = key(
        inner_top
            .get(inner_top.len().saturating_sub(2)..)
            .unwrap_or_default(),
        inner_bottom
            .get(inner_bottom.len().saturating_sub(2)..)
            .unwrap_or_default(),
    );
    if let Some((h, s)) = right.and_then(|it| listed(&TERMINAL_MISMATCH, it)) {
        enthalpy += h;
        entropy += s;
        let shorter = inner_top.len().saturating_sub(1);
        inner_top = inner_top.get(..shorter).unwrap_or_default();
        inner_bottom = inner_bottom.get(..shorter).unwrap_or_default();
    }

    // Initiation, by the base at each end of the whole strand -- not of what a terminal
    // mismatch left, which is the order Biopython reads these in.
    for end in [*top.first()?, *top.last()?] {
        let (h, s) = if matches!(end, b'A' | b'T') {
            INITIATION_AT
        } else {
            INITIATION_GC
        };
        enthalpy += h;
        entropy += s;
    }

    // Then the run between the ends, pair of neighbours by pair of neighbours: a
    // mispaired base takes an internal mismatch parameter, a paired one its stack.
    for at in 0..inner_top.len().saturating_sub(1) {
        let it = key(inner_top.get(at..)?, inner_bottom.get(at..)?)?;
        let (h, s) = parameter(&INTERNAL_MISMATCH, it).or_else(|| parameter(&STACK, it))?;
        enthalpy += h;
        entropy += s;
    }

    // One strand pairs with itself at the whole concentration, and loses the symmetry
    // term; otherwise it pairs with its complement at a quarter of it (SantaLucia 1998,
    // equation 3, as Biopython reads the two strands' concentrations).
    let self_complementary = conditions.other_strand == 0.0;
    let strands = if self_complementary {
        enthalpy += SYMMETRY.0;
        entropy += SYMMETRY.1;
        conditions.strand * 1e-9
    } else {
        (conditions.strand - conditions.other_strand / 2.0) * 1e-9
    };
    if strands <= 0.0 {
        return None;
    }
    let monovalent = conditions.monovalent();
    if monovalent <= 0.0 {
        return None;
    }
    #[expect(
        clippy::cast_precision_loss,
        reason = "a duplex is at most a few hundred bases"
    )]
    let length = (bases - 1) as f64;
    entropy += 0.368 * length * monovalent.ln();

    let celsius = 1000.0 * enthalpy / (entropy + GAS_CONSTANT * strands.ln()) - 273.15;
    if celsius.is_finite() {
        Some(rounded(celsius))
    } else {
        None
    }
}

/// Degrees Celsius for a primer against its exact complement.
#[must_use]
pub fn matched_melting_temperature(bases: &[u8], conditions: &Conditions) -> Option<f64> {
    let complement: Vec<u8> = bases.iter().map(|base| complement(*base)).collect();
    melting_temperature(bases, &complement, conditions)
}

/// The base that pairs with this one, or the byte itself where nothing pairs with it.
#[must_use]
pub const fn complement(base: u8) -> u8 {
    match base {
        b'A' => b'T',
        b'C' => b'G',
        b'G' => b'C',
        b'T' => b'A',
        other => other,
    }
}

/// A temperature to one decimal place, rounded as a decimal with halves going to the even
/// digit -- the rounding Iki's records already hold, so a ported measurement reads the
/// same to the last digit.
fn rounded(celsius: f64) -> f64 {
    let decimal = format!("{celsius:.1}");
    decimal.parse().unwrap_or(celsius)
}

#[cfg(test)]
mod tests {
    use super::{Conditions, matched_melting_temperature, melting_temperature};

    #[test]
    fn a_matched_primer_melts_where_the_reference_says() {
        // The M13/pUC primers, under the conditions Iki quotes a primer's Tm at.
        // SantaLucia 1998, equations 3 and 7; cross-checked against Biopython's Tm_NN
        // with DNA_NN3 and saltcorr=5 in spec/tests/test_plasmid_tools.py.
        for (bases, expected) in [
            (b"GTAAAACGACGGCCAGT".as_slice(), 49.2),
            (b"CAGGAAACAGCTATGAC".as_slice(), 43.6),
            (b"ACGTACGTACGT".as_slice(), 34.9),
        ] {
            assert_eq!(
                matched_melting_temperature(bases, &Conditions::PRIMER),
                Some(expected)
            );
        }
    }

    #[test]
    fn a_mismatch_lowers_it() {
        let bases = b"GTAAAACGACGGCCAGT";
        let matched = matched_melting_temperature(bases, &Conditions::PRIMER);
        // The same primer against a template whose middle base doesn't pair with it.
        let mut bottom: Vec<u8> = bases.iter().map(|b| super::complement(*b)).collect();
        bottom[8] = b'A';
        let mismatched = melting_temperature(bases, &bottom, &Conditions::PRIMER);
        assert!(
            matched > mismatched,
            "{matched:?} should beat {mismatched:?}"
        );
    }

    #[test]
    fn nothing_is_reported_where_no_parameter_applies() {
        for (top, bottom) in [
            (b"".as_slice(), b"".as_slice()),
            (b"A", b"T"),
            (b"ACGT", b"TGC"),
            (b"ACNT", b"TGNA"),
            (b"ACGU", b"TGCA"),
        ] {
            assert_eq!(
                melting_temperature(top, bottom, &Conditions::PRIMER),
                None,
                "{}",
                String::from_utf8_lossy(top)
            );
        }
    }

    #[test]
    fn a_salt_free_buffer_has_no_melting_temperature() {
        let dry = Conditions {
            sodium: 0.0,
            ..Conditions::PRIMER
        };
        assert_eq!(
            matched_melting_temperature(b"GTAAAACGACGGCCAGT", &dry),
            None
        );
    }
}
