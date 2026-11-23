/** Display and keyboard navigation for an already aligned set of sequences. */
import type { Alphabet } from "./parsers.js";

/** Residue groups for a Clustal X-style display. The threshold below is a viewer setting.
 * https://www.theclustal.com/download/clustalx_help/ */
export type Kind =
  | "hydrophobic"
  | "positive"
  | "negative"
  | "polar"
  | "cysteine"
  | "glycine"
  | "proline"
  | "aromatic";

const PROTEIN: Record<string, Kind> = {
  A: "hydrophobic",
  I: "hydrophobic",
  L: "hydrophobic",
  M: "hydrophobic",
  F: "hydrophobic",
  W: "hydrophobic",
  V: "hydrophobic",
  K: "positive",
  R: "positive",
  E: "negative",
  D: "negative",
  N: "polar",
  Q: "polar",
  S: "polar",
  T: "polar",
  C: "cysteine",
  G: "glycine",
  P: "proline",
  H: "aromatic",
  Y: "aromatic",
};

/** Nucleotides take the colours of four of the kinds: A green, C blue, G orange, T and U red. */
const NUCLEOTIDE: Record<string, Kind> = {
  A: "polar",
  C: "hydrophobic",
  G: "glycine",
  T: "positive",
  U: "positive",
};

export const KINDS: Kind[] = [
  "hydrophobic",
  "positive",
  "negative",
  "polar",
  "cysteine",
  "glycine",
  "proline",
  "aromatic",
];

/** A residue's kind, or null for a gap or a residue no kind has (such as X or N). */
export function kindOf(residue: string, alphabet: Alphabet): Kind | null {
  const upper = residue.toUpperCase();
  return (alphabet === "protein" ? PROTEIN : NUCLEOTIDE)[upper] ?? null;
}

/** How residues are coloured: where their column agrees, all of them, or none. */
export type Colouring = "agreeing" | "all" | "none";

/** The share of a column that has to agree with its consensus for its residues to be coloured. */
export const AGREEING = 50;

/** The kind to colour a residue by, if it's coloured at all. Where its column agrees, a
 * residue is coloured when it's of the same kind as the column's consensus residue. */
export function colourOf(
  residue: string,
  consensus: string,
  agreement: number,
  alphabet: Alphabet,
  colouring: Colouring,
): Kind | null {
  if (colouring === "none") return null;
  const kind = kindOf(residue, alphabet);
  if (!kind || colouring === "all") return kind;
  return agreement >= AGREEING && kindOf(consensus, alphabet) === kind ? kind : null;
}

export function isGap(residue: string): boolean {
  return residue === "-" || residue === ".";
}

/** A residue's number in its own sequence at a column (from 0), or null at a gap: counted
 * from `start`, the row's first residue's number (1, or more for part of a sequence), and
 * counting down when the row is its sequence's other strand (`reverse`). */
export function residueNumber(
  aligned: string,
  column: number,
  start = 1,
  reverse = false,
): number | null {
  if (column < 0 || column >= aligned.length || isGap(aligned.charAt(column))) return null;
  let before = 0;
  for (let i = 0; i < column; i++) if (!isGap(aligned.charAt(i))) before++;
  return reverse ? start - before : start + before;
}

/** Where the cursor goes for a key, in a grid of `rows` by `columns`, with `page` rows in
 * view; null for keys that don't move it. */
export function moved(
  key: string,
  at: { row: number; column: number },
  rows: number,
  columns: number,
  page: number,
): { row: number; column: number } | null {
  const clamp = (n: number, most: number) => Math.min(Math.max(n, 0), most - 1);
  const { row, column } = at;
  switch (key) {
    case "ArrowLeft":
      return { row, column: clamp(column - 1, columns) };
    case "ArrowRight":
      return { row, column: clamp(column + 1, columns) };
    case "ArrowUp":
      return { row: clamp(row - 1, rows), column };
    case "ArrowDown":
      return { row: clamp(row + 1, rows), column };
    case "PageUp":
      return { row: clamp(row - page, rows), column };
    case "PageDown":
      return { row: clamp(row + page, rows), column };
    case "Home":
      return { row, column: 0 };
    case "End":
      return { row, column: columns - 1 };
    default:
      return null;
  }
}
