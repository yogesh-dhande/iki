//! Where restriction enzymes cut a DNA molecule, the ends they leave, and the fragments
//! and gel bands that result.
//!
//! A **cut** is where an enzyme cuts the top strand, with the overhang it leaves: the
//! boundary index `at` (the cut lies between the bases `at` and `at + 1`, counting from
//! 0) and `overhang`, negative for a 5' overhang of that many bases, positive for a 3'
//! one, zero for a blunt cut. The bottom strand is then cut at `at - overhang`.
//!
//! An **end**'s bases are the overhang's bases at that boundary, read along the top strand
//! of the molecule being cut: a 5' overhang lies after the top-strand cut (EcoRI is
//! `G^AATT_C`, so AATT), and a 3' one before it (PstI is `C_TGCA^G`, so TGCA). Read that
//! way one stretch belongs to both of the ends a cut makes, so two ends that came from
//! the same kind of cut are equal, and two ends whose overhangs differ -- PstI's TGCA
//! against KpnI's GTAC -- are not.
//!
//! A **fragment** is one piece of a digest, held as the top strand between two cuts, with
//! each end's overhang and the bases it's made of. A **band** is that same piece as a gel
//! and a map read it: its length, where it lies, and what made each of its ends.
//!
//! **Where an enzyme is recognised.** A site is matched base by base, with IUPAC
//! ambiguity codes in the site standing for the bases they may be. A non-palindromic
//! enzyme is looked for on both strands: at each place the site is tried first, and the
//! site's reverse complement only where the site itself doesn't match. A site may run
//! across a circular molecule's origin, and never across a linear molecule's ends.
//!
//! **Where the enzymes come from.** This module holds no enzyme data: the recognition
//! sites and cut offsets come in with the request. They are facts about REBASE's
//! enzymes, and REBASE's own terms are not a redistribution licence Iki can rely on
//! (ADR 0200), so each runtime passes the table it already has a licence for -- for the
//! plasmid plugin, the one in the Biopython it depends on. The algorithm is shared; the
//! data stays where its licence put it.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

/// What a linear molecule's own end is called: no enzyme made it.
pub const MOLECULE_END: &str = "End of the molecule";

/// One enzyme, as the caller's own table describes it. The field names are REBASE's, as
/// Biopython's generated table spells them.
#[derive(Debug, Clone, Deserialize)]
pub struct Enzyme {
    /// Its name, as REBASE writes it.
    pub name: String,
    /// The recognition site, in IUPAC codes. Where REBASE lists alternatives separated by
    /// `|`, only the first is recognised, as Biopython's own pattern does.
    pub site: String,
    /// How many bases of the molecule the site covers.
    pub size: usize,
    /// The first cut in the top strand, relative to the site's first base.
    #[serde(default)]
    pub fst5: Option<i64>,
    /// The first cut in the bottom strand.
    #[serde(default)]
    pub fst3: Option<i64>,
    /// The second cut in the top strand, for an enzyme that cuts twice.
    #[serde(default)]
    pub scd5: Option<i64>,
    /// The second cut in the bottom strand.
    #[serde(default)]
    pub scd3: Option<i64>,
    /// The overhang it leaves: negative for 5', positive for 3', zero for blunt.
    #[serde(default)]
    pub ovhg: Option<i64>,
    /// Whether the site reads the same on both strands, so it is looked for once.
    pub palindromic: bool,
    /// Whether it cuts each strand twice.
    pub two_cuts: bool,
}

/// One end of a fragment: the overhang's kind and length, and its bases, read on the top
/// strand. Two ends ligate when they're equal.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct End {
    /// Negative for a 5' overhang of that many bases, positive for a 3' one, zero for a
    /// blunt cut.
    pub overhang: i64,
    /// The overhang's bases, read along the top strand.
    pub bases: String,
}

impl End {
    /// A blunt end.
    #[must_use]
    pub const fn blunt() -> Self {
        Self {
            overhang: 0,
            bases: String::new(),
        }
    }

    /// How a person reads it: `blunt`, or the kind and the bases.
    #[must_use]
    pub fn describe(&self) -> String {
        if self.overhang == 0 {
            return "blunt".to_owned();
        }
        let kind = if self.overhang < 0 { '5' } else { '3' };
        format!("{kind}' {}", self.bases)
    }
}

