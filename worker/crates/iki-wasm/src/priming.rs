//! Where a project's primers bind a DNA sequence, and the product a pair of them gives.
//!
//! **Placing a primer.** A polymerase extends from a primer's 3' end, so that end has to
//! pair. A primer is placed by its 3'-most [`ANCHOR`] bases, on either strand, without
//! gaps, and binds where they pair well enough. "Well enough" is a penalty: a mismatch
//! among the 3'-most [`THREE_PRIME`] bases costs [`NEAR_THREE_PRIME`], one further away
//! costs [`ELSEWHERE`], and a site is kept where the total is at most the run's allowance
//! ([`MOST_PENALTY`] by default). So by default a primer binds where its anchor matches
//! exactly, or with one or two mismatches away from its 3' end, and never with one in its
//! last five bases. That weighting is what PCR does: a mismatch at or near the 3'
//! terminus cuts extension sharply, the more the nearer the end (Kwok et al., *Nucleic
//! Acids Res* 18:999-1005, 1990, <https://doi.org/10.1093/nar/18.4.999>; Stadhouders et
//! al., *J Mol Diagn* 12:109-117, 2010, <https://doi.org/10.2353/jmoldx.2010.090035>),
//! while mismatches toward the 5' end, and a 5' tail most of all, are carried into the
//! product without stopping it. SnapGene places a primer the same way: by a stretch of
//! its 3' end and how well that pairs.
//!
//! Beyond the anchor the primer goes on pairing toward its 5' end while its bases match;
//! the rest is its 5' tail, which the template hasn't got and the product has. IUPAC
//! codes pair with what they may be: a primer's N pairs with any base, its R with A or G.
//!
//! A site may run across a circle's origin, and never across a linear molecule's ends
//! (ADR 0168): beyond an end there is nothing to pair with.
//!
//! **Melting temperature** is [`crate::thermodynamics`] under [`Conditions::PRIMER`]. A
//! site's Tm is of its annealed bases against the template's own bases, so a mismatch
//! lowers it; where no published parameter covers a pair of neighbours, the Tm is left
//! out rather than guessed.
//!
//! **A product** comes of two sites facing each other: one on the forward strand
//! extending rightwards and one on the reverse extending leftwards, the first's 3' end at
//! or before the second's. Its bases are the forward primer whole, the template between
//! the two 3' ends, and the reverse primer's reverse complement whole -- so both 5' tails
//! are in it, and so is every base a primer mismatched, since the product is copied from
//! the primer there. On a circle each facing pair gives the product that runs from one to
//! the other, across the origin where that is the way round; on a linear molecule only a
//! pair with the forward site to the left gives one.

use serde::{Deserialize, Serialize};

use crate::thermodynamics::{Conditions, melting_temperature};

/// The 3'-most bases that place a primer.
pub const ANCHOR: usize = 15;
/// The fewest bases a primer is placed by: a shorter one binds too many places to say
/// anything about.
pub const SHORTEST: usize = 8;
/// Where a mismatch weighs more: within this many bases of the primer's 3' end.
pub const THREE_PRIME: usize = 5;
/// What a mismatch weighs there.
pub const NEAR_THREE_PRIME: u32 = 3;
/// What a mismatch weighs further from the 3' end.
pub const ELSEWHERE: u32 = 1;
/// What a site's mismatches may weigh together by default.
pub const MOST_PENALTY: u32 = 2;
/// A polymerase's reach by default: a pair of sites further apart than this amplifies
/// nothing in the tube, so no product is worked out for it.
pub const LONGEST_PRODUCT: usize = 20_000;
/// What a run keeps at most: sites of all the primers, and products of one reaction.
pub const MOST_SITES: usize = 500;
/// The most products one reaction is worked out to.
pub const MOST_PRODUCTS: usize = 20;
/// How the melting temperatures are arrived at, for the record that holds them.
pub const CONDITIONS_TEXT: &str = "SantaLucia 1998 nearest-neighbour (Biopython Tm_NN, DNA_NN3), 50 mM Na+, no Mg2+ or \
     dNTPs, 25 nM of each strand";

impl Strand {
    /// What a record calls it.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Forward => "forward",
            Self::Reverse => "reverse",
        }
    }
}

