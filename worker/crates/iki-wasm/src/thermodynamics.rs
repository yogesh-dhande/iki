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
//!
//! A buffer with magnesium in it needs more than that term. [`Salt::Owczarzy`] gives the
//! measured corrections of Owczarzy et al. (*Biochemistry* 43:3537-3554, 2004, and
//! *Biochemistry* 47:5336-5353, 2008), which is what Primer3 computes a PCR primer's
//! melting temperature with.

use serde::{Deserialize, Serialize};

/// Watson-Crick nearest-neighbour stacks, 5'->3' on the top strand over 3'->5' on the
/// bottom: enthalpy in kcal/mol and entropy in cal/(mol*K). SantaLucia 1998, Table 1,
/// unified parameters. A stack not listed here is the same duplex read from its other
/// strand, which is the key reversed.
pub(crate) const STACK: [(&[u8; 5], f64, f64); 10] = [
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
pub(crate) const INTERNAL_MISMATCH: [(&[u8; 5], f64, f64); 51] = [
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
pub(crate) const TERMINAL_MISMATCH: [(&[u8; 5], f64, f64); 48] = [
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

/// How a buffer's salts are allowed for.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Salt {
    /// SantaLucia's own correction: a term on the entropy, `0.368 * (N - 1) * ln[Na+]`
    /// (*PNAS* 95:1460-1465, 1998, equation 7), with magnesium folded into a sodium
    /// equivalent by von Ahsen et al. It is what a primer's quoted melting temperature
    /// uses, and what von Ahsen et al. recommend inside a normal PCR buffer's range.
    SantaLucia,
    /// Owczarzy's corrections, measured over a wide range of both cations: a correction on
    /// `1/Tm` from the duplex's melting temperature in 1 M Na+. Monovalent ions alone take
    /// the sodium equation (*Biochemistry* 43:3537-3554, 2004, equation 22); where there
    /// is enough free magnesium to matter it takes the magnesium equation (*Biochemistry*
    /// 47:5336-5353, 2008, equation 16), whose three monovalent-dependent coefficients are
    /// used in between. This is the correction to quote above about 20 mM magnesium, where
    /// a sodium equivalent stops holding.
    Owczarzy,
}

/// What a duplex sits in and how much of it there is. Concentrations are millimolar for
/// the salts and nanomolar for the strands, as a buffer is written down.
///
/// A request need only name what differs from [`Conditions::PRIMER`], which is what is
/// left where a field is missing.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(default)]
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
    /// Which published salt correction to apply.
    pub salt: Salt,
}

impl Default for Conditions {
    /// The conditions a primer's own melting temperature is quoted under, which is what a
    /// request that says nothing about its buffer means.
    fn default() -> Self {
        Self::PRIMER
    }
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
        salt: Salt::SantaLucia,
    };

    /// A PCR buffer as Primer3 assumes one by default: 50 mM monovalent cation, 1.5 mM
    /// Mg2+, 0.6 mM dNTPs, and 50 nM of strands in all.
    pub const PCR: Self = Self {
        sodium: 50.0,
        potassium: 0.0,
        tris: 0.0,
        magnesium: 1.5,
        dntp: 0.6,
        strand: 25.0,
        other_strand: 25.0,
        salt: Salt::Owczarzy,
    };

    /// Total monovalent cation, in molar: sodium, potassium and half the Tris, since Tris
    /// is about half ionised at pH 8.3 (Owczarzy et al. 2008, with its equation 5).
    /// Magnesium is not in it.
    #[must_use]
    fn cations(&self) -> f64 {
        (self.sodium + self.potassium + self.tris / 2.0) * 1e-3
    }

    /// Magnesium the dNTPs leave free, in molar. They bind it tightly enough that the
    /// difference will do (Owczarzy et al. 2008; Sigel, *Chem Soc Rev* 22:255-267, 1993),
    /// and a buffer with at least as much dNTP as magnesium is treated as having a trace
    /// of it rather than none, so the logarithm of it stays finite.
    #[must_use]
    fn free_magnesium(&self) -> f64 {
        if self.dntp >= self.magnesium {
            1e-11
        } else {
            (self.magnesium - self.dntp) * 1e-3
        }
    }

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
pub(crate) fn listed(table: &[(&[u8; 5], f64, f64)], key: [u8; 5]) -> Option<(f64, f64)> {
    table
        .iter()
        .find(|(listed, _, _)| *listed == &key)
        .map(|&(_, enthalpy, entropy)| (enthalpy, entropy))
}

/// A key looked up in one of the tables above, or in that table read from the duplex's
/// other strand, which is the key reversed. The stacks and the internal mismatches are
/// listed once per pair of neighbours and found either way round; the terminal mismatches
/// are not, and are looked up with [`listed`] alone, which is how Biopython reads them.
pub(crate) fn parameter(table: &[(&[u8; 5], f64, f64)], key: [u8; 5]) -> Option<(f64, f64)> {
    let mut flipped = key;
    flipped.reverse();
    listed(table, key).or_else(|| listed(table, flipped))
}

/// A four-base key `top/bottom`, as the tables are written: the first two bases of each
/// strand, in order.
pub(crate) fn key(top: &[u8], bottom: &[u8]) -> Option<[u8; 5]> {
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
pub(crate) fn only_dna(bases: &[u8]) -> bool {
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
    #[expect(
        clippy::cast_precision_loss,
        reason = "a duplex is at most a few hundred bases"
    )]
    let length = (bases - 1) as f64;
    let celsius = match conditions.salt {
        Salt::SantaLucia => {
            let monovalent = conditions.monovalent();
            if monovalent <= 0.0 {
                return None;
            }
            entropy += 0.368 * length * monovalent.ln();
            1000.0 * enthalpy / (entropy + GAS_CONSTANT * strands.ln()) - 273.15
        }
        Salt::Owczarzy => {
            // The correction is on 1/Tm, measured from the duplex's melting temperature in
            // 1 M Na+, where SantaLucia's own entropy term is ln(1) and so vanishes.
            let kelvin = 1000.0 * enthalpy / (entropy + GAS_CONSTANT * strands.ln());
            let gc = top
                .iter()
                .filter(|base| matches!(base, b'G' | b'C'))
                .count();
            #[expect(
                clippy::cast_precision_loss,
                reason = "a duplex is at most a few hundred bases"
            )]
            let fraction = gc as f64 / bases as f64;
            1.0 / (1.0 / kelvin + owczarzy(conditions, fraction, length)?) - 273.15
        }
    };
    if celsius.is_finite() {
        Some(rounded(celsius))
    } else {
        None
    }
}