/// One cut: where it lies, the overhang it leaves, and every enzyme that makes it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Cut {
    /// The boundary index, from 0: the cut lies between `at` and `at + 1`.
    pub at: usize,
    /// The overhang it leaves.
    pub overhang: i64,
    /// Every enzyme that breaks these bonds, named as REBASE names them, in order.
    pub made_by: Vec<String>,
}

/// A piece of a digest, as a ligation reads it: the top strand between two cuts, its two
/// ends, and which part of the plan it came from.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Fragment {
    /// The part of the plan it was cut from.
    pub part: String,
    /// Its top strand, from the cut on the left to the cut on the right.
    pub bases: String,
    /// The end on its left.
    pub left: End,
    /// The end on its right.
    pub right: End,
    /// The enzymes that made its two ends.
    pub cut_with: Vec<String>,
}

/// One fragment of a virtual digest, as a gel and a map read it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Band {
    /// Its base pairs.
    pub length: usize,
    /// Where it starts on the molecule's top strand, from 1.
    pub start: usize,
    /// Where it ends, or a lower number where it crosses a circle's origin.
    pub end: usize,
    /// What made its left end.
    pub left: String,
    /// What made its right end.
    pub right: String,
}

/// A digest that can't be worked out: an enzyme whose cut nobody knows, or two enzymes
/// that break the same bond and leave different ends.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Impossible(pub String);

/// What a site's base accepts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Accepts {
    /// Any byte at all, as the ambiguity code N does: a molecule's own N is a match.
    Anything,
    /// One of these bases.
    OneOf(u8),
}

const A: u8 = 1;
const C: u8 = 2;
const G: u8 = 4;
const T: u8 = 8;

/// What one IUPAC code in a recognition site accepts.
fn accepts(code: u8) -> Option<Accepts> {
    Some(match code {
        b'A' => Accepts::OneOf(A),
        b'C' => Accepts::OneOf(C),
        b'G' => Accepts::OneOf(G),
        b'T' => Accepts::OneOf(T),
        b'R' => Accepts::OneOf(A | G),
        b'Y' => Accepts::OneOf(C | T),
        b'S' => Accepts::OneOf(C | G),
        b'W' => Accepts::OneOf(A | T),
        b'K' => Accepts::OneOf(G | T),
        b'M' => Accepts::OneOf(A | C),
        b'B' => Accepts::OneOf(C | G | T),
        b'D' => Accepts::OneOf(A | G | T),
        b'H' => Accepts::OneOf(A | C | T),
        b'V' => Accepts::OneOf(A | C | G),
        b'N' => Accepts::Anything,
        _ => return None,
    })
}

/// Whether a base of the molecule is one this position of the site accepts.
fn accepted(position: Accepts, base: u8) -> bool {
    match position {
        // An N in a site is written as "any character" in Biopython's own pattern, so a
        // molecule's own ambiguity code there is a match, not a mismatch.
        Accepts::Anything => base != b'\n',
        Accepts::OneOf(mask) => {
            let base = match base {
                b'A' => A,
                b'C' => C,
                b'G' => G,
                b'T' => T,
                _ => 0,
            };
            base != 0 && mask & base != 0
        }
    }
}

/// The IUPAC code that pairs with this one.
const fn complement(code: u8) -> u8 {
    match code {
        b'A' => b'T',
        b'C' => b'G',
        b'G' => b'C',
        b'T' => b'A',
        b'R' => b'Y',
        b'Y' => b'R',
        b'K' => b'M',
        b'M' => b'K',
        b'B' => b'V',
        b'V' => b'B',
        b'D' => b'H',
        b'H' => b'D',
        other => other,
    }
}

