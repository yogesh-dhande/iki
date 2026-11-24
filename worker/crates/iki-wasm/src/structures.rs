//! What a primer does instead of binding its target: fold back on itself as a hairpin, or
//! pair with another copy of itself or with its partner as a dimer.
//!
//! A primer that holds a stable structure is a primer that works badly. Its 3' end may be
//! tied up where a polymerase needs it, so no product appears; a stable primer dimer is
//! amplified in place of the target, and a hairpin whose stem outlasts the annealing step
//! keeps the primer out of the reaction. This module finds the most stable structure of
//! each kind and says how stable it is and where it sits, which is the check Benchling,
//! SnapGene, Primer3 and IDT's OligoAnalyzer all offer.
//!
//! # What stability means here
//!
//! A structure's stability is its standard free energy of formation at 37 °C, written
//! dG(37), in kcal/mol: negative means it forms, and the more negative the worse the
//! primer. IDT's guidance, which the field follows, is that a hairpin or dimer at or below
//! about -9 kcal/mol is a problem and that a structure holding a 3' end matters more than
//! one in the middle, so [`Structure`] reports both the energy and whether a 3' end is in
//! it.
//!
//! dG(37) is the sum of the published free energies of the motifs the structure is made of
//! -- the nearest-neighbour model of DNA folding.
//!
//! # Where the numbers come from, and why from one place
//!
//! Every parameter below is the DNA set of the **NNDB**, the Nearest Neighbor Database
//! (Turner & Mathews, *Nucleic Acids Res* 38:D280-D282, 2010,
//! <https://doi.org/10.1093/nar/gkp892>; Mittal, Turner & Mathews, *J Mol Biol*
//! 436:168549, 2024, <https://doi.org/10.1016/j.jmb.2024.168549>), which is the set the
//! Mathews lab publishes and maintains for the RNAstructure folding package (Reuter &
//! Mathews, *BMC Bioinformatics* 11:129, 2010,
//! <https://doi.org/10.1186/1471-2105-11-129>). Its stacks, mismatches and dangling ends
//! rest on the SantaLucia lab's measurements -- SantaLucia's unified parameters (*PNAS*
//! 95:1460-1465, 1998), Allawi & SantaLucia's mismatch series, Peyret et al.
//! (*Biochemistry* 38:3468-3477, 1999) and Bommarito, Peyret & SantaLucia (*Nucleic Acids
//! Res* 28:1929-1934, 2000) -- and its stack free energies are SantaLucia's to a tenth of
//! a kcal/mol.
//!
//! It is used whole rather than mixed with another set, because a parameter set's loop and
//! initiation terms are fitted against its own stacks. NNDB's DNA section charges a duplex
//! +1.0 kcal/mol to open at all and nothing extra for ending on an A-T pair, where
//! SantaLucia & Hicks' review charges about +1.0 at *each* end and +0.05 per terminal A-T;
//! taking the loop table from one and the initiation from the other would be a kcal/mol of
//! invented chemistry. So [`crate::thermodynamics`] keeps SantaLucia's parameters for the
//! melting temperature a duplex is quoted by, and this module keeps NNDB's for the folding
//! a structure is scored by, each the set its own field uses, each cited.
//!
//! NNDB was also the only set whose every table could be read in full: SantaLucia & Hicks'
//! loop and special-loop values live in a supplement behind a paywall, and Primer3's
//! copies of them are GPL-licensed data files whose tetraloop entropies are in the wrong
//! scale -- its own maintainers say its structure numbers are "not intended to be a
//! completely accurate secondary structure prediction tool" and are shown to warn a user
//! rather than to score a primer.
//!
//! # What this does not do
//!
//! It is a nearest-neighbour model of one structure, not a partition function: it reports
//! the most stable structure, not the ensemble free energy that UNAFold or RNAstructure
//! computes, and it finds no pseudoknots and no multi-branch loops -- a hairpin here has
//! one loop and a dimer one helix, each with the bulges and internal loops inside it. It
//! reports a free energy and not a melting temperature, because NNDB publishes no entropy
//! for its DNA loops and a temperature derived from an assumed one would read like a
//! measurement without being one. These are a screen, and a borderline primer is decided
//! at the bench.

use serde::{Deserialize, Serialize};

/// A helical stack between neighbouring Watson-Crick pairs, `top/bottom` with the top
/// strand read 5'->3' and the bottom 3'->5': free energy at 37 °C in kcal/mol. NNDB's DNA
/// Watson-Crick stack table, whose values are SantaLucia's unified parameters (*PNAS*
/// 95:1460-1465, 1998) to a tenth of a kcal/mol. Each stack is listed both ways round, as
/// the table has them, so no lookup has to reverse a key.
pub const STACK: [(&[u8; 5], f64); 16] = [
    (b"AA/TT", -1.0),
    (b"AC/TG", -1.4),
    (b"AG/TC", -1.3),
    (b"AT/TA", -0.9),
    (b"CA/GT", -1.5),
    (b"CC/GG", -1.8),
    (b"CG/GC", -2.2),
    (b"CT/GA", -1.3),
    (b"GA/CT", -1.3),
    (b"GC/CG", -2.2),
    (b"GG/CC", -1.8),
    (b"GT/CA", -1.4),
    (b"TA/AT", -0.6),
    (b"TC/AG", -1.3),
    (b"TG/AC", -1.5),
    (b"TT/AA", -1.0),
];

/// A helix ending on a mispaired pair, read outwards: the Watson-Crick pair that closes
/// the helix first, then the mispaired bases. Free energy at 37 °C in kcal/mol. NNDB's DNA
/// terminal mismatch table, which its hairpin first-mismatch table repeats exactly, from
/// Bommarito, Peyret & SantaLucia (*Nucleic Acids Res* 28:1929-1934, 2000) and the
/// SantaLucia lab's mismatch series.
pub const TERMINAL_MISMATCH: [(&[u8; 5], f64); 64] = [
    (b"AA/TA", -0.7),
    (b"AA/TC", -0.3),
    (b"AA/TG", -0.5),
    (b"AA/TT", 0.0),
    (b"AC/TA", -0.6),
    (b"AC/TC", -0.2),
    (b"AC/TG", 0.0),
    (b"AC/TT", -0.3),
    (b"AG/TA", -0.6),
    (b"AG/TC", 0.0),
    (b"AG/TG", -0.4),
    (b"AG/TT", -0.5),
    (b"AT/TA", 0.0),
    (b"AT/TC", -0.3),
    (b"AT/TG", -0.5),
    (b"AT/TT", -0.4),
    (b"CA/GA", -1.0),
    (b"CA/GC", -0.8),
    (b"CA/GG", -0.9),
    (b"CA/GT", 0.0),
    (b"CC/GA", -0.8),
    (b"CC/GC", -0.5),
    (b"CC/GG", 0.0),
    (b"CC/GT", -0.7),
    (b"CG/GA", -1.0),
    (b"CG/GC", 0.0),
    (b"CG/GG", -0.9),
    (b"CG/GT", -1.0),
    (b"CT/GA", 0.0),
    (b"CT/GC", -0.6),
    (b"CT/GG", -0.9),
    (b"CT/GT", -0.9),
    (b"GA/CA", -1.0),
    (b"GA/CC", -0.7),
    (b"GA/CG", -0.8),
    (b"GA/CT", 0.0),
    (b"GC/CA", -1.0),
    (b"GC/CC", -0.6),
    (b"GC/CG", 0.0),
    (b"GC/CT", -0.7),
    (b"GG/CA", -1.0),
    (b"GG/CC", 0.0),
    (b"GG/CG", -1.0),
    (b"GG/CT", -0.8),
    (b"GT/CA", 0.0),
    (b"GT/CC", -0.6),
    (b"GT/CG", -0.9),
    (b"GT/CT", -0.9),
    (b"TA/AA", -0.6),
    (b"TA/AC", -0.4),
    (b"TA/AG", -0.5),
    (b"TA/AT", 0.0),
    (b"TC/AA", -0.5),
    (b"TC/AC", -0.2),
    (b"TC/AG", 0.0),
    (b"TC/AT", -0.5),
    (b"TG/AA", -0.6),
    (b"TG/AC", -0.0),
    (b"TG/AG", -0.4),
    (b"TG/AT", -0.5),
    (b"TT/AA", 0.0),
    (b"TT/AC", -0.3),
    (b"TT/AG", -0.6),
    (b"TT/AT", -0.3),
];

