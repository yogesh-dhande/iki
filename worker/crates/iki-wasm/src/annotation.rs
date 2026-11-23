//! Exact sequence annotation against Iki's versioned cloning-vector feature library.

use aho_corasick::AhoCorasick;
use serde::{Deserialize, Serialize};
use std::sync::OnceLock;

/// Maximum DNA length accepted by the annotation action, in bases.
pub const MAX_BASES: usize = 16 * 1024 * 1024;
/// Space for the sequence and the annotation request's JSON envelope.
pub const MAX_REQUEST: usize = MAX_BASES + 1024;
const MAX_FEATURES: usize = 5_000;
const DATA: &str = include_str!("../data/dna_features.json");

#[derive(Deserialize)]
struct Request {
    bases: String,
    circular: bool,
    double_stranded: bool,
}

#[derive(Deserialize)]
struct Library {
    name: String,
    version: String,
    parts: Vec<Part>,
}

#[derive(Deserialize)]
struct Part {
    name: String,
    label: String,
    #[serde(rename = "type")]
    kind: String,
    role: String,
    bases: String,
    source: Source,
}

#[derive(Deserialize)]
struct Source {
    database: String,
    accession: String,
    location: String,
    record: String,
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "lowercase")]
enum Strand {
    Forward,
    Reverse,
    None,
}

struct Matcher {
    automaton: AhoCorasick,
    parts: Vec<(usize, Strand)>,
}

struct IndexedLibrary {
    library: Library,
    single: Matcher,
    double: Matcher,
    longest: usize,
}

#[derive(Serialize)]
struct Feature<'a> {
    #[serde(rename = "type")]
    kind: &'a str,
    name: &'a str,
    start: usize,
    end: usize,
    strand: Strand,
    qualifiers: Qualifiers<'a>,
}

#[derive(Serialize)]
struct Qualifiers<'a> {
    label: &'a str,
    role: &'a str,
    identity: &'static str,
    library: &'a str,
    source: String,
}

#[derive(Serialize)]
struct Answer<'a> {
    features: Vec<Feature<'a>>,
    annotated_with: &'a str,
}

const fn complement(base: u8) -> u8 {
    match base {
        b'A' => b'T',
        b'C' => b'G',
        b'G' => b'C',
        b'T' => b'A',
        _ => base,
    }
}

impl Matcher {
    fn build(library: &Library, double: bool) -> Result<Self, &'static str> {
        let mut patterns = Vec::new();
        let mut parts = Vec::new();
        for (index, part) in library.parts.iter().enumerate() {
            let forward = part.bases.as_bytes();
            let reverse: Vec<_> = forward.iter().rev().copied().map(complement).collect();
            let palindromic = double && forward == reverse;
            patterns.push(forward.to_vec());
            parts.push((
                index,
                if palindromic {
                    Strand::None
                } else {
                    Strand::Forward
                },
            ));
            if double && !palindromic {
                patterns.push(reverse);
                parts.push((index, Strand::Reverse));
            }
        }
        // Find every occurrence, including overlaps, in a single pass (Aho and
        // Corasick, 1975, https://doi.org/10.1145/360825.360855).
        let automaton = AhoCorasick::new(patterns)
            .map_err(|_| "The feature library index could not be built.")?;
        Ok(Self { automaton, parts })
    }
}

impl IndexedLibrary {
    fn load() -> Result<Self, &'static str> {
        let library: Library =
            serde_json::from_str(DATA).map_err(|_| "The feature library could not be read.")?;
        let longest = library
            .parts
            .iter()
            .map(|part| part.bases.len())
            .max()
            .filter(|length| *length > 0)
            .ok_or("The feature library is empty.")?;
        let single = Matcher::build(&library, false)?;
        let double = Matcher::build(&library, true)?;
        Ok(Self {
            library,
            single,
            double,
            longest,
        })
    }

    fn annotate<'a>(
        &'a self,
        request: Request,
        label: &'a str,
    ) -> Result<Answer<'a>, &'static str> {
        let mut bases = request.bases.into_bytes();
        let length = bases.len();
        if length == 0 || length > MAX_BASES {
            return Err("DNA annotation takes between 1 and 16,777,216 bases.");
        }
        bases.make_ascii_uppercase();
        if !bases.iter().all(|base| b"ACGTRYSWKMBDHVN".contains(base)) {
            return Err("DNA annotation takes only IUPAC DNA base codes.");
        }
        // Repeat only the portion needed for a hit across the origin. Every accepted
        // hit starts in the original sequence and covers at most one revolution.
        if request.circular {
            let tail = self.longest.min(length) - 1;
            bases.extend_from_within(..tail);
        }
        let matcher = if request.double_stranded {
            &self.double
        } else {
            &self.single
        };
        let mut features = Vec::new();
        for hit in matcher.automaton.find_overlapping_iter(&bases) {
            if hit.start() >= length || hit.len() > length {
                continue;
            }
            if features.len() == MAX_FEATURES {
                return Err("More than 5,000 library features match this sequence.");
            }
            let (part_index, strand) = matcher.parts[hit.pattern().as_usize()];
            let part = &self.library.parts[part_index];
            let source = &part.source;
            features.push(Feature {
                kind: &part.kind,
                name: &part.name,
                start: hit.start() + 1,
                end: (hit.end() - 1) % length + 1,
                strand,
                qualifiers: Qualifiers {
                    label: &part.label,
                    role: &part.role,
                    identity: "100.0%",
                    library: label,
                    source: format!(
                        "{} {} {} ({})",
                        source.database, source.accession, source.location, source.record
                    ),
                },
            });
        }
        features.sort_unstable_by_key(|feature| {
            (feature.start, feature.end, feature.name, feature.strand)
        });
        Ok(Answer {
            features,
            annotated_with: label,
        })
    }
}

