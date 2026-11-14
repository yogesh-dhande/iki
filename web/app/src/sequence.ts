/**
 * The sequence viewer's layout: residues in rows, and features as a track along them,
 * each in a lane of its own where they overlap. Pure, so it's tested without a page.
 */

/** Residues in a row, and in a group within it. */
export const ROW = 60;
export const GROUP = 10;

export type Strand = "forward" | "reverse" | "none";

/** A region along the sequence, as a type's features field lists it (GenBank's way). */
export interface Feature {
  /** Its place in the list, which names it when it has no name. */
  index: number;
  type: string;
  name: string;
  /** From 1, inclusive. An end before the start wraps the origin of a circular sequence. */
  start: number;
  end: number;
  strand: Strand;
  lane: number;
}

/** What a feature looks like, by its type: coding, regulatory, and so on. */
export type Kind = "coding" | "regulatory" | "terminator" | "origin" | "binding" | "other";

const KINDS: Record<string, Kind> = {
  cds: "coding",
  gene: "coding",
  mrna: "coding",
  exon: "coding",
  mat_peptide: "coding",
  sig_peptide: "coding",
  promoter: "regulatory",
  enhancer: "regulatory",
  regulatory: "regulatory",
  "5'utr": "regulatory",
  rbs: "regulatory",
  terminator: "terminator",
  polya_signal: "terminator",
  "3'utr": "terminator",
  rep_origin: "origin",
  origin_of_replication: "origin",
  orit: "origin",
  primer_bind: "binding",
  protein_bind: "binding",
  misc_binding: "binding",
};

export function kindOf(type: string): Kind {
  return KINDS[type.toLowerCase()] ?? "other";
}

/** A feature's name as it reads: its own, or its type and place. */
export function featureName(feature: Feature): string {
  return feature.name || `${feature.type} ${String(feature.index + 1)}`;
}

/** The features a field holds, as far as they can be drawn: anything else is left out,
 * so one bad entry doesn't hide the rest. Laned so overlapping ones don't cover each other. */
export function readFeatures(value: unknown, length: number): Feature[] {
  if (!Array.isArray(value)) return [];
  const out: Feature[] = [];
  value.forEach((item: unknown, index) => {
    if (typeof item !== "object" || item === null) return;
    const f = item as Record<string, unknown>;
    const { type, start, end } = f;
    if (typeof type !== "string" || !type) return;
    if (!Number.isInteger(start) || !Number.isInteger(end)) return;
    const [from, to] = [start as number, end as number];
    if (from < 1 || to < 1 || from > length || to > length) return;
    const strand = f["strand"];
    out.push({
      index,
      type,
      name: typeof f["name"] === "string" ? f["name"] : "",
      start: from,
      end: to,
      strand: strand === "forward" || strand === "reverse" ? strand : "none",
      lane: 0,
    });
  });
  return laned(out, length);
}

/** The stretches a feature covers, from 1: one, or two if it wraps the origin. */
export function spans(feature: Feature, length: number): [number, number][] {
  return feature.end >= feature.start
    ? [[feature.start, feature.end]]
    : [
        [feature.start, length],
        [1, feature.end],
      ];
}

export function featureLength(feature: Feature, length: number): number {
  return spans(feature, length).reduce((sum, [from, to]) => sum + to - from + 1, 0);
}

/** Each feature in the first lane where it overlaps nothing, longest first so the
 * big ones (a gene) sit above the small ones (its parts). */
function laned(features: Feature[], length: number): Feature[] {
  const order = [...features].sort(
    (a, b) => featureLength(b, length) - featureLength(a, length) || a.start - b.start,
  );
  const lanes: [number, number][][] = [];
  for (const feature of order) {
    const mine = spans(feature, length);
    let lane = lanes.findIndex((taken) =>
      taken.every(([a, b]) => mine.every(([c, d]) => d < a || c > b)),
    );
    if (lane < 0) lane = lanes.push([]) - 1;
    lanes[lane]?.push(...mine);
    feature.lane = lane;
  }
  return features;
}

/** Where a stretch lies within one row: its first column and how many it covers. */
export interface Piece {
  feature: Feature;
  /** Columns from the row's start, counting the gaps between groups. */
  column: number;
  width: number;
  /** Whether the feature begins or ends here, so its arrow is drawn where it ends. */
  first: boolean;
  last: boolean;
}

/** The column a residue's place in its row (from 0) is drawn at, with a gap per group. */
export function columnOf(offset: number): number {
  return offset + Math.floor(offset / GROUP);
}

/** The pieces of features in the row starting at `rowStart` (from 1). */
export function piecesIn(features: Feature[], rowStart: number, length: number): Piece[] {
  const rowEnd = Math.min(rowStart + ROW - 1, length);
  const out: Piece[] = [];
  for (const feature of features) {
    for (const [from, to] of spans(feature, length)) {
      const a = Math.max(from, rowStart);
      const b = Math.min(to, rowEnd);
      if (a > b) continue;
      const column = columnOf(a - rowStart);
      out.push({
        feature,
        column,
        width: columnOf(b - rowStart) - column + 1,
        first: a === feature.start,
        last: b === feature.end,
      });
    }
  }
  return out;
}

/** How many lanes each row's features need, so rows can be sized before they're drawn. */
export function lanesPerRow(features: Feature[], length: number): number[] {
  const rows = new Array<number>(Math.ceil(length / ROW)).fill(0);
  for (const feature of features) {
    for (const [from, to] of spans(feature, length)) {
      for (let row = Math.floor((from - 1) / ROW); row <= Math.floor((to - 1) / ROW); row++) {
        rows[row] = Math.max(rows[row] ?? 0, feature.lane + 1);
      }
    }
  }
  return rows;
}

/** "1..2686", "2601..120 (across the origin)": a location as GenBank would write it. */
export function locationText(feature: Feature): string {
  const at = `${String(feature.start)}..${String(feature.end)}`;
  return feature.end < feature.start ? `${at} (across the origin)` : at;
}
