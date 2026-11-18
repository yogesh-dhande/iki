/**
 * How an alignment's rows compare with its reference, for the viewer's summary. Clustal's
 * count of identical columns says little when rows cover only part of the reference, as
 * reads do: every column has a gap somewhere. Compared with the reference, the summary
 * says how much of it the other rows cover and where they differ from it.
 *
 * A row says something of a column when it has a residue there, or a gap (`-`, a deletion)
 * between its first and last residue. It says nothing where it has `.` (no observation,
 * such as where a read doesn't reach, or across a circular reference's origin), a gap
 * outside its span, or an ambiguous residue that could be the reference's (an N, say):
 * none of those covers the reference or differs from it. Residues compare as the sets
 * their IUPAC codes stand for, the same either way round: a row differs only where none of
 * what it could be is anything the reference could be.
 */

import type { Alphabet } from "./reading";

/** The most residues compared on the page, so a huge alignment doesn't hold it up. */
export const MAX_COMPARED = 50_000_000;

export interface Compared {
  /** The reference's residues. */
  residues: number;
  /** Its residues another row says what's at: a residue, or a deletion. */
  covered: number;
  /** Columns where another row has something the reference can't be: another residue, a
   * gap where the reference has a residue, or a residue where it has a gap. */
  differing: number;
}

/** The bases each IUPAC nucleotide code stands for (IUBMB, 1985). */
const BASES: Record<string, string> = {
  A: "A",
  C: "C",
  G: "G",
  T: "T",
  U: "T",
  R: "AG",
  Y: "CT",
  S: "CG",
  W: "AT",
  K: "GT",
  M: "AC",
  B: "CGT",
  D: "AGT",
  H: "ACT",
  V: "ACG",
  N: "ACGT",
};

/** The amino acids each ambiguous code stands for (IUPAC-IUB); others stand for themselves. */
const RESIDUES: Record<string, string> = {
  B: "DN",
  Z: "EQ",
  J: "IL",
  X: "ACDEFGHIKLMNPQRSTVWYUO",
};

/** What a residue could be, as a set of letters. */
export function standsFor(residue: string, alphabet: Alphabet): string {
  const upper = residue.toUpperCase();
  return (alphabet === "nucleotide" ? BASES[upper] : RESIDUES[upper]) ?? upper;
}

/** How a row's residue compares with the reference's: `same` if it's one residue the
 * reference could be, `differs` if it can be none of what the reference could be, and
 * `unresolved` if it's ambiguous and could be the reference's, which says nothing. */
export function call(
  mine: string,
  theirs: string,
  alphabet: Alphabet,
): "same" | "differs" | "unresolved" {
  const a = standsFor(mine, alphabet);
  const b = standsFor(theirs, alphabet);
  let shared = false;
  for (let i = 0; i < a.length && !shared; i++) shared = b.includes(a.charAt(i));
  if (!shared) return "differs";
  return a.length === 1 ? "same" : "unresolved";
}

const GAP = "-";
const NOTHING = ".";

/** How the other rows compare with `reference`, or null when that's more than
 * `MAX_COMPARED` residues' work. */
export function compared(
  rows: readonly { aligned: string }[],
  reference: { aligned: string },
  alphabet: Alphabet,
): Compared | null {
  const ref = reference.aligned;
  const length = ref.length;
  if (rows.length * length > MAX_COMPARED) return null;
  const residueAt = (aligned: string, at: number) => {
    const c = aligned.charAt(at);
    return c !== GAP && c !== NOTHING;
  };
  const covered = new Uint8Array(length);
  const differs = new Uint8Array(length);
  for (const row of rows) {
    if (row === reference) continue;
    const aligned = row.aligned;
    let first = 0;
    while (first < length && !residueAt(aligned, first)) first++;
    let last = length - 1;
    while (last >= first && !residueAt(aligned, last)) last--;
    for (let at = first; at <= last; at++) {
      const mine = aligned.charAt(at);
      const theirs = ref.charAt(at);
      if (mine === NOTHING) continue;
      const theirsResidue = residueAt(ref, at);
      if (mine === GAP) {
        // A deletion where the reference has a residue; a gap opposite a gap is nothing.
        if (theirsResidue) {
          covered[at] = 1;
          differs[at] = 1;
        }
        continue;
      }
      if (!theirsResidue) {
        differs[at] = 1; // a residue the reference doesn't have
        continue;
      }
      const found = call(mine, theirs, alphabet);
      if (found === "unresolved") continue;
      covered[at] = 1;
      if (found === "differs") differs[at] = 1;
    }
  }
  let residues = 0;
  let within = 0;
  let differing = 0;
  for (let at = 0; at < length; at++) {
    const residue = residueAt(ref, at);
    if (residue) residues++;
    if (residue && covered[at]) within++;
    if (differs[at]) differing++;
  }
  return { residues, covered: within, differing };
}