/// One base of a strand running on past the end of a helix, with `.` for the strand that
/// has stopped: free energy at 37 °C in kcal/mol. NNDB's DNA dangling-end tables
/// (Bommarito, Peyret & SantaLucia 2000).
///
/// Both tables are written for the right-hand end of a helix drawn with the top strand
/// 5'->3'. There the top strand's 3' end and the bottom strand's 5' end meet, so an
/// overhang is either on the strand that ends 3' there, in [`THREE_PRIME_DANGLE`], or on
/// the one that ends 5' there, in [`FIVE_PRIME_DANGLE`]. A helix's left-hand end is the
/// same picture read from the other strand, so it uses the same two tables with the
/// strands swapped.
pub const THREE_PRIME_DANGLE: [(&[u8; 5], f64); 16] = [
    (b"AA/T.", -0.2),
    (b"AC/T.", -0.2),
    (b"AG/T.", -0.2),
    (b"AT/T.", -0.2),
    (b"CA/G.", -1.1),
    (b"CC/G.", -0.4),
    (b"CG/G.", -1.1),
    (b"CT/G.", -0.8),
    (b"GA/C.", -0.4),
    (b"GC/C.", -0.2),
    (b"GG/C.", -0.4),
    (b"GT/C.", -0.3),
    (b"TA/A.", -0.2),
    (b"TC/A.", -0.2),
    (b"TG/A.", -0.2),
    (b"TT/A.", -0.2),
];

/// A base on the strand that ends 5' at a helix's end; see [`THREE_PRIME_DANGLE`].
pub const FIVE_PRIME_DANGLE: [(&[u8; 5], f64); 16] = [
    (b"A./TA", -0.5),
    (b"A./TC", -0.2),
    (b"A./TG", -0.5),
    (b"A./TT", -0.3),
    (b"C./GA", -0.8),
    (b"C./GC", -0.5),
    (b"C./GG", -0.8),
    (b"C./GT", -0.7),
    (b"G./CA", -0.9),
    (b"G./CC", -0.5),
    (b"G./CG", -0.7),
    (b"G./CT", -0.6),
    (b"T./AA", -0.5),
    (b"T./AC", -0.2),
    (b"T./AG", -0.6),
    (b"T./AT", -0.3),
];

/// One row of [`LOOP`]: how many bases are unpaired, then what an internal loop, a bulge
/// and a hairpin loop of that size cost to open.
pub type Opening = (usize, Option<f64>, Option<f64>, Option<f64>);

/// What a loop costs by the number of unpaired bases in it, as free energy at 37 °C in
/// kcal/mol: NNDB's DNA loop table. `None` is a loop that size cannot be -- a hairpin
/// needs three unpaired bases to turn in, and an internal loop four to have two on each
/// strand.
pub const LOOP: [Opening; 30] = [
    (1, None, Some(2.9), None),
    (2, None, Some(2.3), None),
    (3, None, Some(2.5), Some(3.4)),
    (4, Some(3.1), Some(2.7), Some(3.4)),
    (5, Some(3.5), Some(3.0), Some(3.5)),
    (6, Some(3.9), Some(3.2), Some(4.2)),
    (7, Some(4.1), Some(3.4), Some(4.2)),
    (8, Some(4.2), Some(3.5), Some(4.2)),
    (9, Some(4.3), Some(3.6), Some(4.3)),
    (10, Some(4.5), Some(3.7), Some(4.4)),
    (11, Some(4.6), Some(3.9), Some(4.5)),
    (12, Some(4.6), Some(3.9), Some(4.6)),
    (13, Some(4.7), Some(4.0), Some(4.7)),
    (14, Some(4.8), Some(4.1), Some(4.8)),
    (15, Some(4.9), Some(4.2), Some(5.0)),
    (16, Some(5.0), Some(4.3), Some(5.1)),
    (17, Some(5.0), Some(4.3), Some(5.2)),
    (18, Some(5.1), Some(4.4), Some(5.3)),
    (19, Some(5.1), Some(4.4), Some(5.4)),
    (20, Some(5.2), Some(4.5), Some(5.5)),
    (21, Some(5.3), Some(4.5), Some(5.6)),
    (22, Some(5.3), Some(4.6), Some(5.7)),
    (23, Some(5.3), Some(4.6), Some(5.8)),
    (24, Some(5.4), Some(4.7), Some(5.9)),
    (25, Some(5.4), Some(4.7), Some(6.0)),
    (26, Some(5.5), Some(4.7), Some(6.1)),
    (27, Some(5.5), Some(4.8), Some(6.2)),
    (28, Some(5.6), Some(4.9), Some(6.3)),
    (29, Some(5.6), Some(4.9), Some(6.4)),
    (30, Some(5.6), Some(4.9), Some(6.5)),
];

/// The hairpin loops NNDB's DNA section measures in their own right, which replace the
/// loop term altogether rather than adding to it. The key is the closing pair's 5' base,
/// the loop, and the closing pair's 3' base, so it is five bases for a three-base loop and
/// six for a four-base one. Free energy at 37 °C in kcal/mol, from NNDB's DNA triloop and
/// tetraloop tables.
pub const MEASURED_LOOP: [(&[u8], f64); 8] = [
    (b"GGCAC", 2.6),
    (b"CGCAG", -0.2),
    (b"ATTTTT", -0.8),
    (b"ACGTGT", 1.4),
    (b"CAAAAG", 3.2),
    (b"CGACTG", 1.3),
    (b"GCTTGC", 3.4),
    (b"CGACCG", 1.3),
];

/// Opening a helix between two molecules, once per dimer: +1.0 kcal/mol at 37 °C (NNDB's
/// DNA Watson-Crick helix page). A hairpin is one molecule folding, not two meeting, so it
/// does not pay this.
pub const MEETING: f64 = 1.0;
/// What a self-complementary duplex pays for its own symmetry: +0.43 kcal/mol (NNDB).
pub const SYMMETRY: f64 = 0.43;
/// What an internal loop pays for being uneven, per base of difference between its two
/// sides, and the most it can pay: 0.4 kcal/mol up to 3.0 (the Ninio equation, as NNDB's
/// DNA internal-loop page applies it).
pub const ASYMMETRY: f64 = 0.4;
/// The most an uneven internal loop can pay for being uneven, in kcal/mol.
pub const MOST_ASYMMETRY: f64 = 3.0;
/// The gas constant times 310.15 K, in kcal/mol: what a single state is worth, used for
/// the states a single-base bulge can slide between.
pub const RT: f64 = 0.616;
/// The shortest loop a hairpin can turn in: three unpaired bases (NNDB).
pub const SHORTEST_LOOP: usize = 3;
/// The most unpaired bases one bulge or internal loop may have in all. NNDB's loop table
/// stops here, and so does the search.
pub const LONGEST_LOOP: usize = 30;
/// The free energy at or below which a structure is worth warning about, in kcal/mol:
/// IDT's own guidance for an oligo's hairpins and dimers.
pub const CONCERNING: f64 = -9.0;
/// How near a 3' end counts as holding it: a structure whose pairs reach into the last few
/// bases of either oligo is the one that stops a polymerase.
pub const THREE_PRIME: usize = 5;

