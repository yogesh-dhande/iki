/**
 * The sequence viewer's layout: residues in rows, and features as a track along them,
 * each in a lane of its own where they overlap. Pure, so it's tested without a page.
 */

/** Residues in a row, and in a group within it. */
export const ROW = 60;
export const GROUP = 10;

export type Strand = "forward" | "reverse" | "none";

/** Where a feature lies, a part at a time: a join's parts, or a site between two bases. */
export interface Part {
  /** From 1, inclusive. An end before the start wraps the origin. */
  start: number;
  end: number;
  strand: Strand;
  /** A site between `start` and `end`, two neighbouring bases, covering neither. */
  between: boolean;
}

/** A region along the sequence, as a type's features field lists it (GenBank's way). */
export interface Feature {
  /** Its place in the list, which names it when it has no name. */
  index: number;
  type: string;
  name: string;
  /** The stretch it lies within, from 1, inclusive. An end before the start wraps the origin
   * of a circular sequence. */
  start: number;
  end: number;
  /** Mixed when its parts lie on both strands. */
  strand: Strand | "mixed";
  /** Where it lies within that stretch, in the order its parts are read (a reverse-strand
   * join's last first): the whole stretch, unless the field lists parts. */
  parts: Part[];
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

function strandOf(value: unknown): Strand {
  return value === "forward" || value === "reverse" ? value : "none";
}

/** A feature's parts, if the field lists them and each lies on the sequence; else null. */
function readParts(value: unknown, length: number): Part[] | null {
  if (!Array.isArray(value) || !value.length) return null;
  const out: Part[] = [];
  for (const item of value as unknown[]) {
    if (typeof item !== "object" || item === null) return null;
    const p = item as Record<string, unknown>;
    const { start, end } = p;
    if (!Number.isInteger(start) || !Number.isInteger(end)) return null;
    const [from, to] = [start as number, end as number];
    if (from < 1 || to < 1 || from > length || to > length) return null;
    out.push({
      start: from,
      end: to,
      strand: strandOf(p["strand"]),
      between: p["between"] === true,
    });
  }
  return out;
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
    const strand = f["strand"] === "mixed" ? "mixed" : strandOf(f["strand"]);
    const listed = f["parts"] === undefined ? null : readParts(f["parts"], length);
    if (f["parts"] !== undefined && !listed) return;
    out.push({
      index,
      type,
      name: typeof f["name"] === "string" ? f["name"] : "",
      start: from,
      end: to,
      strand,
      parts: listed ?? [
        { start: from, end: to, strand: strand === "mixed" ? "none" : strand, between: false },
      ],
      lane: 0,
    });
  });
  return laned(out, length);
}

/** The stretches from `start` to `end`, from 1: one, or two if it wraps the origin. */
function stretches(start: number, end: number, length: number): [number, number][] {
  return end >= start
    ? [[start, end]]
    : [
        [start, length],
        [1, end],
      ];
}

/** The stretches a feature lies within, from its first base to its last: one, or two if
 * it wraps the origin. Its parts, and what joins them, are drawn in them. */
export function spans(feature: Feature, length: number): [number, number][] {
  return stretches(feature.start, feature.end, length);
}

/** The bases a feature covers, in order along the sequence: its parts' (not what's between a
 * join's parts, nor a site between two bases). */
export function covered(feature: Feature, length: number): [number, number][] {
  return feature.parts
    .filter((p) => !p.between)
    .flatMap((p) => stretches(p.start, p.end, length))
    .sort((a, b) => a[0] - b[0]);
}

/** How many bases it covers: its parts', counted once each. */
export function featureLength(feature: Feature, length: number): number {
  let total = 0;
  let reached = 0;
  for (const [from, to] of covered(feature, length)) {
    const a = Math.max(from, reached + 1);
    if (to >= a) total += to - a + 1;
    reached = Math.max(reached, to);
  }
  return total;
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
  /** What it is: part of the feature, the line joining two of its parts, or a site between
   * two bases, drawn as a mark at the boundary after `column`. */
  shape: "part" | "joining" | "site";
  /** Its direction's arrow, drawn where the feature's last part ends, if it's here. */
  arrow: "forward" | "reverse" | null;
}

/** The column a residue's place in its row (from 0) is drawn at, with a gap per group. */
export function columnOf(offset: number): number {
  return offset + Math.floor(offset / GROUP);
}