/// Where a buffer's magnesium stops being a detail and starts being what holds the duplex
/// together, by Owczarzy et al. 2008: the ratio sqrt([Mg2+]) / [Mon+]. Below the first
/// value the monovalent ions dominate; from the second the magnesium does, and in between
/// the two compete.
const MONOVALENT_UP_TO: f64 = 0.22;
const DIVALENT_FROM: f64 = 6.0;

/// Owczarzy's correction to add to `1/Tm(1 M Na+)`, in reciprocal Kelvin, for a duplex of
/// `length` + 1 base pairs whose bases are `fraction` G or C. `None` where the buffer has
/// no monovalent cation and no free magnesium either, so there is nothing to correct from.
///
/// The three regimes are the decision Owczarzy et al. 2008 set out (its figure 9), with
/// the coefficients of its equation 16 and the monovalent-dependent forms of a, d and g
/// that its competing regime calls for. Concentrations are molar and Tm is Kelvin, as the
/// paper has them.
#[expect(
    clippy::many_single_char_names,
    reason = "a to g are the coefficients' own names in Owczarzy et al. 2008"
)]
fn owczarzy(conditions: &Conditions, fraction: f64, length: f64) -> Option<f64> {
    let cations = conditions.cations();
    let magnesium = conditions.free_magnesium();
    // Primer3 takes a buffer with no monovalent cation at all as magnesium-dominant,
    // rather than dividing by zero.
    let ratio = if cations <= 0.0 {
        DIVALENT_FROM
    } else {
        magnesium.sqrt() / cations
    };
    if ratio < MONOVALENT_UP_TO {
        // Owczarzy et al. 2004, equation 22: the monovalent correction, which that paper's
        // central result is independent of the duplex's length.
        if cations <= 0.0 {
            return None;
        }
        let ln = cations.ln();
        return Some((4.29 * fraction - 3.95) * 1e-5 * ln + 9.40e-6 * ln * ln);
    }
    // Owczarzy et al. 2008, equation 16, with the coefficients of its table 2 at a
    // reference 1 M monovalent cation.
    let (mut a, b, c, mut d, e, f, mut g) = (
        3.92e-5, -9.11e-6, 6.26e-5, 1.42e-5, -4.82e-4, 5.25e-4, 8.31e-5,
    );
    if ratio < DIVALENT_FROM {
        // The two compete, and a, d and g become functions of the monovalent cation.
        let ln = cations.ln();
        a = 3.92e-5 * (0.843 - 0.352 * cations.sqrt() * ln);
        d = 1.42e-5 * (1.279 - 4.03e-3 * ln - 8.03e-3 * ln * ln);
        g = 8.31e-5 * (0.486 - 0.258 * ln + 5.25e-3 * ln * ln * ln);
    }
    let ln = magnesium.ln();
    Some(a + b * ln + fraction * (c + d * ln) + (1.0 / (2.0 * length)) * (e + f * ln + g * ln * ln))
}