/// Which bases an IUPAC code may be, as a mask over A, C, G and T.
const fn iupac(code: u8) -> u8 {
    const A: u8 = 1;
    const C: u8 = 2;
    const G: u8 = 4;
    const T: u8 = 8;
    match code {
        b'A' => A,
        b'C' => C,
        b'G' => G,
        b'T' | b'U' => T,
        b'R' => A | G,
        b'Y' => C | T,
        b'S' => C | G,
        b'W' => A | T,
        b'K' => G | T,
        b'M' => A | C,
        b'B' => C | G | T,
        b'D' => A | G | T,
        b'H' => A | C | T,
        b'V' => A | C | G,
        b'N' => A | C | G | T,
        _ => 0,
    }
}

/// Whether a primer's base may pair with the template's: every base the template's code
/// may be is one the primer's may be. Nothing else says the primer pairs there.
#[must_use]
pub const fn pairs(primer: u8, template: u8) -> bool {
    let may_be = iupac(template);
    may_be != 0 && may_be & !iupac(primer) == 0
}

/// The base that pairs with an IUPAC code, keeping the ambiguity.
#[must_use]
pub const fn complement(base: u8) -> u8 {
    match base {
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
        b'S' => b'S',
        b'W' => b'W',
        b'N' => b'N',
        other => other,
    }
}

/// A stretch of DNA read from its other strand.
#[must_use]
pub fn reverse_complement(bases: &[u8]) -> Vec<u8> {
    bases.iter().rev().map(|base| complement(*base)).collect()
}

/// One primer to look for.
#[derive(Debug, Clone, Deserialize)]
pub struct Primer {
    /// The record it is.
    pub hrid: String,
    /// What to call it.
    #[serde(default)]
    pub name: String,
    /// Its bases, 5' to 3'.
    pub bases: String,
}

/// Which strand of the template a primer reads along.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Strand {
    /// Its bases are the forward strand's, so it reads rightwards.
    Forward,
    /// Its bases are the reverse strand's, so it reads leftwards.
    Reverse,
}

/// One mismatch in a site.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Mismatch {
    /// How far the base is from the primer's 3' end, 1 being the 3'-most.
    pub from_three_prime: usize,
    /// The primer's base there, read 5' to 3' along the primer.
    pub primer: char,
    /// The template's base opposite it, on the primer's own strand's partner.
    pub template: char,
}

/// Where one primer binds. `start` and `end` (from 1) are the lowest and highest template
/// bases its annealed part pairs with, an `end` before `start` crossing a circle's origin;
/// `three_prime` is the base its 3' end pairs with, from which it extends -- rightwards on
/// the forward strand, leftwards on the reverse.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Site {
    /// The primer this is a site of.
    pub primer: String,
    /// What to call it.
    pub name: String,
    /// Which strand it reads along.
    pub strand: Strand,
    /// The lowest template base its annealed part pairs with, from 1.
    pub start: usize,
    /// The highest, or a lower number where the site crosses a circle's origin.
    pub end: usize,
    /// The template base its 3' end pairs with, from 1.
    pub three_prime: usize,
    /// How many of the primer's bases pair with the template.
    pub annealed: usize,
    /// Its 5' bases that don't, and go into a product.
    pub tail: usize,
    /// Each mismatch, from the 5' end of the annealed part toward the 3'.
    #[serde(default)]
    pub mismatches: Vec<Mismatch>,
    /// What those mismatches weigh together.
    pub penalty: u32,
    /// Degrees Celsius, where every pair of neighbours has a published parameter.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub melting_temperature: Option<f64>,
}

/// What a pair of facing sites amplifies.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Product {
    /// Its bases, 5' to 3' on the forward primer's strand.
    pub bases: String,
    /// Which of the sites given is its forward one.
    pub forward: usize,
    /// Which is its reverse one.
    pub reverse: usize,
    /// The first template base copied, from 1.
    pub template_start: usize,
    /// The last, or a lower number where the product crosses a circle's origin.
    pub template_end: usize,
}

/// Why the primers can't be placed, or the product worked out, as asked.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refused(pub String);

/// What a mismatch weighs, `away` bases from the primer's 3' end (1 is its last).
const fn weight(away: usize) -> u32 {
    if away <= THREE_PRIME {
        NEAR_THREE_PRIME
    } else {
        ELSEWHERE
    }
}