/** The pieces of features in the row starting at `rowStart` (from 1): their parts, the lines
 * joining them, and sites between bases. */
export function piecesIn(features: Feature[], rowStart: number, length: number): Piece[] {
  const rowEnd = Math.min(rowStart + ROW - 1, length);
  const out: Piece[] = [];
  const place = (
    feature: Feature,
    from: number,
    to: number,
    shape: Piece["shape"],
    arrow: (a: number, b: number) => Piece["arrow"],
  ) => {
    const a = Math.max(from, rowStart);
    const b = Math.min(to, rowEnd);
    if (a > b) return;
    const column = columnOf(a - rowStart);
    out.push({
      feature,
      column,
      width: columnOf(b - rowStart) - column + 1,
      shape,
      arrow: arrow(a, b),
    });
  };
  for (const feature of features) {
    const last = feature.parts.at(-1);
    const bases = covered(feature, length);
    // What's between its parts, within its stretch, is drawn as a line joining them.
    for (const [from, to] of spans(feature, length)) {
      let at = from;
      for (const [a, b] of bases) {
        if (b < at || a > to) continue;
        if (a > at) place(feature, at, a - 1, "joining", () => null);
        at = Math.max(at, b + 1);
      }
      if (at <= to && bases.length) place(feature, at, to, "joining", () => null);
    }
    for (const part of feature.parts) {
      if (part.between) {
        // The boundary after base `start`: drawn on that base's column, at its right edge.
        place(feature, part.start, part.start, "site", () => null);
        continue;
      }
      const end = part === last ? part : null;
      for (const [from, to] of stretches(part.start, part.end, length)) {
        place(feature, from, to, "part", (a, b) =>
          end?.strand === "forward" && b === end.end
            ? "forward"
            : end?.strand === "reverse" && a === end.start
              ? "reverse"
              : null,
        );
      }
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

/** "1..2686", "2601..120 (across the origin)", "join(2..4,10..12)", "5^6": a location as
 * GenBank would write it. */
export function locationText(feature: Feature): string {
  const across = feature.end < feature.start ? " (across the origin)" : "";
  const [only] = feature.parts;
  if (feature.parts.length === 1 && only && !only.between) {
    return `${String(feature.start)}..${String(feature.end)}${across}`;
  }
  const strands = new Set(feature.parts.map((p) => p.strand));
  const reverse = strands.size === 1 && strands.has("reverse");
  // A reverse-strand feature's parts are listed last first; GenBank writes them in order
  // along the sequence, inside one complement().
  const listed = reverse ? [...feature.parts].reverse() : feature.parts;
  const text = listed.map((p) => {
    const at = `${String(p.start)}${p.between ? "^" : ".."}${String(p.end)}`;
    return p.strand === "reverse" && !reverse ? `complement(${at})` : at;
  });
  const joined = text.length > 1 ? `join(${text.join(",")})` : (text[0] ?? "");
  return `${reverse ? `complement(${joined})` : joined}${across}`;
}

/** A track's values: one number per residue, or null where there's none. */
export type Values = (number | null)[];

/** The values, if `value` holds one per residue of a sequence of `length`. */
export function readTrack(value: unknown, length: number): Values | null {
  if (!Array.isArray(value) || value.length !== length) return null;
  return value.every((v) => v === null || (typeof v === "number" && Number.isFinite(v)))
    ? (value as Values)
    : null;
}

/** Where a track's scale runs, from its field's bounds or else its values, always with zero
 * in it so bars rise above or fall below a baseline. */
export function trackScale(
  values: Values,
  min: number | null,
  max: number | null,
): [number, number] {
  let low = min ?? 0;
  let high = max ?? 0;
  for (const v of values) {
    if (v === null) continue;
    if (min === null && v < low) low = v;
    if (max === null && v > high) high = v;
  }
  low = Math.min(low, 0);
  high = Math.max(high, 0);
  return low === high ? [low, low + 1] : [low, high];
}

/** A track's highest and lowest values, and the residues (from 1) they're at. */
export function extremes(
  values: Values,
): { highest: [number, number]; lowest: [number, number] } | null {
  let highest: [number, number] | null = null;
  let lowest: [number, number] | null = null;
  for (const [i, v] of values.entries()) {
    if (v === null) continue;
    if (!highest || v > highest[0]) highest = [v, i + 1];
    if (!lowest || v < lowest[0]) lowest = [v, i + 1];
  }
  return highest && lowest ? { highest, lowest } : null;
}