/// The two patterns an enzyme is looked for by: its site, and -- for a non-palindromic
/// enzyme -- its site read from the other strand.
fn patterns(enzyme: &Enzyme) -> Result<(Vec<Accepts>, Option<Vec<Accepts>>), Impossible> {
    let site = enzyme.site.split('|').next().unwrap_or_default();
    let codes: Vec<u8> = site.bytes().map(|code| code.to_ascii_uppercase()).collect();
    let sense: Option<Vec<Accepts>> = codes.iter().map(|code| accepts(*code)).collect();
    let Some(sense) = sense else {
        return Err(Impossible(format!(
            "{}'s recognition site, {site}, isn't written in IUPAC codes.",
            enzyme.name
        )));
    };
    if sense.len() != enzyme.size {
        return Err(Impossible(format!(
            "{}'s recognition site is {} bases long, not the {} it says.",
            enzyme.name,
            sense.len(),
            enzyme.size
        )));
    }
    if enzyme.palindromic {
        return Ok((sense, None));
    }
    let antisense: Option<Vec<Accepts>> = codes
        .iter()
        .rev()
        .map(|code| accepts(complement(*code)))
        .collect();
    Ok((sense, antisense))
}

/// Whether a pattern matches the molecule from `at` (from 0), round a circle's origin.
fn matches(pattern: &[Accepts], bases: &[u8], at: usize, circular: bool) -> bool {
    let length = bases.len();
    if length == 0 {
        return false;
    }
    if !circular && at + pattern.len() > length {
        return false;
    }
    pattern.iter().enumerate().all(|(step, position)| {
        bases
            .get((at + step) % length)
            .copied()
            .is_some_and(|base| accepted(*position, base))
    })
}

/// Where an enzyme's cuts lie, once REBASE is known to say where it cuts.
#[derive(Debug, Clone, Copy)]
struct Knife {
    /// The first cut in the top strand, relative to the site's first base.
    first_top: i64,
    /// The first cut in the bottom strand.
    first_bottom: i64,
    /// The second cut in each strand, for an enzyme that cuts twice.
    second: Option<(i64, i64)>,
    /// The overhang every one of its cuts leaves.
    overhang: i64,
}

/// What REBASE says about where this enzyme cuts, or why a digest can't use it.
fn knife(enzyme: &Enzyme) -> Result<Knife, Impossible> {
    let unknown = || {
        Impossible(format!(
            "REBASE doesn't say where {} cuts, so a digest with it has no fragments: name \
             an enzyme whose cut is known, such as EcoRI, BamHI, BsaI or BsmBI.",
            enzyme.name
        ))
    };
    let second = if enzyme.two_cuts {
        Some((
            enzyme.scd5.ok_or_else(unknown)?,
            enzyme.scd3.ok_or_else(unknown)?,
        ))
    } else {
        None
    };
    Ok(Knife {
        first_top: enzyme.fst5.ok_or_else(unknown)?,
        first_bottom: enzyme.fst3.ok_or_else(unknown)?,
        second,
        overhang: enzyme.ovhg.ok_or_else(unknown)?,
    })
}

/// Where one enzyme cuts the top strand: each cut's position counted from 1, as REBASE's
/// offsets are, before the ones outside a linear molecule are dropped.
fn cuts_of(
    enzyme: &Enzyme,
    knife: Knife,
    bases: &[u8],
    circular: bool,
) -> Result<Vec<i64>, Impossible> {
    let (sense, antisense) = patterns(enzyme)?;
    let length = bases.len();
    // The places the site's first base could sit, counted from 1 as REBASE counts. A
    // circle is read right round its origin; a linear molecule only as far as its end.
    let places = if circular {
        length
    } else {
        length.saturating_sub(enzyme.size).saturating_add(1)
    };
    let mut forward: Vec<i64> = Vec::new();
    let mut reverse: Vec<i64> = Vec::new();
    for place in 0..places {
        let at = i64::try_from(place).map_err(|_| too_long())? + 1;
        if matches(&sense, bases, place, circular) {
            forward.push(at + knife.first_top);
            if let Some((top, _)) = knife.second {
                forward.push(at + top);
            }
        } else if antisense
            .as_ref()
            .is_some_and(|pattern| matches(pattern, bases, place, circular))
        {
            reverse.push(at - knife.first_bottom);
            if let Some((_, bottom)) = knife.second {
                reverse.push(at - bottom);
            }
        }
    }
    if enzyme.palindromic {
        // A palindromic enzyme is found once per place, so its cuts come in the order the
        // offsets give them.
        return Ok(forward);
    }
    forward.extend(reverse);
    forward.sort_unstable();
    Ok(forward)
}