/// The template's base at `at` (from 0), round a circle, or `None` past a linear end.
fn template_at(bases: &[u8], at: isize, circular: bool) -> Option<u8> {
    let length = bases.len().cast_signed();
    if at >= 0 && at < length {
        return bases.get(usize::try_from(at).ok()?).copied();
    }
    if circular {
        let wrapped = usize::try_from(at.rem_euclid(length)).ok()?;
        return bases.get(wrapped).copied();
    }
    None
}

/// A template's bases as the search reads them: upper case, with U read as T.
#[must_use]
pub fn normalise(bases: &str) -> Vec<u8> {
    bases
        .bytes()
        .map(|base| match base.to_ascii_uppercase() {
            b'U' => b'T',
            other => other,
        })
        .collect()
}

/// Every site the primers bind, on both strands, in order along the template.
///
/// A primer is looked for on the forward strand (its bases are the forward strand's, so
/// it reads rightwards) and on the reverse (its bases are the reverse strand's, reading
/// leftwards). A primer shorter than [`SHORTEST`] is skipped.
///
/// # Errors
///
/// Where the template has no bases, or the primers bind more than [`MOST_SITES`] places.
pub fn find_sites(
    template: &str,
    circular: bool,
    primers: &[Primer],
    anchor: usize,
    most_penalty: u32,
) -> Result<Vec<Site>, Refused> {
    let template = normalise(template);
    let length = template.len();
    if length == 0 {
        return Err(Refused("The template has no bases.".to_owned()));
    }
    let mut found: Vec<Site> = Vec::new();
    for primer in primers {
        let bases = normalise(&primer.bases);
        if bases.len() < SHORTEST {
            continue;
        }
        for strand in [Strand::Forward, Strand::Reverse] {
            // The primer's bases along the forward strand, 5' to 3' of that strand: a
            // reverse-strand primer reads as its reverse complement there.
            let along = if strand == Strand::Forward {
                bases.clone()
            } else {
                reverse_complement(&bases)
            };
            // Every place the primer's 3'-most base could sit (from 0). On a linear
            // molecule a site whose anchor would reach past an end is no site, which
            // `site_at` finds when the template runs out.
            for offset in 0..length {
                if let Some(site) = site_at(
                    &template,
                    circular,
                    primer,
                    &bases,
                    &along,
                    offset,
                    anchor,
                    most_penalty,
                    strand,
                ) {
                    found.push(site);
                }
                if found.len() > MOST_SITES {
                    return Err(Refused(format!(
                        "The primers bind more than {MOST_SITES} places on a \
                         {} bp template. Allow fewer mismatches, or a longer 3' match.",
                        thousands(length)
                    )));
                }
            }
        }
    }
    // In order along the template, then by strand -- forward before reverse, as the
    // names sort -- then by the primer.
    found.sort_by(|one, other| {
        (one.start, one.end, one.strand.name(), &one.primer).cmp(&(
            other.start,
            other.end,
            other.strand.name(),
            &other.primer,
        ))
    });
    Ok(found)
}