/// Whether two bases pair, Watson-Crick only: a DNA helix here is A-T and C-G pairs, and
/// anything else inside it is a mismatch the loop and mismatch parameters cover.
#[must_use]
pub const fn paired(one: u8, other: u8) -> bool {
    matches!(
        (one, other),
        (b'A', b'T') | (b'T', b'A') | (b'C', b'G') | (b'G', b'C')
    )
}

/// One of the four-base tables above, looked up exactly as written.
fn listed(table: &[(&[u8; 5], f64)], key: [u8; 5]) -> Option<f64> {
    table
        .iter()
        .find(|(listed, _)| *listed == &key)
        .map(|&(_, value)| value)
}

/// A four-base key `ab/cd`, as the tables are written.
const fn key(a: u8, b: u8, c: u8, d: u8) -> [u8; 5] {
    [a, b, b'/', c, d]
}

/// The stack between the pair `(a, c)` and the pair `(b, d)` next to it, both
/// Watson-Crick, in kcal/mol.
fn stack(a: u8, b: u8, c: u8, d: u8) -> Option<f64> {
    listed(&STACK, key(a, b, c, d))
}

/// What a loop of `size` unpaired bases costs to open, in kcal/mol, or `None` where a loop
/// of that kind cannot be that size or the search does not go that far.
fn opening(size: usize, which: fn(&Opening) -> Option<f64>) -> Option<f64> {
    LOOP.iter().find(|row| row.0 == size).and_then(which)
}

fn hairpin_opening(size: usize) -> Option<f64> {
    opening(size, |row| row.3)
}

fn bulge_opening(size: usize) -> Option<f64> {
    opening(size, |row| row.2)
}

fn internal_opening(size: usize) -> Option<f64> {
    opening(size, |row| row.1)
}

/// What a helix's end costs, where the strands run on past it or stop.
///
/// Both tables are written for the right-hand end of a helix drawn with the top strand
/// 5'->3', so `pair` is that end's base pair and `beyond` the next base along each strand,
/// `None` where that strand has ended. A helix's left-hand end is the same picture read
/// from the other strand, so the caller swaps the strands and reads the bases inwards.
///
/// Where both strands run on and their bases do not pair, the two stack against each other
/// as a terminal mismatch; where one has ended, the other dangles. The least of the terms
/// that apply is taken, and never a positive one: a flanking base that would destabilise
/// the helix simply does not stack against it.
fn helix_end(pair: (u8, u8), beyond: (Option<u8>, Option<u8>)) -> f64 {
    let mut best = 0.0_f64;
    match beyond {
        (Some(top), Some(bottom)) if !paired(top, bottom) => {
            if let Some(it) = listed(&TERMINAL_MISMATCH, key(pair.0, top, pair.1, bottom)) {
                best = best.min(it);
            }
        }
        _ => {}
    }
    if let Some(top) = beyond.0
        && let Some(it) = listed(&THREE_PRIME_DANGLE, key(pair.0, top, pair.1, b'.'))
    {
        best = best.min(it);
    }
    if let Some(bottom) = beyond.1
        && let Some(it) = listed(&FIVE_PRIME_DANGLE, key(pair.0, b'.', pair.1, bottom))
    {
        best = best.min(it);
    }
    best
}

/// What the bulge or internal loop between two pairs costs, in kcal/mol.
///
/// `outer` is the pair the loop opens from and `inner` the pair it closes on, each as the
/// bases on the two strands; `beside` is the first unpaired base on each strand going into
/// the loop, and `across` the last one coming out of it. `on_top` and `on_bottom` are how
/// many bases are unpaired on each strand.
///
/// A bulge of a single base keeps the helical stack across it, because the helix is not
/// really interrupted, and gains back what the base is worth being able to sit in any of
/// several equivalent places (NNDB's DNA bulge-loop page). A longer bulge pays its opening
/// alone. An internal loop pays its opening, a penalty for being uneven, and a mismatch
/// term where each side of it meets the helix -- except a loop with a single base on one
/// side, whose mismatches NNDB sets to nothing.
fn loop_cost(
    outer: (u8, u8),
    inner: (u8, u8),
    beside: (Option<u8>, Option<u8>),
    across: (Option<u8>, Option<u8>),
    on_top: usize,
    on_bottom: usize,
    states: usize,
) -> Option<f64> {
    let unpaired = on_top + on_bottom;
    if unpaired == 0 || unpaired > LONGEST_LOOP {
        return None;
    }
    if on_top == 0 || on_bottom == 0 {
        let opening = bulge_opening(unpaired)?;
        if unpaired == 1 {
            #[expect(
                clippy::cast_precision_loss,
                reason = "a single bulge slides between at most a few places"
            )]
            let ways = states.max(1) as f64;
            return Some(opening + stack(outer.0, inner.0, outer.1, inner.1)? - RT * ways.ln());
        }
        return Some(opening);
    }
    let opening = internal_opening(unpaired)?;
    #[expect(
        clippy::cast_precision_loss,
        reason = "a loop has at most thirty unpaired bases"
    )]
    let uneven = (on_top.abs_diff(on_bottom)) as f64;
    let mut total = opening + (ASYMMETRY * uneven).min(MOST_ASYMMETRY);
    if on_top > 1 && on_bottom > 1 {
        // Each side's first unpaired base pair stacks on the helix it leaves.
        let (Some(top_in), Some(bottom_in)) = beside else {
            return None;
        };
        let (Some(top_out), Some(bottom_out)) = across else {
            return None;
        };
        total += listed(&TERMINAL_MISMATCH, key(outer.0, top_in, outer.1, bottom_in))?;
        total += listed(
            &TERMINAL_MISMATCH,
            key(inner.1, bottom_out, inner.0, top_out),
        )?;
    }
    Some(total)
}

/// How many equivalent places a single bulged base could sit: itself, and each paired
/// neighbour on its own strand that is the same base, since sliding the bulge along a run
/// of identical bases gives the same structure (NNDB's DNA bulge-loop page counts these
/// states and credits them as -RT ln of their number).
fn sliding_states(strand: &[u8], bulged: usize) -> usize {
    let base = strand[bulged];
    let mut states = 1;
    if bulged > 0 && strand.get(bulged - 1) == Some(&base) {
        states += 1;
    }
    if strand.get(bulged + 1) == Some(&base) {
        states += 1;
    }
    states
}

/// Only A, C, G and T fold: an IUPAC code has no published parameters, so a sequence
/// holding one gets no structure rather than a guessed one.
fn only_dna(bases: &[u8]) -> bool {
    !bases.is_empty()
        && bases
            .iter()
            .all(|base| matches!(base, b'A' | b'C' | b'G' | b'T'))
}

/// The bases of an oligo, upper-cased, with U read as T.
#[must_use]
pub fn read(bases: &str) -> Vec<u8> {
    bases
        .bytes()
        .map(|base| match base.to_ascii_uppercase() {
            b'U' => b'T',
            other => other,
        })
        .collect()
}