fn too_long() -> Impossible {
    Impossible("The molecule is longer than a digest is worked out for.".to_owned())
}

/// The cuts that lie on the molecule: a linear molecule's are those with bases on both
/// sides of them on both strands, and a circle's are brought back inside its origin.
fn within(cuts: Vec<i64>, overhang: i64, length: usize, circular: bool) -> Vec<i64> {
    let span = i64::try_from(length).unwrap_or(i64::MAX);
    if circular {
        return cuts
            .into_iter()
            .map(|cut| {
                if cut < 1 {
                    cut + span
                } else if cut > span {
                    cut - span
                } else {
                    cut
                }
            })
            .collect();
    }
    cuts.into_iter()
        .filter(|cut| {
            let other = cut - overhang;
            1 < *cut && *cut <= span && 1 < other && other <= span
        })
        .collect()
}

/// Where `enzymes` cut `bases`: each cut's boundary index (from 0), the overhang it
/// leaves, and every enzyme that makes that cut, in order along the molecule.
///
/// A cut is one place where the strands are broken, not one enzyme's work: isoschizomers
/// such as SacI and SstI, or BsaI and Eco31I, recognise the same site and break the same
/// bonds, so a digest with both gives the fragments either one gives alone, with both
/// names recorded against each boundary.
///
/// # Errors
///
/// Where an enzyme's cut isn't known, or two enzymes break the same bond and leave
/// different ends, so what they make together isn't one molecule.
pub fn cuts(bases: &[u8], circular: bool, enzymes: &[Enzyme]) -> Result<Vec<Cut>, Impossible> {
    let mut found: BTreeMap<usize, (i64, Vec<String>)> = BTreeMap::new();
    for enzyme in enzymes {
        let knife = knife(enzyme)?;
        let overhang = knife.overhang;
        let placed = cuts_of(enzyme, knife, bases, circular)?;
        for cut in within(placed, overhang, bases.len(), circular) {
            let at = usize::try_from(cut - 1).map_err(|_| too_long())?;
            let known = found.entry(at).or_insert_with(|| (overhang, Vec::new()));
            if known.0 != overhang {
                let first = known.1.first().map_or("", String::as_str);
                return Err(Impossible(format!(
                    "{} and {first} both cut the top strand after base {at}, but leave \
                     different overhangs ({overhang} and {}), so what they make together \
                     isn't one molecule: digest with one of them.",
                    enzyme.name, known.0
                )));
            }
            if !known.1.contains(&enzyme.name) {
                known.1.push(enzyme.name.clone());
            }
        }
    }
    Ok(found
        .into_iter()
        .map(|(at, (overhang, mut made_by))| {
            made_by.sort();
            Cut {
                at,
                overhang,
                made_by,
            }
        })
        .collect())
}

/// The end a cut at boundary `at` leaves, read on the top strand.
///
/// A 5' overhang's bases are the `overhang` bases just after the top-strand cut, a 3'
/// one's the bases just before it. Either stretch can cross a circular molecule's origin,
/// so both are read from the doubled sequence.
#[must_use]
pub fn end_at(at: usize, overhang: i64, bases: &[u8], circular: bool) -> End {
    if overhang == 0 || bases.is_empty() {
        return End::blunt();
    }
    let length = bases.len();
    let span = i64::try_from(length).unwrap_or(i64::MAX);
    let from = i64::try_from(at).unwrap_or(i64::MAX);
    let start = if overhang < 0 { from } else { from - overhang };
    let start = if circular {
        start.rem_euclid(span)
    } else {
        start.max(0)
    };
    let start = usize::try_from(start).unwrap_or(0);
    let wanted = usize::try_from(overhang.abs()).unwrap_or(0);
    let doubled: Vec<u8> = bases.iter().chain(bases.iter()).copied().collect();
    let taken = doubled
        .get(start..(start + wanted).min(doubled.len()))
        .unwrap_or_default();
    End {
        overhang,
        bases: String::from_utf8_lossy(taken).into_owned(),
    }
}