/// The site where the primer's 3'-most base sits at `offset` (from 0), or `None` if its
/// 3' end doesn't pair there well enough.
#[expect(
    clippy::too_many_arguments,
    reason = "one call site; splitting the template, the primer and the run's allowances \
              into structs would hide which of them vary down the loop"
)]
fn site_at(
    template: &[u8],
    circular: bool,
    primer: &Primer,
    bases: &[u8],
    along: &[u8],
    offset: usize,
    anchor: usize,
    most_penalty: u32,
    strand: Strand,
) -> Option<Site> {
    let forward = strand == Strand::Forward;
    let anchored = anchor.max(1).min(bases.len());
    let mut penalty = 0;
    let mut mismatches: Vec<Mismatch> = Vec::new();
    let offset_at = isize::try_from(offset).ok()?;
    // From the 3'-most base of the primer back along it: `away` is 1 for its last base.
    let mut away = along.len() + 1;
    for step in 1..=along.len() {
        let step_away = isize::try_from(step - 1).ok()?;
        let at = if forward {
            offset_at - step_away
        } else {
            offset_at + step_away
        };
        // Where that base of the primer lies in `along`: from its end, or from its start.
        let within = if forward {
            along.len().checked_sub(step)?
        } else {
            step - 1
        };
        let mine = *along.get(within)?;
        let Some(there) = template_at(template, at, circular) else {
            // A linear molecule's end: the anchor must pair, and beyond it the primer's
            // remaining bases are a 5' tail hanging off the end, which a product keeps.
            if step <= anchored {
                return None;
            }
            away = step;
            break;
        };
        if pairs(mine, there) {
            continue;
        }
        if step > anchored {
            // The pairing stops here: the rest of the primer is its 5' tail.
            away = step;
            break;
        }
        penalty += weight(step);
        if penalty > most_penalty {
            return None;
        }
        // The primer's own base, 5' to 3' on its own strand, and the template's opposite.
        let own = *bases.get(bases.len().checked_sub(step)?)?;
        let theirs = if forward { there } else { complement(there) };
        mismatches.push(Mismatch {
            from_three_prime: step,
            primer: char::from(own),
            template: char::from(theirs),
        });
    }
    let annealed = away - 1;
    if annealed < anchored {
        return None;
    }
    let length = template.len();
    let signed_length = isize::try_from(length).ok()?;
    let annealed_at = isize::try_from(annealed).ok()?;
    // Where the annealed bases lie on the template, as the map counts them (from 1).
    let (first, last) = if forward {
        (offset_at - annealed_at + 1, offset_at)
    } else {
        (offset_at, offset_at + annealed_at - 1)
    };
    let annealed_bases = bases.get(bases.len().checked_sub(annealed)?..)?;
    let opposite: Vec<u8> = (first..=last)
        .filter_map(|at| template.get(usize::try_from(at.rem_euclid(signed_length)).ok()?))
        .copied()
        .collect();
    // The template's bases under the primer, read 5' to 3' on the primer's own strand;
    // the temperature is of the primer against the strand those bases pair with, which
    // is that stretch complemented, base for base and so read 3' to 5'.
    let under = if forward {
        opposite
    } else {
        reverse_complement(&opposite)
    };
    let against: Vec<u8> = under.iter().map(|base| complement(*base)).collect();
    mismatches.reverse();
    Some(Site {
        primer: primer.hrid.clone(),
        name: if primer.name.is_empty() {
            primer.hrid.clone()
        } else {
            primer.name.clone()
        },
        strand,
        start: usize::try_from(first.rem_euclid(signed_length)).ok()? + 1,
        end: usize::try_from(last.rem_euclid(signed_length)).ok()? + 1,
        three_prime: offset % length + 1,
        annealed,
        tail: bases.len() - annealed,
        mismatches,
        penalty,
        melting_temperature: melting_temperature(annealed_bases, &against, &Conditions::PRIMER),
    })
}

/// The bases from `from_at` to `to_at` (both from 1, both counted), going along the
/// forward strand, round a circle's origin where that is the way. `None` where a linear
/// molecule's order rules it out.
fn distance(from_at: usize, to_at: usize, length: usize, circular: bool) -> Option<usize> {
    if to_at >= from_at {
        return Some(to_at - from_at + 1);
    }
    if circular {
        Some(length - from_at + 1 + to_at)
    } else {
        None
    }
}

/// A base `by` away from `from_at` (both counted from 1), round the molecule's origin.
fn wrapped(from_at: usize, by: isize, length: usize) -> Option<usize> {
    let length = isize::try_from(length).ok()?;
    if length == 0 {
        return None;
    }
    usize::try_from((isize::try_from(from_at).ok()? + by).rem_euclid(length))
        .ok()?
        .checked_add(1)
}