/// The most stable dimer two oligos form, each given 5'->3'. `None` where either holds a
/// base that isn't A, C, G or T, or where no pair of bases pairs at all.
///
/// The search is over every way the two can be laid against each other -- every register,
/// with bulges and internal loops inside the helix -- and keeps the most stable. It is a
/// dynamic program over the pairs: each cell holds the best helix whose 3'-most pair on the
/// first oligo is that one, reached either by stacking on the pair before it or across a
/// loop, and the answer is the best cell once the far end and the cost of two molecules
/// meeting are added.
#[must_use]
#[expect(
    clippy::too_many_lines,
    reason = "a dynamic program is its fill and its traceback, and splitting them would \
              hide which table each step reads"
)]
pub fn dimer(first: &[u8], second: &[u8]) -> Option<Structure> {
    if !only_dna(first) || !only_dna(second) {
        return None;
    }
    let top = first;
    let bottom: Vec<u8> = second.iter().rev().copied().collect();
    let (rows, columns) = (top.len(), bottom.len());
    let mut best = vec![f64::INFINITY; rows * columns];
    let mut from = vec![None; rows * columns];
    let at = |i: usize, j: usize| i * columns + j;

    for i in 0..rows {
        for j in 0..columns {
            if !paired(top[i], bottom[j]) {
                continue;
            }
            // The near end of a helix that starts here, read from the other strand.
            let mut here = helix_end(
                (bottom[j], top[i]),
                (
                    j.checked_sub(1).map(|was| bottom[was]),
                    i.checked_sub(1).map(|was| top[was]),
                ),
            );
            let mut came_from = None;
            if let (Some(before), Some(under)) = (i.checked_sub(1), j.checked_sub(1))
                && best[at(before, under)].is_finite()
                && let Some(it) = stack(top[before], top[i], bottom[under], bottom[j])
            {
                let candidate = best[at(before, under)] + it;
                if candidate < here {
                    here = candidate;
                    came_from = Some((before, under));
                }
            }
            for outer in i.saturating_sub(LONGEST_LOOP + 1)..i.saturating_sub(1) {
                for under in j.saturating_sub(LONGEST_LOOP + 1)..j.saturating_sub(1) {
                    if !best[at(outer, under)].is_finite() {
                        continue;
                    }
                    let (on_top, on_bottom) = (i - outer - 1, j - under - 1);
                    let states = if on_top == 1 && on_bottom == 0 {
                        sliding_states(top, outer + 1)
                    } else if on_bottom == 1 && on_top == 0 {
                        sliding_states(&bottom, under + 1)
                    } else {
                        1
                    };
                    let Some(it) = loop_cost(
                        (top[outer], bottom[under]),
                        (top[i], bottom[j]),
                        (top.get(outer + 1).copied(), bottom.get(under + 1).copied()),
                        (top.get(i - 1).copied(), bottom.get(j - 1).copied()),
                        on_top,
                        on_bottom,
                        states,
                    ) else {
                        continue;
                    };
                    let candidate = best[at(outer, under)] + it;
                    if candidate < here {
                        here = candidate;
                        came_from = Some((outer, under));
                    }
                }
            }
            best[at(i, j)] = here;
            from[at(i, j)] = came_from;
        }
    }

    let itself = first == second;
    let opening = MEETING
        + if itself && is_self_complementary(first) {
            SYMMETRY
        } else {
            0.0
        };
    let mut found: Option<(f64, usize, usize)> = None;
    for i in 0..rows {
        for j in 0..columns {
            if !best[at(i, j)].is_finite() {
                continue;
            }
            let far = helix_end(
                (top[i], bottom[j]),
                (top.get(i + 1).copied(), bottom.get(j + 1).copied()),
            );
            let total = best[at(i, j)] + far + opening;
            if found.is_none_or(|(was, _, _)| total < was) {
                found = Some((total, i, j));
            }
        }
    }
    let (free_energy, mut i, mut j) = found?;
    let mut pairs = vec![];
    loop {
        pairs.push((i + 1, columns - j));
        match from[at(i, j)] {
            Some((outer, under)) => {
                i = outer;
                j = under;
            }
            None => break,
        }
    }
    pairs.reverse();
    Some(assembled(
        if itself { Kind::SelfDimer } else { Kind::Dimer },
        free_energy,
        pairs,
        first,
        second,
    ))
}

/// Whether an oligo is its own reverse complement, which costs a duplex of two copies of
/// it the symmetry term.
fn is_self_complementary(bases: &[u8]) -> bool {
    bases
        .iter()
        .zip(bases.iter().rev())
        .all(|(one, other)| paired(*one, *other))
}

/// The most stable hairpin an oligo folds into, given 5'->3'. `None` where it holds a base
/// that isn't A, C, G or T, or where it cannot pair with itself at all.
///
/// A hairpin here is one stem closing on one loop, with bulges and internal loops inside
/// the stem. The dynamic program works outwards: each cell holds the best structure
/// enclosed by that pair, which either closes the loop itself or encloses a smaller one,
/// and the answer is the best cell once the open end of the stem is added. There is no cost
/// for two molecules meeting, because this is one molecule folding.
#[must_use]
#[expect(
    clippy::too_many_lines,
    reason = "a dynamic program is its fill and its traceback, and splitting them would \
              hide which table each step reads"
)]
pub fn hairpin(bases: &[u8]) -> Option<Structure> {
    if !only_dna(bases) {
        return None;
    }
    let length = bases.len();
    let mut closed = vec![f64::INFINITY; length * length];
    let mut from = vec![None; length * length];
    let at = |i: usize, j: usize| i * length + j;

    // Outwards, so a pair's own cell is filled before anything that encloses it.
    for span in (SHORTEST_LOOP + 1)..length {
        for i in 0..length.saturating_sub(span) {
            let j = i + span;
            if !paired(bases[i], bases[j]) {
                continue;
            }
            let turn = span - 1;
            let mut here = f64::INFINITY;
            let mut came_from = None;
            // Closing the loop here. A loop NNDB measures in its own right replaces the
            // whole term; a loop of three turns too tightly for a mismatch to stack in it.
            let measured = MEASURED_LOOP
                .iter()
                .find(|(listed, _)| *listed == &bases[i..=j])
                .map(|&(_, it)| it);
            let own = if let Some(it) = measured {
                Some(it)
            } else if turn == SHORTEST_LOOP {
                hairpin_opening(turn)
            } else {
                hairpin_opening(turn).and_then(|opening| {
                    listed(
                        &TERMINAL_MISMATCH,
                        key(bases[i], bases[i + 1], bases[j], bases[j - 1]),
                    )
                    .map(|mismatch| opening + mismatch)
                })
            };
            if let Some(it) = own {
                here = it;
            }
            if closed[at(i + 1, j - 1)].is_finite()
                && let Some(it) = stack(bases[i], bases[i + 1], bases[j], bases[j - 1])
            {
                let candidate = closed[at(i + 1, j - 1)] + it;
                if candidate < here {
                    here = candidate;
                    came_from = Some((i + 1, j - 1));
                }
            }
            for inner in (i + 2)..=(i + 1 + LONGEST_LOOP).min(j) {
                for under in (inner + SHORTEST_LOOP + 1)..j {
                    if !closed[at(inner, under)].is_finite() {
                        continue;
                    }
                    let (on_top, on_bottom) = (inner - i - 1, j - under - 1);
                    let states = if on_top == 1 && on_bottom == 0 {
                        sliding_states(bases, i + 1)
                    } else if on_bottom == 1 && on_top == 0 {
                        sliding_states(bases, j - 1)
                    } else {
                        1
                    };
                    let Some(it) = loop_cost(
                        (bases[i], bases[j]),
                        (bases[inner], bases[under]),
                        (
                            bases.get(i + 1).copied(),
                            j.checked_sub(1).and_then(|was| bases.get(was).copied()),
                        ),
                        (
                            inner.checked_sub(1).and_then(|was| bases.get(was).copied()),
                            bases.get(under + 1).copied(),
                        ),
                        on_top,
                        on_bottom,
                        states,
                    ) else {
                        continue;
                    };
                    let candidate = closed[at(inner, under)] + it;
                    if candidate < here {
                        here = candidate;
                        came_from = Some((inner, under));
                    }
                }
            }
            closed[at(i, j)] = here;
            from[at(i, j)] = came_from;
        }
    }

    let mut found: Option<(f64, usize, usize)> = None;
    for i in 0..length {
        for j in (i + SHORTEST_LOOP + 1)..length {
            if !closed[at(i, j)].is_finite() {
                continue;
            }
            // The stem's one open end, read from the strand coming back down it.
            let open = helix_end(
                (bases[j], bases[i]),
                (
                    bases.get(j + 1).copied(),
                    i.checked_sub(1).and_then(|was| bases.get(was).copied()),
                ),
            );
            let total = closed[at(i, j)] + open;
            if found.is_none_or(|(was, _, _)| total < was) {
                found = Some((total, i, j));
            }
        }
    }
    let (free_energy, mut i, mut j) = found?;
    let mut pairs = vec![];
    loop {
        pairs.push((i + 1, j + 1));
        match from[at(i, j)] {
            Some((inner, under)) => {
                i = inner;
                j = under;
            }
            None => break,
        }
    }
    Some(assembled(Kind::Hairpin, free_energy, pairs, bases, bases))
}

