/**
 * The alignment viewer's working parts that aren't drawing (ADR 0088): which rows are
 * sequences in Iki, how residues are coloured, and where a residue falls in its own
 * sequence once the gaps are taken out.
 */

import type { Entity } from "./entities";
import type { AlignmentRow } from "./reading";

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
    if (!ref) continue;
    const shown = { id, hrid: ref.hrid, title: ref.title, type: ref.type };
    // An entity imported with a new HRID: a file made where it's from names it by its old one.
    for (const former of ref.formerly ?? []) {
      if (!byHrid.has(former)) byHrid.set(former, shown);
    }
    byHrid.set(ref.hrid, shown);
  }
  return rows.map((row, index) => ({ index, row, entity: byHrid.get(row.id) ?? null }));
}

/** The IDs a field's value names: one reference, or a list of them. */
export function idsOf(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.filter((v): v is string => typeof v === "string");
  return [];
}

export { colourOf, moved, residueNumber } from "@ikisci/science/alignment-viewer";