fn annotation(bytes: &[u8]) -> Result<Vec<u8>, String> {
    static LIBRARY: OnceLock<Result<IndexedLibrary, &'static str>> = OnceLock::new();
    if bytes.len() > MAX_REQUEST {
        return Err("The DNA annotation request is too large.".to_owned());
    }
    let request = serde_json::from_slice::<Request>(bytes)
        .map_err(|error| format!("The DNA annotation request is invalid: {error}."))?;
    let library = LIBRARY
        .get_or_init(IndexedLibrary::load)
        .as_ref()
        .map_err(|error| *error)?;
    let label = format!("{} {}", library.library.name, library.library.version);
    let answer = library.annotate(request, &label)?;
    serde_json::to_vec(&answer)
        .map_err(|error| format!("The annotations could not be written: {error}."))
}

/// Annotates a JSON request containing `bases`, `circular` and `double_stranded`.
/// Returns `features` and `annotated_with`, or an `error` without partial hits.
#[must_use]
pub fn annotate_json(bytes: &[u8]) -> Vec<u8> {
    annotation(bytes).unwrap_or_else(|error| {
        serde_json::json!({ "error": error })
            .to_string()
            .into_bytes()
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    fn annotate(bases: &str, circular: bool, double_stranded: bool) -> Value {
        let request =
            json!({ "bases": bases, "circular": circular, "double_stranded": double_stranded });
        serde_json::from_slice(&annotate_json(request.to_string().as_bytes()))
            .unwrap_or_else(|error| unreachable!("{error}"))
    }

    #[test]
    fn finds_an_independent_genbank_vectors_origin_on_the_reverse_strand() {
        // GenBank J01749.1 (pBR322) independently contains the 589-base pMB1 library
        // feature from OL989372.1. The NCBI pBR322 record labels the core replication
        // origin inside this span. The expected interval is the full library match.
        let record = include_str!("../../../../server/tests/data/plasmids/J01749.1.gb");
        let bases: String = record
            .split_once("\nORIGIN")
            .map(|(_, bases)| bases)
            .unwrap_or_default()
            .chars()
            .filter(char::is_ascii_alphabetic)
            .collect();
        let result = annotate(&bases, true, true);
        assert_eq!(
            result["features"][1],
            json!({
                "type": "rep_origin", "name": "ori", "start": 2534, "end": 3122,
                "strand": "reverse",
                "qualifiers": {
                    "label": "ori (ColE1/pMB1/pBR322/pUC origin of replication)",
                    "role": "origin", "identity": "100.0%",
                    "library": "Routine cloning-vector features 1.0",
                    "source": "INSDC OL989372.1 1497..2085 (Cloning vector pET-28a-ZaTdT12)"
                }
            })
        );
        let single = annotate(&bases, true, false);
        assert_eq!(single["features"].as_array().map(Vec::len), Some(1));
        assert_eq!(single["features"][0]["name"], "rop");
        assert_eq!(single["features"][0]["strand"], "forward");
    }

    #[test]
    fn detects_origin_crossings_only_on_a_circle_and_accepts_lowercase() {
        // T7 promoter, GenBank OL989372.1 4984..5002, rotated with synthetic flanks.
        let bases = "tcactataggNNNNtaatacgac";
        assert_eq!(annotate(bases, false, true)["features"], json!([]));
        let matches = annotate(bases, true, true);
        assert_eq!(matches["features"].as_array().map(Vec::len), Some(1));
        assert_eq!(matches["features"][0]["name"], "T7 promoter");
        assert_eq!(matches["features"][0]["start"], 15);
        assert_eq!(matches["features"][0]["end"], 10);
    }

    #[test]
    fn keeps_separate_copies_and_requires_every_base_to_match() {
        // Original T7 promoter sequence, duplicate copy, and a copy with an unknown base.
        let bases = "TAATACGACTCACTATAGGNNTAATACGACTCACTATAGGNNTAATACGACTCACTATAGN";
        let matches = annotate(bases, false, false);
        let features = matches["features"]
            .as_array()
            .unwrap_or_else(|| unreachable!());
        assert_eq!(features.len(), 2);
        assert_eq!(features[0]["start"], 1);
        assert_eq!(features[1]["start"], 22);
    }

    #[test]
    fn rejects_unrecognised_bases_and_partial_results() {
        assert!(annotate("AUCG", false, true)["error"].is_string());
        assert!(annotate("", false, true)["error"].is_string());
        let repeated = "TAATACGACTCACTATAGG".repeat(MAX_FEATURES + 1);
        let answer = annotate(&repeated, false, false);
        assert_eq!(
            answer["error"],
            "More than 5,000 library features match this sequence."
        );
        assert!(answer.get("features").is_none());
    }
}