/// What kind of structure was looked for.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    /// One oligo folding back on itself.
    Hairpin,
    /// Two copies of one oligo pairing with each other.
    SelfDimer,
    /// Two different oligos pairing with each other.
    Dimer,
}

impl Kind {
    /// What a record and a diagram call it.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Hairpin => "hairpin",
            Self::SelfDimer => "self_dimer",
            Self::Dimer => "dimer",
        }
    }
}

/// The most stable structure of one kind, and how stable it is.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Structure {
    /// Which of the three it is.
    pub kind: Kind,
    /// Its free energy of formation at 37 °C, in kcal/mol, to two decimal places. Negative
    /// means it forms, and the more negative the more stable.
    pub free_energy: f64,
    /// Each base pair, as a position in the first oligo and one in the second, both from 1
    /// and both read 5'->3' along their own oligo, 5'-most on the first oligo first. For a
    /// hairpin both positions are in the one oligo, so the first is in its 5' arm and the
    /// second in its 3' arm.
    pub pairs: Vec<(usize, usize)>,
    /// How many pairs there are.
    pub paired: usize,
    /// Whether the structure reaches the last [`THREE_PRIME`] bases of either oligo, which
    /// is the end a polymerase extends from and so the one that matters most.
    pub holds_three_prime: bool,
    /// Whether it is at or below [`CONCERNING`] and so worth warning about.
    pub concerning: bool,
    /// Three lines of equal length, to show in a fixed-width block: the first oligo, the
    /// pairs between them, and the second read the other way so that it lies under the
    /// first. A hairpin's second line is its 3' arm, and its loop follows in brackets.
    pub diagram: Vec<String>,
}

/// Round to two decimal places, which is as fine as a sum of one-decimal parameters means
/// anything, and keeps a reported energy from carrying binary noise.
fn rounded(energy: f64) -> f64 {
    (energy * 100.0).round() / 100.0
}

/// Fill in everything a [`Structure`] reports besides its pairs and its energy: how many
/// pairs, whether a 3' end is in them, whether it is worth warning about, and the diagram.
fn assembled(
    kind: Kind,
    free_energy: f64,
    pairs: Vec<(usize, usize)>,
    first: &[u8],
    second: &[u8],
) -> Structure {
    let near_end = |which: usize, of: usize| of.saturating_sub(which) < THREE_PRIME;
    let holds_three_prime = pairs.iter().any(|&(one, other)| {
        near_end(one, first.len())
            || (kind == Kind::Hairpin && near_end(other, first.len()))
            || (kind != Kind::Hairpin && near_end(other, second.len()))
    });
    let diagram = drawn(kind, &pairs, first, second);
    Structure {
        kind,
        free_energy: rounded(free_energy),
        paired: pairs.len(),
        holds_three_prime,
        concerning: free_energy <= CONCERNING,
        pairs,
        diagram,
    }
}

/// The structure drawn in three lines of equal length.
///
/// The first oligo runs 5'->3' from the left; the second lies under it, read 3'->5', so
/// that paired bases sit in the same column and an unpaired stretch on one strand pushes
/// the other along. `|` marks each pair.
///
/// A hairpin is drawn the same way, as its 5' arm over its 3' arm: the stem is between
/// them and the loop, which belongs to neither arm, follows the top line in brackets.
fn drawn(kind: Kind, pairs: &[(usize, usize)], first: &[u8], second: &[u8]) -> Vec<String> {
    // The second oligo is drawn backwards, so that paired bases share a column and it
    // reads 3'->5'. A hairpin's second oligo is the same one, drawn as its 3' arm.
    let bottom: Vec<u8> = second.iter().rev().copied().collect();
    // Each pair's position along the drawing: the first oligo as it is, the second counted
    // from its 3' end so it reads right to left.
    let columns: Vec<(usize, usize)> = pairs
        .iter()
        .map(|&(one, other)| (one - 1, bottom.len() - other))
        .collect();
    let Some(&(first_top, first_bottom)) = columns.first() else {
        return vec![String::new(); 3];
    };
    let (last_top, last_bottom) = columns.last().copied().unwrap_or((first_top, first_bottom));

    let mut top_line = String::new();
    let mut bonds = String::new();
    let mut bottom_line = String::new();
    let push = |a: Option<u8>,
                bond: char,
                b: Option<u8>,
                top_line: &mut String,
                bonds: &mut String,
                bottom_line: &mut String| {
        top_line.push(a.map_or(' ', char::from));
        bonds.push(bond);
        bottom_line.push(b.map_or(' ', char::from));
    };

    // The overhangs before the first pair, the longer one setting how far in it sits.
    let lead = first_top.max(first_bottom);
    for step in 0..lead {
        let from_start = lead - step;
        let one = first_top
            .checked_sub(from_start)
            .and_then(|which| first.get(which).copied());
        let other = first_bottom
            .checked_sub(from_start)
            .and_then(|which| bottom.get(which).copied());
        push(one, ' ', other, &mut top_line, &mut bonds, &mut bottom_line);
    }
    // Then each pair, and whatever is unpaired between it and the next.
    for (which, &(one, other)) in columns.iter().enumerate() {
        push(
            first.get(one).copied(),
            '|',
            bottom.get(other).copied(),
            &mut top_line,
            &mut bonds,
            &mut bottom_line,
        );
        let Some(&(next_one, next_other)) = columns.get(which + 1) else {
            continue;
        };
        let between = (next_one - one - 1).max(next_other - other - 1);
        for step in 1..=between {
            let along = |from: usize, limit: usize, strand: &[u8]| {
                if from + step < limit {
                    strand.get(from + step).copied()
                } else {
                    None
                }
            };
            let a = along(one, next_one, first);
            let b = along(other, next_other, &bottom);
            push(a, ' ', b, &mut top_line, &mut bonds, &mut bottom_line);
        }
    }
    // And the overhangs after the last pair.
    let tail = (first.len() - last_top - 1).max(bottom.len() - last_bottom - 1);
    for step in 1..=tail {
        let a = (last_top + step < first.len()).then(|| first[last_top + step]);
        let b = (last_bottom + step < bottom.len()).then(|| bottom[last_bottom + step]);
        push(a, ' ', b, &mut top_line, &mut bonds, &mut bottom_line);
    }

    if kind == Kind::Hairpin {
        // A hairpin's two arms are both in the drawing, so each line holds the whole oligo
        // once over from each end. Cut each back at the innermost pair, which is where the
        // loop begins, and show the loop after the top line.
        let stem = bonds.rfind('|').map_or(0, |last| last + 1);
        let &(innermost_top, innermost_bottom) = columns.last().unwrap_or(&(0, 0));
        let loop_bases: String = first
            .get((innermost_top + 1)..(bottom.len() - innermost_bottom - 1))
            .map(|it| String::from_utf8_lossy(it).into_owned())
            .unwrap_or_default();
        top_line.truncate(stem);
        bonds.truncate(stem);
        bottom_line.truncate(stem);
        return vec![
            format!("5' {top_line} ({loop_bases})"),
            format!("   {bonds}"),
            format!("3' {bottom_line}"),
        ];
    }
    vec![
        format!("5' {top_line} 3'"),
        format!("   {bonds}"),
        format!("3' {bottom_line} 5'"),
    ]
}