/// Every product the sites give, shortest first.
///
/// A forward site extends rightwards from its 3' end and a reverse site leftwards from
/// its, so a pair makes a product when the forward site's 3' end is at or before the
/// reverse site's, going along the forward strand. The product is the forward primer
/// whole, the template between the two 3' ends, and the reverse primer's reverse
/// complement whole: both 5' tails are in it, and so is every base either primer
/// mismatched, since there the product is copied from the primer.
///
/// `primers` holds each site's bases, by the same index as `sites`.
///
/// # Errors
///
/// Where the sites make more than [`MOST_PRODUCTS`] products.
pub fn products(
    template: &str,
    circular: bool,
    sites: &[Site],
    primers: &[Vec<u8>],
    longest: usize,
) -> Result<Vec<Product>, Refused> {
    let template = normalise(template);
    let length = template.len();
    let mut made: Vec<Product> = Vec::new();
    for (ahead, forward) in sites.iter().enumerate() {
        if forward.strand != Strand::Forward {
            continue;
        }
        for (back, reverse) in sites.iter().enumerate() {
            if reverse.strand != Strand::Reverse {
                continue;
            }
            // The template between the two primers' 3' ends, the ends themselves left to
            // the primers: the product has the primer's own bases there, mismatches and
            // all.
            let Some(between) =
                distance(forward.three_prime, reverse.three_prime, length, circular)
            else {
                continue;
            };
            if between < 2 || length == 0 {
                continue;
            }
            let (Some(ahead_bases), Some(back_bases)) = (primers.get(ahead), primers.get(back))
            else {
                continue;
            };
            // The base after the forward 3' end.
            let middle_from = forward.three_prime % length;
            let mut bases = ahead_bases.clone();
            bases.extend(
                (0..between - 2).filter_map(|step| template.get((middle_from + step) % length)),
            );
            bases.extend(reverse_complement(back_bases));
            if bases.len() > longest {
                continue;
            }
            let Some(start) = wrapped(forward.three_prime, -forward.annealed.cast_signed(), length)
            else {
                continue;
            };
            let Some(end) = wrapped(
                reverse.three_prime,
                reverse.annealed.cast_signed() - 2,
                length,
            ) else {
                continue;
            };
            made.push(Product {
                template_start: start,
                template_end: end,
                bases: String::from_utf8_lossy(&bases).into_owned(),
                forward: ahead,
                reverse: back,
            });
            if made.len() > MOST_PRODUCTS {
                return Err(Refused(format!(
                    "The primers make more than {MOST_PRODUCTS} products on this template. \
                     Allow fewer mismatches, or a longer 3' match."
                )));
            }
        }
    }
    made.sort_by_key(|one| (one.bases.len(), one.template_start));
    Ok(made)
}

/// A count with thousands separated by commas, as the messages write lengths.
fn thousands(count: usize) -> String {
    let digits = count.to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (seen, digit) in digits.chars().enumerate() {
        if seen > 0 && (digits.len() - seen).is_multiple_of(3) {
            out.push(',');
        }
        out.push(digit);
    }
    out
}

/// A request to place primers on a template.
#[derive(Debug, Deserialize)]
pub struct SitesRequest {
    /// The template's bases.
    pub template: String,
    /// Whether it is a circle.
    #[serde(default)]
    pub circular: bool,
    /// The primers to place.
    pub primers: Vec<Primer>,
    /// How many of a primer's 3'-most bases must be found.
    #[serde(default = "default_anchor")]
    pub anchor: usize,
    /// What a site's mismatches may weigh together.
    #[serde(default = "default_penalty")]
    pub most_penalty: u32,
}

const fn default_anchor() -> usize {
    ANCHOR
}

const fn default_penalty() -> u32 {
    MOST_PENALTY
}

/// The sites found.
#[derive(Debug, Serialize)]
pub struct SitesAnswer {
    /// Every site, in order along the template.
    pub sites: Vec<Site>,
}

/// The JSON answer to a JSON request, for the module's export.
#[must_use]
pub fn place_json(request: &[u8]) -> Vec<u8> {
    let found = match serde_json::from_slice::<SitesRequest>(request) {
        Err(error) => Err(Refused(format!(
            "The request isn't one this reads: {error}"
        ))),
        Ok(request) => find_sites(
            &request.template,
            request.circular,
            &request.primers,
            request.anchor,
            request.most_penalty,
        )
        .map(|sites| SitesAnswer { sites }),
    };
    answered(found)
}

/// A request to work out what a set of sites amplifies. The sites are the ones the
/// caller chose to put in the reaction, each with the bases of the primer it is a site
/// of, so one pair of primers is worked out and not every site on the molecule.
#[derive(Debug, Deserialize)]
pub struct ProductsRequest {
    /// The template's bases.
    pub template: String,
    /// Whether it is a circle.
    #[serde(default)]
    pub circular: bool,
    /// The sites in the reaction, as [`find_sites`] gave them.
    pub sites: Vec<Site>,
    /// Each site's primer's bases, by the same index.
    pub bases: Vec<String>,
    /// The longest product to work out.
    #[serde(default = "default_longest")]
    pub longest: usize,
}

