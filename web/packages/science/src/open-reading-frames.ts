import { GENETIC_CODES, type GeneticCode } from "./translation.js";
import { reverseComplement } from "./sequence.js";

/** Coordinates use the forward reference strand, 1-based and inclusive. A start greater
 * than the end crosses a circle's origin. Length includes the terminal stop codon. */
export interface OrfAnnotation {
  start: number;
  end: number;
  strand: "forward" | "reverse";
  frame: number;
  length: number;
  amino_acids: number;
  start_codon: string;
  translation: string;
}

export interface OrfOptions {
  circular?: boolean;
  table?: number;
  alternativeStarts?: boolean;
  /** Number of sense codons from the initiator up to the stop, exclusive. Default 75. */
  minimumCodons?: number;
}

// Codons in the order of NCBI's ncbieaa and sncbieaa rows:
// https://www.ncbi.nlm.nih.gov/Taxonomy/Utils/wprintgc.cgi
function codonsOf(table: GeneticCode) {
  const residues = new Map<string, string>();
  const starts = new Set<string>();
  let index = 0;
  for (const a of "TCAG") {
    for (const b of "TCAG") {
      for (const c of "TCAG") {
        const codon = a + b + c;
        residues.set(codon, table.residues.charAt(index));
        if (table.starts.charAt(index) === "M") starts.add(codon);
        index++;
      }
    }
  }
  return { residues, starts };
}

/** Annotate complete start-to-stop reading frames, retaining the longest for each stop
 * as in NCBI ORFfinder's “Ignore nested ORFs” mode. Ambiguous codons are X, and never
 * start or stop. A circular reading ends within one lap, including its stop.
 * https://www.ncbi.nlm.nih.gov/orffinder/ */
export function openReadingFrames(sequence: string, options: OrfOptions = {}): OrfAnnotation[] {
  const bases = sequence.toUpperCase().replaceAll("U", "T");
  if (!/^[ACGTRYSWKMBDHVN]*$/.test(bases))
    throw new RangeError("Expected an IUPAC nucleotide sequence.");
  const minimum = options.minimumCodons ?? 75;
  if (!Number.isSafeInteger(minimum) || minimum < 1)
    throw new RangeError("Expected a positive minimum number of codons.");
  const table = GENETIC_CODES[options.table ?? 1];
  if (!table || table.dual.length)
    throw new RangeError("Expected an NCBI table with unambiguous stop codons.");
  const { residues, starts } = codonsOf(table);
  const length = bases.length;
  if (length < (minimum + 1) * 3) return [];
  const found: OrfAnnotation[] = [];

  for (const strand of ["forward", "reverse"] as const) {
    const read = strand === "forward" ? bases : reverseComplement(bases);
    // Inspect the second copy's stops on a circle, after reading all starts that
    // can reach each stop in one lap. This also covers lengths not divisible by 3.
    const text = options.circular ? read + read + read.slice(0, 2) : read;
    for (let frame = 0; frame < 3; frame++) {
      const candidates: number[] = [];
      for (let at = frame; at + 3 <= text.length; at += 3) {
        const codon = text.slice(at, at + 3);
        if (options.alternativeStarts ? starts.has(codon) : codon === "ATG") candidates.push(at);
        if (residues.get(codon) !== "*") continue;
        const start = candidates.find((candidate) => at + 3 - candidate <= length);
        candidates.length = 0;
        if (start === undefined || at - start < 3 * minimum || (options.circular && at < length))
          continue;
        const first = start % length;
        const last = (at + 2) % length;
        let translation = "M";
        for (let i = start + 3; i < at; i += 3)
          translation += residues.get(text.slice(i, i + 3)) ?? "X";
        found.push({
          start: strand === "forward" ? first + 1 : length - last,
          end: strand === "forward" ? last + 1 : length - first,
          strand,
          frame: ((first % 3) + 1) * (strand === "forward" ? 1 : -1),
          length: at + 3 - start,
          amino_acids: translation.length,
          start_codon: text.slice(start, start + 3),
          translation,
        });
      }
    }
  }
  return found.sort(
    (a, b) => a.start - b.start || a.end - b.end || a.strand.localeCompare(b.strand),
  );
}