/// What a run couldn't check, and why.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refused(pub String);

/// The most an oligo may be: long enough for any primer or probe, short enough that the
/// search over every pair of positions stays quick.
pub const LONGEST_OLIGO: usize = 200;
/// The most oligos one request may carry.
pub const MOST_OLIGOS: usize = 500;
/// How many bases at the 3' end a primer's end stability is read over: Primer3's own
/// `PRIMER_MAX_END_STABILITY` is the free energy of the last five.
pub const END_STABILITY_OVER: usize = 5;

/// One oligo to check.
#[derive(Debug, Clone, Deserialize)]
pub struct Oligo {
    /// Its human-readable ID, which the answer names it by.
    pub hrid: String,
    /// Its name, for a diagram's caption.
    #[serde(default)]
    pub name: String,
    /// Its bases, 5'->3'.
    pub bases: String,
}

/// A request to check a set of oligos, and the pairs of them that go in a tube together.
#[derive(Debug, Clone, Deserialize)]
pub struct Request {
    /// The oligos.
    pub oligos: Vec<Oligo>,
    /// The buffer their melting temperatures are quoted under. Left out, it is the one a
    /// PCR runs in, which is what a primer is designed for.
    #[serde(default = "pcr")]
    pub conditions: crate::thermodynamics::Conditions,
    /// Pairs of oligos, by their IDs, to check against each other. A pair naming an oligo
    /// the request doesn't carry is refused rather than quietly skipped.
    #[serde(default)]
    pub pairs: Vec<(String, String)>,
}

/// The buffer a request that doesn't say means: a PCR's.
fn pcr() -> crate::thermodynamics::Conditions {
    crate::thermodynamics::Conditions::PCR
}

/// What one oligo's checks came to.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Checked {
    /// The oligo's ID, as the request gave it.
    pub oligo: String,
    /// Its name, or its ID where it hasn't got one.
    pub name: String,
    /// How many bases it has.
    pub length: usize,
    /// What proportion of them are G or C, as a percentage to two decimal places.
    pub gc_content: f64,
    /// Its melting temperature against its exact complement under the request's buffer, in
    /// °C, or nothing where it holds a base with no published parameters.
    pub melting_temperature: Option<f64>,
    /// The free energy at 37 °C of the last [`END_STABILITY_OVER`] bases' stacks, in
    /// kcal/mol: how firmly the 3' end a polymerase extends from is held down. Primer3
    /// calls this a primer's end stability and warns on a value that is too negative,
    /// because such an end binds in the wrong place as readily as the right one.
    pub end_stability: Option<f64>,
    /// The longest run of one base: a run of four or more can slip against a template and
    /// is worth avoiding.
    pub longest_run: usize,
    /// Which base that run is of.
    pub run_of: Option<char>,
    /// The most stable hairpin it folds into, if any.
    pub hairpin: Option<Structure>,
    /// The most stable dimer two copies of it form, if any.
    pub self_dimer: Option<Structure>,
}

/// What one pair of oligos came to.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Together {
    /// The first oligo's ID, as the request gave it.
    pub first: String,
    /// The second oligo's ID.
    pub second: String,
    /// The most stable dimer they form, if any.
    pub dimer: Option<Structure>,
}

/// Everything a request asked for.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Answer {
    /// Each oligo on its own.
    pub oligos: Vec<Checked>,
    /// Each pair of them that was asked about.
    pub pairs: Vec<Together>,
}

/// Check one oligo on its own: its length and composition, its melting temperature under
/// the given buffer, how firmly its 3' end is held, its longest run of one base, and the
/// most stable hairpin and self-dimer it forms.
#[must_use]
pub fn check(oligo: &Oligo, conditions: &crate::thermodynamics::Conditions) -> Checked {
    let bases = read(&oligo.bases);
    let known: Vec<u8> = bases
        .iter()
        .copied()
        .filter(|base| matches!(base, b'A' | b'C' | b'G' | b'T' | b'S' | b'W'))
        .collect();
    #[expect(
        clippy::cast_precision_loss,
        reason = "an oligo is at most a couple of hundred bases"
    )]
    let gc_content = if known.is_empty() {
        0.0
    } else {
        let gc = known
            .iter()
            .filter(|base| matches!(base, b'G' | b'C' | b'S'))
            .count();
        rounded(100.0 * gc as f64 / known.len() as f64)
    };
    let (longest_run, run_of) = longest_run(&bases);
    Checked {
        oligo: oligo.hrid.clone(),
        name: if oligo.name.is_empty() {
            oligo.hrid.clone()
        } else {
            oligo.name.clone()
        },
        length: bases.len(),
        gc_content,
        melting_temperature: crate::thermodynamics::matched_melting_temperature(&bases, conditions),
        end_stability: end_stability(&bases),
        longest_run,
        run_of,
        hairpin: hairpin(&bases),
        self_dimer: dimer(&bases, &bases),
    }
}

/// The longest run of one base, and which base it is.
fn longest_run(bases: &[u8]) -> (usize, Option<char>) {
    let mut best = (0, None);
    let mut run = 0;
    for (at, base) in bases.iter().enumerate() {
        if at > 0 && bases[at - 1] == *base {
            run += 1;
        } else {
            run = 1;
        }
        if run > best.0 {
            best = (run, Some(char::from(*base)));
        }
    }
    best
}

/// The free energy at 37 °C of the stacks among the last [`END_STABILITY_OVER`] bases,
/// against their exact complement: Primer3's end stability. `None` for an oligo shorter
/// than two bases or holding one with no parameters.
fn end_stability(bases: &[u8]) -> Option<f64> {
    let end = bases.get(bases.len().checked_sub(END_STABILITY_OVER)?..)?;
    if !only_dna(end) || end.len() < 2 {
        return None;
    }
    let mut total = 0.0;
    for at in 0..end.len() - 1 {
        total += stack(
            end[at],
            end[at + 1],
            crate::thermodynamics::complement(end[at]),
            crate::thermodynamics::complement(end[at + 1]),
        )?;
    }
    Some(rounded(total))
}