/// `bases` cut by `enzymes`: each fragment, with its ends. An uncut circular molecule
/// gives nothing (it can't go into a ligation); an uncut linear one gives itself, blunt at
/// both ends.
///
/// # Errors
///
/// As [`cuts`].
pub fn digest(
    part: &str,
    bases: &[u8],
    circular: bool,
    enzymes: &[Enzyme],
) -> Result<Vec<Fragment>, Impossible> {
    let made = cuts(bases, circular, enzymes)?;
    let length = bases.len();
    if made.is_empty() {
        if circular {
            return Ok(Vec::new());
        }
        return Ok(vec![Fragment {
            part: part.to_owned(),
            bases: String::from_utf8_lossy(bases).into_owned(),
            left: End::blunt(),
            right: End::blunt(),
            cut_with: Vec::new(),
        }]);
    }
    let doubled: Vec<u8> = bases.iter().chain(bases.iter()).copied().collect();
    let mut out: Vec<Fragment> = Vec::new();
    if circular {
        for (index, left) in made.iter().enumerate() {
            let right = made.get((index + 1) % made.len()).unwrap_or(left);
            let stop = if right.at > left.at {
                right.at
            } else {
                right.at + length
            };
            out.push(Fragment {
                part: part.to_owned(),
                bases: String::from_utf8_lossy(
                    doubled
                        .get(left.at..stop.min(doubled.len()))
                        .unwrap_or_default(),
                )
                .into_owned(),
                left: end_at(left.at, left.overhang, bases, circular),
                right: end_at(right.at, right.overhang, bases, circular),
                cut_with: left
                    .made_by
                    .iter()
                    .chain(right.made_by.iter())
                    .cloned()
                    .collect(),
            });
        }
    } else {
        let mut edges: Vec<usize> = vec![0];
        edges.extend(made.iter().map(|cut| cut.at));
        edges.push(length);
        for pair in edges.windows(2) {
            let (Some(&left), Some(&right)) = (pair.first(), pair.get(1)) else {
                continue;
            };
            let end_of = |at: usize| {
                made.iter()
                    .find(|cut| cut.at == at)
                    .map_or_else(End::blunt, |cut| {
                        end_at(cut.at, cut.overhang, bases, circular)
                    })
            };
            let names_at = |at: usize| {
                made.iter()
                    .find(|cut| cut.at == at)
                    .map(|cut| cut.made_by.clone())
                    .unwrap_or_default()
            };
            let mut cut_with = names_at(left);
            cut_with.extend(names_at(right));
            out.push(Fragment {
                part: part.to_owned(),
                bases: String::from_utf8_lossy(
                    bases.get(left..right.min(length)).unwrap_or_default(),
                )
                .into_owned(),
                left: end_of(left),
                right: end_of(right),
                cut_with,
            });
        }
    }
    out.retain(|fragment| !fragment.bases.is_empty());
    Ok(out)
}

/// `bases` cut by `enzymes`, each fragment as a band, largest first, as a gel reads from
/// its wells and as a marker's sizes are listed; bands of one length in the order they lie
/// along the molecule. An uncut circle gives none: it stays a circle. An uncut linear
/// molecule is one band, itself.
///
/// # Errors
///
/// As [`cuts`].
pub fn bands(bases: &[u8], circular: bool, enzymes: &[Enzyme]) -> Result<Vec<Band>, Impossible> {
    let made = cuts(bases, circular, enzymes)?;
    let length = bases.len();
    if made.is_empty() && circular {
        return Ok(Vec::new());
    }
    let named = |cut: &Cut| {
        format!(
            "{}, {}",
            cut.made_by.join(", "),
            end_at(cut.at, cut.overhang, bases, circular).describe()
        )
    };
    let ends: Vec<(usize, String)> = made.iter().map(|cut| (cut.at, named(cut))).collect();
    let mut out: Vec<Band> = Vec::new();
    if circular {
        for (index, (left, said)) in ends.iter().enumerate() {
            let Some((right, then)) = ends.get((index + 1) % ends.len()) else {
                continue;
            };
            let size = if right > left {
                right - left
            } else {
                right + length - left
            };
            out.push(Band {
                length: size,
                start: left % length.max(1) + 1,
                end: if *right > 0 { *right } else { length },
                left: said.clone(),
                right: then.clone(),
            });
        }
    } else {
        let mut edges: Vec<(usize, String)> = vec![(0, MOLECULE_END.to_owned())];
        edges.extend(ends);
        edges.push((length, MOLECULE_END.to_owned()));
        for pair in edges.windows(2) {
            let (Some((left, said)), Some((right, then))) = (pair.first(), pair.get(1)) else {
                continue;
            };
            if right > left {
                out.push(Band {
                    length: right - left,
                    start: left + 1,
                    end: *right,
                    left: said.clone(),
                    right: then.clone(),
                });
            }
        }
    }
    out.sort_by_key(|band| (std::cmp::Reverse(band.length), band.start));
    Ok(out)
}

