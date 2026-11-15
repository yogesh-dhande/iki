/**
 * The alignment viewer's working parts that aren't drawing (ADR 0088): which rows are
 * sequences in Iki, how residues are coloured, and where a residue falls in its own
 * sequence once the gaps are taken out.
 */

import type { Entity } from "./entities";
import type { Alphabet, AlignmentRow } from "./reading";

/** A row as the viewer shows it: the file's, and the sequence in Iki it is, if it's one. */
export interface Shown {
  /** Its place in the file, from 0. */
  index: number;
  row: AlignmentRow;
  /** The sequence in Iki it's headed by the HRID of, among those the alignment lists. */
  entity: { id: string; hrid: string; title: string; type: string } | null;
}

/** The rows in the file's order, each with the sequence in Iki it is. Only sequences the
 * alignment lists (its members and its reference) link: a header that merely looks like an
 * HRID isn't taken for one. */
export function shownRows(rows: AlignmentRow[], listed: string[], refs: Entity["refs"]): Shown[] {
  const byHrid = new Map<string, Shown["entity"]>();
  for (const id of listed) {
    const ref = refs[id];
    if (ref) byHrid.set(ref.hrid, { id, hrid: ref.hrid, title: ref.title, type: ref.type });
  }
  return rows.map((row, index) => ({ index, row, entity: byHrid.get(row.id) ?? null }));
}

/** The IDs a field's value names: one reference, or a list of them. */
export function idsOf(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.filter((v): v is string => typeof v === "string");
  return [];
}

/** Residue kinds, as Clustal X colours them. */
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

/** A residue's number in its own sequence, from 1, at a column (from 0), or null at a gap. */
export function residueNumber(aligned: string, column: number): number | null {
  if (column < 0 || column >= aligned.length || isGap(aligned.charAt(column))) return null;
  let n = 0;
  for (let i = 0; i <= column; i++) if (!isGap(aligned.charAt(i))) n++;
  return n;
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