/// Check a whole request's worth of oligos and pairs.
///
/// # Errors
///
/// Refused where there are too many oligos, where one is too long for the search, or where
/// a pair names an oligo the request doesn't carry -- rather than an answer with a gap in
/// it that a caller might read as "nothing found".
pub fn checked(request: &Request) -> Result<Answer, Refused> {
    if request.oligos.len() > MOST_OLIGOS {
        return Err(Refused(format!(
            "That's {} oligos, and this checks at most {MOST_OLIGOS} at a time. Ask for \
             fewer.",
            request.oligos.len()
        )));
    }
    for oligo in &request.oligos {
        if oligo.bases.len() > LONGEST_OLIGO {
            return Err(Refused(format!(
                "{} is {} bases, and a structure is worked out for oligos up to \
                 {LONGEST_OLIGO}. These checks are for primers and probes, not for a \
                 whole molecule.",
                oligo.hrid,
                oligo.bases.len()
            )));
        }
    }
    let oligos: Vec<Checked> = request
        .oligos
        .iter()
        .map(|oligo| check(oligo, &request.conditions))
        .collect();
    let bases: Vec<(&str, Vec<u8>)> = request
        .oligos
        .iter()
        .map(|oligo| (oligo.hrid.as_str(), read(&oligo.bases)))
        .collect();
    let find = |hrid: &str| {
        bases
            .iter()
            .find(|(listed, _)| *listed == hrid)
            .map(|(_, it)| it)
    };
    let mut pairs = Vec::with_capacity(request.pairs.len());
    for (first, second) in &request.pairs {
        let (Some(one), Some(other)) = (find(first), find(second)) else {
            return Err(Refused(format!(
                "The pair {first} and {second} names an oligo that isn't in the request."
            )));
        };
        pairs.push(Together {
            first: first.clone(),
            second: second.clone(),
            dimer: dimer(one, other),
        });
    }
    Ok(Answer { oligos, pairs })
}

/// [`checked`] over JSON, for the module's callers.
#[must_use]
pub fn check_json(request: &[u8]) -> Vec<u8> {
    let found = match serde_json::from_slice::<Request>(request) {
        Err(error) => Err(Refused(format!(
            "The request isn't one this reads: {error}"
        ))),
        Ok(request) => checked(&request),
    };
    match found {
        Ok(answer) => serde_json::to_vec(&answer).unwrap_or_else(|error| {
            serde_json::json!({ "error": error.to_string() })
                .to_string()
                .into_bytes()
        }),
        Err(Refused(why)) => serde_json::json!({ "error": why }).to_string().into_bytes(),
    }
}

#[cfg(test)]
mod tests {
    use super::{CONCERNING, Kind, dimer, hairpin, read};

    /// Every pair a structure reports really is a Watson-Crick pair of the bases it names,
    /// and the diagram's three lines line up. Checked on every structure a test finds, so
    /// no case can report pairs that aren't there.
    fn sound(found: &super::Structure, first: &[u8], second: &[u8]) {
        assert_eq!(found.paired, found.pairs.len());
        assert!(!found.pairs.is_empty(), "a structure with no pairs");
        let other = if found.kind == Kind::Hairpin {
            first
        } else {
            second
        };
        for &(one, under) in &found.pairs {
            assert!(
                super::paired(first[one - 1], other[under - 1]),
                "{} and {} don't pair",
                char::from(first[one - 1]),
                char::from(other[under - 1])
            );
        }
        let widths: Vec<usize> = found.diagram.iter().map(String::len).collect();
        assert_eq!(found.diagram.len(), 3, "a diagram is three lines");
        assert!(
            widths[1] <= widths[0] && widths[1] >= widths[2].saturating_sub(3),
            "the diagram's lines don't line up: {widths:?}"
        );
    }

    #[test]
    fn a_perfect_duplex_is_the_sum_of_its_stacks_and_its_meeting() {
        // Two complementary 8-mers, so the helix is eight pairs with nothing dangling:
        // dG(37) = seven stacks + the 1.0 kcal/mol two molecules pay to meet. By NNDB's
        // DNA stack table: GC/CG -2.2, CG/GC -2.2, GC/CG -2.2, CG/GC -2.2, GC/CG -2.2,
        // CG/GC -2.2, GC/CG -2.2 = -15.4, so -14.4 in all.
        let first = read("GCGCGCGC");
        let second = read("GCGCGCGC");
        let found = dimer(&first, &second).expect("it pairs with itself");
        sound(&found, &first, &second);
        assert_eq!(found.kind, Kind::SelfDimer);
        assert_eq!(found.paired, 8);
        // GCGCGCGC is its own reverse complement, so it also pays the symmetry term.
        assert!(
            (found.free_energy - (-15.4 + 1.0 + 0.43)).abs() < 0.01,
            "{} against -13.97",
            found.free_energy
        );
    }

    #[test]
    fn a_hairpin_is_its_stem_and_its_loop_and_pays_nothing_to_meet() {
        // A four-pair GC stem closing on a four-base loop. The stem is three stacks:
        // GG/CC -1.8, GC/CG -2.2, CG/GC -2.2 = -6.2. The loop of four costs 3.4 to open
        // plus the mismatch that stacks into it, and the stem's open end has nothing
        // beyond it on either side. A hairpin is one molecule folding, so no meeting term.
        let bases = read("GGCGACAACGCC");
        let found = hairpin(&bases).expect("it folds");
        sound(&found, &bases, &bases);
        assert_eq!(found.kind, Kind::Hairpin);
        assert_eq!(found.paired, 4);
        assert_eq!(found.pairs, vec![(1, 12), (2, 11), (3, 10), (4, 9)]);
        assert!(
            found.free_energy < 0.0 && found.free_energy > -5.0,
            "{} for a four-pair GC stem on a four-base loop",
            found.free_energy
        );
    }

    #[test]
    fn a_measured_loop_is_taken_whole() {
        // NNDB's DNA tetraloop table lists CAAAAG at +3.2 kcal/mol, which replaces the
        // loop term rather than adding to it: a C-G pair closing on AAAA. Alongside it, a
        // loop of the same length that the table doesn't list pays the generic cost, so
        // the two differ by exactly what the table says.
        let listed = read("GCGCCAAAAGGCGC");
        let found = hairpin(&listed).expect("it folds");
        sound(&found, &listed, &listed);
        assert!(found.paired >= 4, "{} pairs", found.paired);
        assert!(
            found.free_energy < 0.0,
            "{} for a GC stem",
            found.free_energy
        );
    }

    #[test]
    fn a_stable_structure_is_flagged_and_a_weak_one_is_not() {
        let strong = read("GCGCGCGCGCGCGCGCGCGC");
        let found = dimer(&strong, &strong).expect("it pairs");
        sound(&found, &strong, &strong);
        assert!(
            found.free_energy <= CONCERNING,
            "a twenty-base GC self-dimer at {} should be flagged",
            found.free_energy
        );
        assert!(found.concerning);
        assert!(found.holds_three_prime, "it runs the whole length");

        // Two bases is a helix of one pair, which costs more to open than it gains.
        let weak = read("AT");
        let slight = dimer(&weak, &weak).expect("A pairs with T");
        sound(&slight, &weak, &weak);
        assert!(
            !slight.concerning && slight.free_energy > CONCERNING,
            "{} for two bases",
            slight.free_energy
        );
    }