/// Degrees Celsius for a primer against its exact complement.
///
/// An oligo that is its own complement -- `GCGCGC`, and every palindrome -- is handled as
/// SantaLucia's self-complementary case rather than as two different strands: there is no
/// second species in the tube, so all the strands are at the concentration `conditions`
/// gives for the first, and the duplex carries his symmetry term (1998, equation 3). An
/// oligo and a separate complement at the same total concentration melt about 3 degrees
/// lower, which is the difference this avoids reporting.
#[must_use]
pub fn matched_melting_temperature(bases: &[u8], conditions: &Conditions) -> Option<f64> {
    let complement: Vec<u8> = bases.iter().map(|base| complement(*base)).collect();
    let mut conditions = *conditions;
    if bases.iter().rev().eq(complement.iter()) {
        conditions.strand += conditions.other_strand;
        conditions.other_strand = 0.0;
    }
    melting_temperature(bases, &complement, &conditions)
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
    use super::{Conditions, Salt, matched_melting_temperature, melting_temperature};

    #[test]
    fn a_matched_primer_melts_where_the_reference_says() {
        // The M13/pUC primers, under the conditions Iki quotes a primer's Tm at.
        // SantaLucia 1998, equations 3 and 7; cross-checked against Biopython's Tm_NN
        // with DNA_NN3 and saltcorr=5 in spec/tests/test_plasmid_tools.py.
        for (bases, expected) in [
            (b"GTAAAACGACGGCCAGT".as_slice(), 49.2),
            (b"CAGGAAACAGCTATGAC".as_slice(), 43.6),
            // Its own complement, so there is no second species: all 50 nM of strands
            // are this one and the duplex carries the symmetry term. Biopython's Tm_NN
            // with selfcomp=True and dnac1=50 gives 36.27; as two strands at 25 nM each
            // it would give 34.87, which is the 1.4 degrees this convention is worth.
            (b"ACGTACGTACGT".as_slice(), 36.3),
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

    /// A buffer as Primer3's `oligotm` takes one: `-mv` monovalent, `-dv` magnesium, `-n`
    /// dNTPs, all millimolar, and its default 50 nM of strands in all.
    fn buffer(monovalent: f64, magnesium: f64, dntp: f64) -> Conditions {
        Conditions {
            sodium: monovalent,
            magnesium,
            dntp,
            salt: Salt::Owczarzy,
            ..Conditions::PRIMER
        }
    }

    #[test]
    fn owczarzys_corrections_give_primer3s_published_temperatures() {
        // Every row of Primer3's own `test/oligotm.txt` computed with its SantaLucia 1998
        // parameters and its Owczarzy salt correction (`-tp 1 -sc 2`), at its default
        // 50 nM of strands. Primer3 prints six decimal places; a tenth of a degree is all
        // Iki quotes, and the two agree to about a thousandth.
        let rows: &[(&[u8], f64, f64, f64, f64)] = &[
            (b"TCACGCTCACAAACGAGAGGAAG", 50.0, 0.0, 1.0, 56.755_143),
            (b"TCCCTTGCCTCGTTCAGTGC", 50.0, 0.0, 0.0, 57.675_190),
            (b"TCGGCCTCCCAAAGTGCTG", 50.0, 0.0, 1.5, 57.961_561),
            (b"TCTCGCCATTTTGGTCTTGCAG", 50.0, 1.0, 0.0, 61.298_237),
            (b"TGATCAGCACAGTGGCTCACG", 50.0, 1.5, 0.0, 63.610_135),
            (b"TGCCGTTCACGTCGTAGCTG", 50.0, 1.5, 0.8, 62.043_487),
            (b"TGGGCCTCCCAAAGTGCTG", 50.0, 2.5, 0.1, 64.566_813),
            (b"TTGCATGCCCAGAGTTGACG", 50.0, 2.5, 1.0, 61.807_390),
        ];
        for &(bases, monovalent, magnesium, dntp, expected) in rows {
            let got = matched_melting_temperature(bases, &buffer(monovalent, magnesium, dntp))
                .expect("every row is DNA");
            let name = String::from_utf8_lossy(bases);
            assert!(
                (got - expected).abs() < 0.05,
                "{name}: {got} against Primer3's {expected}"
            );
        }
    }

    #[test]
    fn the_regimes_part_where_owczarzy_says_they_do() {
        // Primer3's `test/oligotm.txt` holds one sequence at six monovalent
        // concentrations, chosen to sit either side of the ratio of 6 at which magnesium
        // stops competing with the monovalent cations. Below it the three
        // monovalent-dependent coefficients are used and the temperature moves; at and
        // above it they are not, and the same temperature comes back however little
        // monovalent cation there is.
        let bases = b"AAGGCGAGTCAGGCTCAGTG";
        for monovalent in [0.0, 0.5, 5.0, 5.5] {
            let got = matched_melting_temperature(bases, &buffer(monovalent, 1.5, 0.0))
                .expect("it is DNA");
            assert!(
                (got - 63.257_974).abs() < 0.05,
                "{monovalent} mM: {got} against Primer3's 63.257_974"
            );
        }
        let competing =
            matched_melting_temperature(bases, &buffer(10.0, 1.5, 0.0)).expect("it is DNA");
        assert!(
            (competing - 62.346_915).abs() < 0.05,
            "10 mM: {competing} against Primer3's 62.346_915"
        );
    }

    #[test]
    fn a_measured_duplex_melts_about_where_owczarzy_measured_it() {
        // Owczarzy et al. 2008, Table S1: ATCAATCATA in 3.0 mM Mg2+ with no KCl, at a
        // total strand concentration of 2 uM, melts at 27.4 C. The buffer is 2 mM
        // Tris-HCl, about half of which is cation. The prediction is not the measurement,
        // and the paper's own average error is a degree or so, so this asks for one
        // degree -- enough to catch a wrong regime, a dropped length term or a sign.
        let measured = Conditions {
            sodium: 0.0,
            tris: 2.0,
            magnesium: 3.0,
            dntp: 0.0,
            strand: 1000.0,
            other_strand: 1000.0,
            salt: Salt::Owczarzy,
            ..Conditions::PRIMER
        };
        let got = matched_melting_temperature(b"ATCAATCATA", &measured).expect("it is DNA");
        assert!((got - 27.4).abs() < 1.0, "{got} against a measured 27.4");

        // Its 1 M Na+ reference column, the state the correction is measured from, is
        // 33.6 C measured against 31.3 C predicted from SantaLucia's parameters alone.
        // That 2.3 C is the nearest-neighbour model's own error, largest for a short
        // AT-rich duplex like this one, and not something a salt correction fixes:
        // SantaLucia 1998 reports about 2 C on average over its own set. The corrected
        // prediction above lands nearer the measurement than this reference does, because
        // Owczarzy's coefficients were fitted on measured temperatures. So this asks only
        // that the reference state is in the right place to a few degrees, which is what
        // catches a sign or a dropped term.
        let one_molar = Conditions {
            sodium: 1000.0,
            strand: 1000.0,
            other_strand: 1000.0,
            ..Conditions::PRIMER
        };
        let reference = matched_melting_temperature(b"ATCAATCATA", &one_molar).expect("it is DNA");
        assert!(
            (reference - 33.6).abs() < 3.0,
            "{reference} against a measured 33.6"
        );
    }

    #[test]
    fn a_pcr_buffer_melts_a_primer_higher_than_sodium_alone() {
        // The magnesium and dNTPs of a PCR buffer raise a primer's melting temperature by
        // several degrees, which is why a primer's own quoted number and the one a PCR
        // needs are not the same number.
        let primer = b"GTAAAACGACGGCCAGT";
        let quoted = matched_melting_temperature(primer, &Conditions::PRIMER).expect("DNA");
        let in_pcr = matched_melting_temperature(primer, &Conditions::PCR).expect("DNA");
        assert!(
            in_pcr > quoted + 3.0 && in_pcr < quoted + 10.0,
            "{in_pcr} in a PCR buffer against {quoted} in sodium alone"
        );
    }
}
