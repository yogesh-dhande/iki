/**
 * How an alignment's rows compare with its reference, for the viewer's summary. Clustal's
 * count of identical columns says little when rows cover only part of the reference, as
 * reads do: every column has a gap somewhere. Compared with the reference, the summary
 * says how much of it the other rows cover and where they differ from it.
 */

import type { Alphabet } from "./reading";

/** The most residues compared on the page, so a huge alignment doesn't hold it up. */
export const MAX_COMPARED = 50_000_000;

export interface Compared {
  /** The reference's residues. */
  residues: number;
  /** Its residues within another row's span (between that row's first and last residue). */
  covered: number;
  /** Columns where another row, within its span, has something else: another residue, a
   * gap where the reference has a residue, or a residue where it has a gap. An unknown
   * residue (N, or X in a protein) differs from nothing. */
  differing: number;
}

const GAP = "-".charCodeAt(0);

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
  const unknown = (alphabet === "nucleotide" ? "N" : "X").charCodeAt(0);
  const covered = new Uint8Array(length);
  const differs = new Uint8Array(length);
  for (const row of rows) {
    if (row === reference) continue;
    const aligned = row.aligned;
    let first = 0;
    while (first < length && aligned.charCodeAt(first) === GAP) first++;
    let last = length - 1;
    while (last >= first && aligned.charCodeAt(last) === GAP) last--;
    for (let at = first; at <= last; at++) {
      const mine = aligned.charCodeAt(at);
      const theirs = ref.charCodeAt(at);
      covered[at] = 1;
      if (mine !== theirs && mine !== unknown) differs[at] = 1;
    }
  }
  let residues = 0;
  let within = 0;
  let differing = 0;
  for (let at = 0; at < length; at++) {
    const residue = ref.charCodeAt(at) !== GAP;
    if (residue) residues++;
    if (residue && covered[at]) within++;
    if (differs[at]) differing++;
  }
  return { residues, covered: within, differing };
}