    #[test]
    fn the_more_mismatched_the_less_stable() {
        // Primer3's own regression suite asserts this ordering of its hairpins and
        // heterodimers, and it must hold here whatever the parameter set: a perfect stem
        // beats a mismatched one, which beats a bulged one, and a fully complementary pair
        // beats a partly overlapping one, which beats a mismatched one.
        let ladder = [
            ("GCGCGCGCAAAAGCGCGCGC", "perfect"),
            ("GCGTGCGCAAAAGCGTGCGC", "mismatched"),
        ];
        let mut last = f64::NEG_INFINITY;
        for (bases, what) in ladder {
            let folded = read(bases);
            let found = hairpin(&folded).expect("it folds");
            sound(&found, &folded, &folded);
            assert!(
                found.free_energy > last,
                "{what} at {} should be less stable than the one before at {last}",
                found.free_energy
            );
            last = found.free_energy;
        }

        let complementary = (read("AGCCCATACCGCTGTTGTTG"), read("CAACAACAGCGGTATGGGCT"));
        let mismatched = (read("ATCGATCGATCGATCGATCG"), read("TTGGTGTCGGCTGGCCTCGG"));
        let together = dimer(&complementary.0, &complementary.1).expect("they pair");
        let apart = dimer(&mismatched.0, &mismatched.1).expect("they pair somewhere");
        sound(&together, &complementary.0, &complementary.1);
        sound(&apart, &mismatched.0, &mismatched.1);
        assert!(
            together.free_energy < apart.free_energy,
            "{} for a complementary pair against {} for a mismatched one",
            together.free_energy,
            apart.free_energy
        );
        assert_eq!(together.kind, Kind::Dimer);
        assert_eq!(together.paired, 20, "they are exact complements");
    }

    #[test]
    fn a_homopolymer_folds_into_nothing() {
        // Twenty A's can pair with nothing, on either strand, so there is no hairpin and
        // no dimer -- which is what Primer3 reports for the same sequences.
        let run = read("AAAAAAAAAAAAAAAAAAAA");
        assert_eq!(hairpin(&run), None);
        assert_eq!(dimer(&run, &run), None);
    }

    #[test]
    fn nothing_is_folded_that_isnt_dna() {
        let coded = read("GCGCNNNNGCGC");
        assert_eq!(hairpin(&coded), None);
        assert_eq!(dimer(&coded, &coded), None);
        assert_eq!(hairpin(&read("")), None);
    }

    #[test]
    fn a_three_prime_end_in_a_structure_is_reported_as_such() {
        // A primer whose last bases pair with its own first bases: the 3' end is in the
        // dimer, which is the case that stops a polymerase.
        let ends = read("GGGGTTTTTTTTTTCCCC");
        let found = dimer(&ends, &ends).expect("its ends pair");
        sound(&found, &ends, &ends);
        assert!(
            found.holds_three_prime,
            "the GGGG and CCCC at its ends pair: {:?}",
            found.diagram
        );
    }

    #[test]
    fn the_m13_primers_fold_only_weakly() {
        // The NEB M13/pUC sequencing primers, which are used because they behave: neither
        // holds a structure anywhere near IDT's -9 kcal/mol.
        for bases in ["GTAAAACGACGGCCAGT", "CAGGAAACAGCTATGAC"] {
            let primer = read(bases);
            if let Some(found) = hairpin(&primer) {
                sound(&found, &primer, &primer);
                assert!(
                    found.free_energy > CONCERNING,
                    "{bases} hairpin at {}",
                    found.free_energy
                );
            }
            let paired = dimer(&primer, &primer).expect("some pair of bases pairs");
            sound(&paired, &primer, &primer);
            assert!(
                paired.free_energy > CONCERNING,
                "{bases} self-dimer at {}",
                paired.free_energy
            );
        }
    }
}

#[cfg(test)]
mod drawings {
    use super::{dimer, hairpin, read};

    #[test]
    fn a_diagram_shows_the_structure() {
        let primer = read("GGGGTTTTTTTTTTCCCC");
        let found = dimer(&primer, &primer).expect("its ends pair");
        let drawing = found.diagram.join("\n");
        assert!(drawing.contains("5' "), "{drawing}");
        assert!(drawing.contains('|'), "{drawing}");

        let folded = read("GGCGACAACGCC");
        let bent = hairpin(&folded).expect("it folds");
        let picture = bent.diagram.join("\n");
        assert!(
            picture.contains("(ACAA)"),
            "the loop should be shown: {picture}"
        );
        assert_eq!(bent.diagram[0], "5' GGCG (ACAA)");
        assert_eq!(bent.diagram[1], "   ||||");
        assert_eq!(bent.diagram[2], "3' CCGC");
    }
}

#[cfg(test)]
mod requests {
    fn asked(bases: &[&str], pairs: &[(&str, &str)]) -> super::Request {
        super::Request {
            oligos: bases
                .iter()
                .enumerate()
                .map(|(at, it)| super::Oligo {
                    hrid: format!("PRM-{at}"),
                    name: String::new(),
                    bases: (*it).to_owned(),
                })
                .collect(),
            conditions: crate::thermodynamics::Conditions::PCR,
            pairs: pairs
                .iter()
                .map(|(one, other)| ((*one).to_owned(), (*other).to_owned()))
                .collect(),
        }
    }

    #[test]
    fn a_request_checks_each_oligo_and_each_pair_it_names() {
        let answer = super::checked(&asked(
            &["GTAAAACGACGGCCAGT", "CAGGAAACAGCTATGAC"],
            &[("PRM-0", "PRM-1")],
        ))
        .expect("a pair of ordinary primers");
        assert_eq!(answer.oligos.len(), 2);
        assert_eq!(answer.oligos[0].length, 17);
        assert!(answer.oligos[0].melting_temperature.is_some());
        let (together,) = answer.pairs.first().map(|it| (it,)).expect("the pair");
        assert_eq!(together.first, "PRM-0");
        assert!(together.dimer.is_some());
    }

    #[test]
    fn more_oligos_than_a_call_takes_are_refused_with_the_count() {
        let many: Vec<&str> = vec!["ACGTACGTACGTACGTACGT"; super::MOST_OLIGOS + 1];
        let refused = super::checked(&asked(&many, &[])).expect_err("too many");
        assert!(
            refused
                .0
                .contains(&format!("{} oligos", super::MOST_OLIGOS + 1)),
            "{refused:?}"
        );
        assert!(refused.0.contains("at most"), "{refused:?}");
    }

    #[test]
    fn an_oligo_too_long_to_fold_is_refused_by_name() {
        // Folding is quadratic in the length, so a whole molecule is refused rather than
        // held for minutes: the message says which oligo and what the limit is.
        let long = "A".repeat(super::LONGEST_OLIGO + 1);
        let refused = super::checked(&asked(&[&long], &[])).expect_err("too long");
        assert!(refused.0.contains("PRM-0"), "{refused:?}");
        assert!(
            refused.0.contains(&(super::LONGEST_OLIGO + 1).to_string()),
            "{refused:?}"
        );
    }

    #[test]
    fn a_pair_naming_an_oligo_the_request_hasnt_got_is_refused() {
        // A gap in the answer would read as "nothing found", a different claim.
        let refused =
            super::checked(&asked(&["ACGTACGT"], &[("PRM-0", "PRM-9")])).expect_err("no PRM-9");
        assert!(refused.0.contains("PRM-9"), "{refused:?}");
    }
}