/// A request to cut one molecule.
#[derive(Debug, Deserialize)]
pub struct DigestRequest {
    /// What the molecule is called in the plan.
    #[serde(default)]
    pub part: String,
    /// Its bases.
    pub bases: String,
    /// Whether it is a circle.
    #[serde(default)]
    pub circular: bool,
    /// The enzymes to cut it with, each with its own site and offsets.
    pub enzymes: Vec<Enzyme>,
    /// Whether to work out the gel bands as well as the fragments.
    #[serde(default)]
    pub want_bands: bool,
}

/// What the digest leaves.
#[derive(Debug, Serialize)]
pub struct DigestAnswer {
    /// Every cut, in order along the molecule.
    pub cuts: Vec<Cut>,
    /// Every fragment, in order along the molecule.
    pub fragments: Vec<Fragment>,
    /// Every band, largest first, where the request asked for them.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bands: Option<Vec<Band>>,
}

/// Cuts the request's molecule.
///
/// # Errors
///
/// As [`cuts`].
pub fn cut(request: &DigestRequest) -> Result<DigestAnswer, Impossible> {
    let bases: Vec<u8> = request
        .bases
        .bytes()
        .map(|base| base.to_ascii_uppercase())
        .collect();
    Ok(DigestAnswer {
        cuts: cuts(&bases, request.circular, &request.enzymes)?,
        fragments: digest(&request.part, &bases, request.circular, &request.enzymes)?,
        bands: if request.want_bands {
            Some(bands(&bases, request.circular, &request.enzymes)?)
        } else {
            None
        },
    })
}

/// The JSON answer to a JSON request, for the module's export.
#[must_use]
pub fn cut_json(request: &[u8]) -> Vec<u8> {
    let answer = match serde_json::from_slice::<DigestRequest>(request) {
        Err(error) => Err(Impossible(format!(
            "The request isn't one this reads: {error}"
        ))),
        Ok(request) => cut(&request),
    };
    match answer {
        Ok(found) => serde_json::to_vec(&found).unwrap_or_else(|error| {
            serde_json::json!({ "error": error.to_string() })
                .to_string()
                .into_bytes()
        }),
        Err(Impossible(why)) => serde_json::json!({ "error": why }).to_string().into_bytes(),
    }
}

#[cfg(test)]
mod tests {
    use super::{Enzyme, Impossible, bands, cuts, digest};

    /// EcoRI, as REBASE describes it: `G^AATT_C`, a 5' AATT overhang.
    fn eco_ri() -> Enzyme {
        Enzyme {
            name: "EcoRI".to_owned(),
            site: "GAATTC".to_owned(),
            size: 6,
            fst5: Some(1),
            fst3: Some(-1),
            scd5: None,
            scd3: None,
            ovhg: Some(-4),
            palindromic: true,
            two_cuts: false,
        }
    }

    /// PstI: `C_TGCA^G`, a 3' TGCA overhang.
    fn pst_i() -> Enzyme {
        Enzyme {
            name: "PstI".to_owned(),
            site: "CTGCAG".to_owned(),
            size: 6,
            fst5: Some(5),
            fst3: Some(-5),
            scd5: None,
            scd3: None,
            ovhg: Some(4),
            palindromic: true,
            two_cuts: false,
        }
    }

    /// BsaI: GGTCTC(1/5), non-palindromic, so it is looked for on both strands.
    fn bsa_i() -> Enzyme {
        Enzyme {
            name: "BsaI".to_owned(),
            site: "GGTCTC".to_owned(),
            size: 6,
            fst5: Some(7),
            fst3: Some(5),
            scd5: None,
            scd3: None,
            ovhg: Some(-4),
            palindromic: false,
            two_cuts: false,
        }
    }