const fn default_longest() -> usize {
    LONGEST_PRODUCT
}

/// The products the sites give.
#[derive(Debug, Serialize)]
pub struct ProductsAnswer {
    /// Every product, shortest first.
    pub products: Vec<Product>,
}

/// The JSON answer to a JSON request, for the module's export.
#[must_use]
pub fn products_json(request: &[u8]) -> Vec<u8> {
    let made = match serde_json::from_slice::<ProductsRequest>(request) {
        Err(error) => Err(Refused(format!(
            "The request isn't one this reads: {error}"
        ))),
        Ok(request) => {
            let bases: Vec<Vec<u8>> = request.bases.iter().map(|one| normalise(one)).collect();
            products(
                &request.template,
                request.circular,
                &request.sites,
                &bases,
                request.longest,
            )
            .map(|products| ProductsAnswer { products })
        }
    };
    answered(made)
}

/// A request for one duplex's melting temperature under [`Conditions::PRIMER`].
#[derive(Debug, Deserialize)]
pub struct TemperatureRequest {
    /// The primer, 5' to 3'.
    pub primer: String,
    /// The template's bases under it, 5' to 3' on the primer's own strand. Left out, the
    /// primer is read against its exact complement.
    #[serde(default)]
    pub template: Option<String>,
}

/// Degrees Celsius, or nothing where no published parameter covers the duplex.
#[derive(Debug, Serialize)]
pub struct TemperatureAnswer {
    /// Degrees Celsius to one decimal place.
    pub celsius: Option<f64>,
}

/// One primer's melting temperature on one template, under [`Conditions::PRIMER`].
#[must_use]
pub fn temperature(request: &TemperatureRequest) -> Option<f64> {
    let primer = normalise(&request.primer);
    let under = match request.template.as_deref() {
        None => primer.clone(),
        Some(template) => normalise(template),
    };
    if under.len() != primer.len() {
        return None;
    }
    let against: Vec<u8> = under
        .iter()
        .map(|base| crate::thermodynamics::complement(*base))
        .collect();
    melting_temperature(&primer, &against, &Conditions::PRIMER)
}

/// The JSON answer to a JSON request, for the module's export.
#[must_use]
pub fn temperature_json(request: &[u8]) -> Vec<u8> {
    let measured = match serde_json::from_slice::<TemperatureRequest>(request) {
        Err(error) => Err(Refused(format!(
            "The request isn't one this reads: {error}"
        ))),
        Ok(request) => Ok(TemperatureAnswer {
            celsius: temperature(&request),
        }),
    };
    answered(measured)
}

/// An answer or a refusal, as JSON.
fn answered<T: Serialize>(answer: Result<T, Refused>) -> Vec<u8> {
    match answer {
        Ok(found) => serde_json::to_vec(&found).unwrap_or_else(|error| {
            serde_json::json!({ "error": error.to_string() })
                .to_string()
                .into_bytes()
        }),
        Err(Refused(why)) => serde_json::json!({ "error": why }).to_string().into_bytes(),
    }
}

#[cfg(test)]
mod tests {
    use super::{ANCHOR, MOST_PENALTY, Mismatch, Primer, find_sites, pairs, thousands};

    fn primer(hrid: &str, bases: &str) -> Primer {
        Primer {
            hrid: hrid.to_owned(),
            name: String::new(),
            bases: bases.to_owned(),
        }
    }

    /// A stretch of pUC19 around its lac promoter and the M13 reverse primer's site.
    const PUC19: &str = "GTTGTGTGGAATTGTGAGCGGATAACAATTTCACACAGGAAACAGCTATGACCATG";

    #[test]
    fn a_primer_binds_where_its_three_prime_end_pairs() {
        let found = find_sites(
            PUC19,
            false,
            &[primer("PRM-1", "GGAAACAGCTATGACCATG")],
            ANCHOR,
            MOST_PENALTY,
        )
        .expect("the template has bases");
        assert_eq!(found.len(), 1, "one site, not {found:?}");
        let site = found.first().expect("one site");
        assert_eq!(
            (
                site.strand.name(),
                site.start,
                site.end,
                site.three_prime,
                site.annealed,
                site.tail,
                site.penalty,
                site.melting_temperature,
            ),
            ("forward", 38, 56, 56, 19, 0, 0, Some(47.8))
        );
        assert!(site.mismatches.is_empty());
    }

    #[test]
    fn a_primer_that_lies_elsewhere_binds_nowhere() {
        // The M13 forward primer reads from a stretch this one doesn't hold.
        let found = find_sites(
            PUC19,
            false,
            &[primer("PRM-1", "GTAAAACGACGGCCAGT")],
            ANCHOR,
            MOST_PENALTY,
        )
        .expect("the template has bases");
        assert!(found.is_empty(), "{found:?}");
    }

    #[test]
    fn a_mismatch_away_from_the_three_prime_end_is_kept_and_weighed() {
        // The same primer with its tenth base from the 3' end changed: weight 1, and the
        // melting temperature falls from 47.8 C to 41.8 C.
        let found = find_sites(
            PUC19,
            false,
            &[primer("PRM-1", "GGAAACAGCAATGACCATG")],
            ANCHOR,
            MOST_PENALTY,
        )
        .expect("the template has bases");
        assert_eq!(found.len(), 1, "one site, not {found:?}");
        let site = found.first().expect("one site");
        assert_eq!(site.penalty, 1);
        assert_eq!(site.melting_temperature, Some(41.8));
        assert_eq!(
            site.mismatches,
            [Mismatch {
                from_three_prime: 10,
                primer: 'A',
                template: 'T',
            }]
        );
    }

    #[test]
    fn a_mismatch_in_the_last_five_bases_is_refused_under_the_default_allowance() {
        // Weight 3 is more than the allowance of 2, so this primer has no site at all.
        let found = find_sites(
            PUC19,
            false,
            &[primer("PRM-1", "GGAAACAGCTATGACCATA")],
            ANCHOR,
            MOST_PENALTY,
        )
        .expect("the template has bases");
        assert!(found.is_empty(), "{found:?}");
    }

    #[test]
    fn a_site_may_cross_a_circles_origin_and_never_a_linear_end() {
        // The primer's bases are the molecule's last nine and its first nine, so its
        // site runs across the origin -- and, being its own reverse complement, it
        // binds both strands there.
        let template = "AAAAAAAAACCCCCCCCCGGGGGGGGGTTTTTTTTT";
        let primers = [primer("PRM-1", "TTTTTTTTTAAAAAAAAA")];
        let circle = find_sites(template, true, &primers, 15, 0).expect("bases");
        assert_eq!(
            circle
                .iter()
                .map(|site| (site.strand.name(), site.start, site.end, site.three_prime))
                .collect::<Vec<_>>(),
            [("forward", 28, 9, 9), ("reverse", 28, 9, 28)]
        );
        assert!(
            find_sites(template, false, &primers, 15, 0)
                .expect("bases")
                .is_empty(),
            "a linear molecule has nothing to pair with past its end"
        );
    }

    #[test]
    fn a_primer_shorter_than_the_fewest_bases_is_passed_over() {
        let found = find_sites(PUC19, false, &[primer("PRM-1", "GACCATG")], 7, 0)
            .expect("the template has bases");
        assert!(found.is_empty(), "{found:?}");
    }

    #[test]
    fn an_iupac_code_pairs_with_what_it_may_be() {
        assert!(pairs(b'N', b'A'));
        assert!(pairs(b'R', b'G'));
        assert!(!pairs(b'R', b'C'));
        // The template's code must be no wider than the primer's: an N on the template
        // isn't a pairing with the primer's A.
        assert!(!pairs(b'A', b'N'));
        assert!(pairs(b'N', b'N'));
        assert!(!pairs(b'A', b'-'));
    }

    #[test]
    fn a_template_without_bases_is_refused() {
        let why = find_sites("", false, &[], ANCHOR, MOST_PENALTY).expect_err("no bases");
        assert_eq!(why.0, "The template has no bases.");
    }

    #[test]
    fn lengths_read_with_their_thousands_marked() {
        assert_eq!(
            [
                thousands(7),
                thousands(999),
                thousands(1_000),
                thousands(123_456)
            ],
            ["7", "999", "1,000", "123,456"]
        );
    }
}