    #[test]
    fn a_five_prime_overhang_reads_after_the_cut_and_a_three_prime_one_before_it() {
        let bases = b"TTTTTGAATTCTTTTTCTGCAGTTTTT";
        let found = cuts(bases, false, &[eco_ri(), pst_i()]).expect("a digest");
        assert_eq!(
            found
                .iter()
                .map(|cut| {
                    let end = super::end_at(cut.at, cut.overhang, bases, false);
                    (cut.at, cut.overhang, end.bases, cut.made_by.clone())
                })
                .collect::<Vec<_>>(),
            [
                (6, -4, "AATT".to_owned(), vec!["EcoRI".to_owned()]),
                (21, 4, "TGCA".to_owned(), vec!["PstI".to_owned()]),
            ]
        );
    }

    #[test]
    fn a_non_palindromic_enzyme_cuts_on_both_strands() {
        // The site on the top strand, and its reverse complement further along.
        let bases = b"TTTTTGGTCTCAAAAATTTTTGAGACCTTTTT";
        let found = cuts(bases, false, &[bsa_i()]).expect("a digest");
        assert_eq!(found.iter().map(|cut| cut.at).collect::<Vec<_>>(), [12, 16]);
    }

    #[test]
    fn an_uncut_molecule_is_itself_or_nothing_by_its_topology() {
        let bases = b"TTTTTTTTTTTTTTTTTTTT";
        let linear = digest("backbone", bases, false, &[eco_ri()]).expect("a digest");
        assert_eq!(linear.len(), 1);
        assert_eq!(linear.first().map(|one| one.bases.len()), Some(bases.len()));
        let circle = digest("backbone", bases, true, &[eco_ri()]).expect("a digest");
        assert!(circle.is_empty(), "an uncut circle stays a circle");
    }

    #[test]
    fn a_circle_cut_once_gives_one_fragment_of_its_whole_length() {
        let bases = b"TTTTTGAATTCTTTTTTTTTT";
        let fragments = digest("backbone", bases, true, &[eco_ri()]).expect("a digest");
        assert_eq!(fragments.len(), 1);
        let one = fragments.first().expect("one fragment");
        assert_eq!(one.bases.len(), bases.len());
        assert_eq!(one.left, one.right, "both ends are the same cut");
    }

    #[test]
    fn bands_come_largest_first_with_what_made_each_end() {
        let bases = b"TTTTTGAATTCTTTTTCTGCAGTTTTT";
        let found = bands(bases, false, &[eco_ri(), pst_i()]).expect("a digest");
        assert_eq!(
            found
                .iter()
                .map(|band| (band.length, band.start, band.end))
                .collect::<Vec<_>>(),
            [(15, 7, 21), (6, 1, 6), (6, 22, 27)]
        );
        assert_eq!(
            found
                .first()
                .map(|band| (band.left.clone(), band.right.clone())),
            Some(("EcoRI, 5' AATT".to_owned(), "PstI, 3' TGCA".to_owned()))
        );
    }

    #[test]
    fn an_enzyme_whose_cut_rebase_doesnt_know_is_refused() {
        let unknown = Enzyme {
            name: "AbaB8342IV".to_owned(),
            site: "CCCGAG".to_owned(),
            size: 6,
            fst5: None,
            fst3: None,
            scd5: None,
            scd3: None,
            ovhg: None,
            palindromic: false,
            two_cuts: false,
        };
        let Err(Impossible(why)) = cuts(b"TTTTTCCCGAGTTTTT", false, &[unknown]) else {
            unreachable!("an enzyme with no known cut leaves no fragments");
        };
        assert!(
            why.starts_with("REBASE doesn't say where AbaB8342IV cuts"),
            "{why}"
        );
    }

    #[test]
    fn two_enzymes_that_break_one_bond_differently_are_refused() {
        let mut odd = eco_ri();
        odd.name = "NotEcoRI".to_owned();
        odd.ovhg = Some(-2);
        let Err(Impossible(why)) = cuts(b"TTTTTGAATTCTTTTT", false, &[eco_ri(), odd]) else {
            unreachable!("two overhangs at one boundary aren't one molecule");
        };
        assert!(why.contains("different overhangs"), "{why}");
    }
}
