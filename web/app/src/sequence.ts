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
  /** It goes on beyond `start`, or `end`, which is only as far as it's known: INSDC's `<`
   * and `>`, a partial end. */
  partialStart: boolean;
  partialEnd: boolean;
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
  /** Its parts are INSDC's `order()`: found in that order, with nothing said of joining
   * them, rather than a `join()`, one molecule. */
  ordered: boolean;
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
      partialStart: p["partial_start"] === true,
      partialEnd: p["partial_end"] === true,
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
        {
          start: from,
          end: to,
          strand: strand === "mixed" ? "none" : strand,
          between: false,
          partialStart: false,
          partialEnd: false,
        },
      ],
      ordered: f["operator"] === "order",
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

/** Whether a feature's ends go on beyond what's annotated (INSDC's `<` or `>`), so its
 * length is only as much of it as is known. */
export function isPartial(feature: Feature): boolean {
  return feature.parts.some((p) => p.partialStart || p.partialEnd);
}

/** "1..2686", "2601..120 (across the origin)", "join(2..4,10..12)", "order(2..4,10..12)",
 * "<2..>9", "5^6": a location as GenBank would write it. */
export function locationText(feature: Feature): string {
  const across = feature.end < feature.start ? " (across the origin)" : "";
  const [only] = feature.parts;
  if (feature.parts.length === 1 && only && !only.between && !isPartial(feature)) {
    return `${String(feature.start)}..${String(feature.end)}${across}`;
  }
  const strands = new Set(feature.parts.map((p) => p.strand));
  const reverse = strands.size === 1 && strands.has("reverse");
  // A reverse-strand feature's parts are listed last first; GenBank writes them in order
  // along the sequence, inside one complement().
  const listed = reverse ? [...feature.parts].reverse() : feature.parts;
  const text = listed.map((p) => {
    const at = p.between
      ? `${String(p.start)}^${String(p.end)}`
      : p.start === p.end && !(p.partialStart && p.partialEnd)
        ? `${p.partialStart ? "<" : p.partialEnd ? ">" : ""}${String(p.start)}`
        : `${p.partialStart ? "<" : ""}${String(p.start)}..${p.partialEnd ? ">" : ""}${String(p.end)}`;
    return p.strand === "reverse" && !reverse ? `complement(${at})` : at;
  });
  const operator = feature.ordered ? "order" : "join";
  const joined = text.length > 1 ? `${operator}(${text.join(",")})` : (text[0] ?? "");
  return `${reverse ? `complement(${joined})` : joined}${across}`;
}

/** How many bases it covers, as the feature table says it: "at least" that many if an end
 * goes on beyond what's annotated. */
export function lengthText(feature: Feature, length: number, format: Intl.NumberFormat): string {
  const counted = format.format(featureLength(feature, length));
  return isPartial(feature) ? `at least ${counted}` : counted;
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

// --- The two strands, and what a stretch of them is ---------------------------------------

/** Each IUPAC nucleotide code's complement (IUBMB, 1985): A pairs with T (U in RNA), C with
 * G, and each ambiguity code with the code for its bases' complements, so R (A or G) with Y
 * (C or T). S (C or G), W (A or T) and N are their own. */
const COMPLEMENTS: Record<string, string> = {
  A: "T",
  C: "G",
  G: "C",
  T: "A",
  U: "A",
  R: "Y",
  Y: "R",
  K: "M",
  M: "K",
  B: "V",
  V: "B",
  D: "H",
  H: "D",
  S: "S",
  W: "W",
  N: "N",
};

export type Alphabet = "dna" | "rna" | "protein";

/** The bases that pair with `bases`, base by base, in the same order and case: the other
 * strand read 3′ to 5′, as it's drawn under the first. RNA's A pairs with U. Anything that
 * isn't a nucleotide code (a gap, say) stays as it is. */
export function complement(bases: string, alphabet: Alphabet = "dna"): string {
  let out = "";
  for (const base of bases) {
    const upper = base.toUpperCase();
    let paired = COMPLEMENTS[upper] ?? base;
    if (alphabet === "rna" && paired === "T") paired = "U";
    out += base === upper ? paired : paired.toLowerCase();
  }
  return out;
}

/** The other strand, 5′ to 3′: what a primer that binds `bases` reads. */
export function reverseComplement(bases: string, alphabet: Alphabet = "dna"): string {
  const paired = complement(bases, alphabet);
  let out = "";
  for (let i = paired.length - 1; i >= 0; i--) out += paired.charAt(i);
  return out;
}

/** The bases GC content counts (G, C, and S, which is G or C), among those whose pairing is
 * known (A, C, G, T, U, S and W): as the DNA plugin's `compute_composition` counts them, so
 * the two never disagree. N and the other codes are left out of both. */
const GC = new Set("GCS");
const KNOWN = new Set("ACGTUSW");

/** GC content, as a percentage rounded to two places, or null if none of the bases is known
 * to pair as G or C or not. */
export function gcContent(bases: string): number | null {
  let known = 0;
  let gc = 0;
  for (const base of bases.toUpperCase()) {
    if (!KNOWN.has(base)) continue;
    known++;
    if (GC.has(base)) gc++;
  }
  if (!known) return null;
  return Math.round((10_000 * gc) / known) / 100;
}

/** SantaLucia's unified nearest-neighbour ΔH (kcal/mol) and ΔS (cal/K/mol), for each pair
 * of neighbours 5′→3′ on one strand (PNAS 95, 1460; 1998, table 2). */
const NEIGHBOURS: Record<string, [number, number]> = {
  AA: [-7.9, -22.2],
  AT: [-7.2, -20.4],
  TA: [-7.2, -21.3],
  CA: [-8.5, -22.7],
  GT: [-8.4, -22.4],
  CT: [-7.8, -21.0],
  GA: [-8.2, -22.2],
  CG: [-10.6, -27.2],
  GC: [-9.8, -24.4],
  GG: [-8.0, -19.9],
};
/** Initiation, by the base pair at each end. */
const INITIATION: Record<string, [number, number]> = {
  G: [0.1, -2.8],
  C: [0.1, -2.8],
  A: [2.3, 4.1],
  T: [2.3, 4.1],
};
const SYMMETRY: [number, number] = [0, -1.4];
const GAS_CONSTANT = 1.987; // cal/K/mol
/** 50 mM Na+, sodium alone: no Mg2+ or dNTPs. */
const SODIUM = 0.05;
/** SantaLucia's CT, all the strands together (mol/L): 25 nM each of a strand and its
 * complement, or 50 nM of one that pairs with itself. */
const STRANDS = 50e-9;

/** The melting temperature (°C, to one place) of `bases` with their complement, as the DNA
 * plugin's primers have it: SantaLucia's unified nearest-neighbour parameters (1998) with
 * his salt correction, at 50 mM Na+ alone and 50 nM of strands in all (his equation 3).
 * Null for fewer than two bases, or any but A, C, G and T. */
export function meltingTemperature(bases: string): number | null {
  const upper = bases.toUpperCase();
  if (upper.length < 2 || !/^[ACGT]+$/.test(upper)) return null;
  const first = INITIATION[upper[0] ?? ""] ?? [0, 0];
  const last = INITIATION[upper.at(-1) ?? ""] ?? [0, 0];
  let enthalpy = first[0] + last[0];
  let entropy = first[1] + last[1];
  for (let i = 0; i < upper.length - 1; i++) {
    const pair = upper.slice(i, i + 2);
    const [dh, ds] = NEIGHBOURS[pair] ?? NEIGHBOURS[reverseComplement(pair)] ?? [0, 0];
    enthalpy += dh;
    entropy += ds;
  }
  let strands = STRANDS / 4;
  if (upper === reverseComplement(upper)) {
    enthalpy += SYMMETRY[0];
    entropy += SYMMETRY[1];
    strands = STRANDS;
  }
  entropy += 0.368 * (upper.length - 1) * Math.log(SODIUM);
  const kelvin = (1000 * enthalpy) / (entropy + GAS_CONSTANT * Math.log(strands));
  return Math.round((kelvin - 273.15) * 10) / 10;
}

/** The longest stretch a selection's Tm is given for: the longest primer the DNA plugin
 * takes. A nearest-neighbour Tm is a two-state model of an oligo melting from its duplex;
 * a longer stretch melts in domains, which it doesn't describe. */
export const LONGEST_TM = 200;

// --- The caret and the selection -----------------------------------------------------------

/**
 * Where the caret is and what's selected, as a text field has them: places between bases,
 * from 0 (before the first) to the length (after the last). The selection runs between
 * `anchor`, where it started, and `caret`, where it's been extended to; it's empty when
 * they're the same place. On a circle, a selection can run from `anchor` on round the
 * origin to `caret` (`across`), as a site found across the origin does.
 *
 * A place at the end of a row is also the start of the next, so, as a wrapped text field
 * does, the caret shows at the start of the next row unless it went to the end of its own
 * (`atEnd`): with End, say.
 */
export interface Caret {
  anchor: number;
  caret: number;
  across: boolean;
  atEnd?: boolean;
}

export const AT_START: Caret = { anchor: 0, caret: 0, across: false };

/** The bases selected, from 1, inclusive: an end before the start runs across the origin.
 * Null when nothing is. */
export function selected(at: Caret): [number, number] | null {
  if (at.across) return [at.anchor + 1, at.caret];
  if (at.anchor === at.caret) return null;
  return [Math.min(at.anchor, at.caret) + 1, Math.max(at.anchor, at.caret)];
}

/** The row (from 0) the caret shows in. */
export function caretRow(at: Caret, length: number): number {
  const last = Math.max(Math.ceil(length / ROW) - 1, 0);
  const row = at.atEnd && at.caret > 0 ? at.caret / ROW - 1 : Math.floor(at.caret / ROW);
  return Math.min(Math.max(Math.floor(row), 0), last);
}

/** How many bases a stretch from `start` to `end` (from 1, inclusive) holds, round the
 * origin if `end` is before `start`. */
export function stretchLength(start: number, end: number, length: number): number {
  return end >= start ? end - start + 1 : length - start + 1 + end;
}

/** The residues from `start` to `end`, from 1, inclusive, round the origin if `end` is
 * before `start`. */
export function residuesOf(sequence: string, start: number, end: number): string {
  return end >= start
    ? sequence.slice(start - 1, end)
    : sequence.slice(start - 1) + sequence.slice(0, end);
}

/** A stretch's ranges of bases, in order along the sequence: one, or two across the origin.
 * For drawing, a row at a time. */
export function rangesOf(start: number, end: number, length: number): [number, number][] {
  return stretches(start, end, length);
}

export interface Key {
  key: string;
  shiftKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
}

/** What a key does to the caret, as in a text field: the arrows move it a residue, or a row
 * up or down; Home and End go to its row's start and end (⌘ with the left and right arrows
 * too, as on a Mac), and with Ctrl (or ⌘) to the sequence's; Page Up and Page Down go
 * `page` rows; Ctrl (⌥ on a Mac) with the left and right arrows goes to the start of the
 * group of ten before or after. With Shift, each extends the selection from its anchor
 * instead of moving both; without it, the left and right arrows put the caret at the
 * selection's start or end, as a text field does. Ctrl+A (⌘A) selects it all. Null for any
 * other key. */
export function moved(at: Caret, key: Key, length: number, page: number): Caret | null {
  const command = key.ctrlKey || key.metaKey;
  if (command && !key.shiftKey && !key.altKey && key.key.toLowerCase() === "a") {
    return { anchor: 0, caret: length, across: false, atEnd: true };
  }
  const from = at.caret;
  const span = selected(at);
  const row = caretRow(at, length);
  let name = key.key;
  if (key.metaKey && name === "ArrowLeft") name = "Home";
  else if (key.metaKey && name === "ArrowRight") name = "End";
  const whole = command && (name === "Home" || name === "End") && !key.key.startsWith("Arrow");
  let to: number;
  let atEnd = false;
  switch (name) {
    case "ArrowLeft":
      if (key.altKey || key.ctrlKey) to = Math.ceil(from / GROUP) * GROUP - GROUP;
      else if (span && !key.shiftKey) to = at.across ? at.anchor : span[0] - 1;
      else to = from - 1;
      break;
    case "ArrowRight":
      if (key.altKey || key.ctrlKey) to = Math.floor(from / GROUP) * GROUP + GROUP;
      else if (span && !key.shiftKey) to = at.across ? at.caret : span[1];
      else to = from + 1;
      break;
    case "ArrowUp":
      to = from - ROW;
      break;
    case "ArrowDown":
      to = from + ROW;
      break;
    case "PageUp":
      to = from - page * ROW;
      break;
    case "PageDown":
      to = from + page * ROW;
      break;
    case "Home":
      to = whole ? 0 : row * ROW;
      break;
    case "End":
      to = whole ? length : Math.min((row + 1) * ROW, length);
      atEnd = true;
      break;
    default:
      return null;
  }
  to = Math.min(Math.max(to, 0), length);
  // At the sequence's end there's no next row to show it at the start of.
  if (to === length) atEnd = true;
  // Extending a selection across the origin starts it again from its anchor, along the
  // sequence.
  return key.shiftKey
    ? { anchor: at.anchor, caret: to, across: false, atEnd }
    : { anchor: to, caret: to, across: false, atEnd };
}

/** The place between bases nearest `column` of a row starting at `rowStart` (from 1),
 * columns counting the gap after each group of ten, as `columnOf` lays them out. A place
 * in a gap is the end of the group before it. */
export function placeAt(column: number, rowStart: number, length: number): number {
  const rowEnd = Math.min(rowStart + ROW - 1, length);
  const group = Math.floor(Math.max(column, 0) / (GROUP + 1));
  const within = Math.min(Math.max(column, 0) - group * (GROUP + 1), GROUP);
  const offset = group * GROUP + Math.round(within);
  return Math.min(rowStart - 1 + offset, rowEnd);
}

// --- Finding a motif ------------------------------------------------------------------------

/** The bases each IUPAC nucleotide code stands for (IUBMB, 1985), U read as T. */
const STANDS_FOR: Record<string, string> = {
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

/** The amino acids each ambiguous code stands for (IUPAC-IUB): B is D or N, Z is E or Q,
 * J is I or L, and X any; others stand for themselves. */
const RESIDUES_FOR: Record<string, string> = {
  B: "BDN",
  Z: "EQZ",
  J: "IJL",
};
const AMINO_ACIDS = "ACDEFGHIKLMNPQRSTVWYUOBZJX*";

/** The longest motif looked for: a primer is at most 200 bases, and a longer search is a
 * job for an alignment. */
export const LONGEST_MOTIF = 200;
/** The most matches found: past this many, the motif says too little to step through. */
export const MOST_MATCHES = 10_000;

/** A motif as typed, read: its residues, upper case (spaces and numbers, as a pasted GenBank
 * or FASTA has, dropped), or why it can't be looked for. */
export function readMotif(
  text: string,
  alphabet: Alphabet,
): { motif: string } | { error: string } | null {
  const motif = text.replace(/[\s\d]/g, "").toUpperCase();
  if (!motif) return null;
  const allowed = alphabet === "protein" ? AMINO_ACIDS : Object.keys(STANDS_FOR).join("");
  const wrong = [...new Set(motif)].filter((c) => !allowed.includes(c));
  if (wrong.length) {
    const one = wrong.length === 1;
    const listed = one
      ? wrong.join("")
      : `${wrong.slice(0, -1).join(", ")} and ${wrong.at(-1) ?? ""}`;
    const what =
      alphabet === "protein"
        ? one
          ? "isn't an amino acid's one-letter code"
          : "aren't amino acids' one-letter codes"
        : one
          ? "isn't an IUPAC nucleotide code"
          : "aren't IUPAC nucleotide codes";
    return { error: `${listed} ${what}.` };
  }
  if (motif.length > LONGEST_MOTIF) {
    return { error: `That's ${String(motif.length)} long: at most ${String(LONGEST_MOTIF)}.` };
  }
  return { motif };
}

/** What a motif's code matches in the sequence: each code there whose residues are all ones
 * the motif's code allows, so the motif's N matches an A or an R, and its A matches an A
 * but not an N, which only might be one. */
function classOf(code: string, alphabet: Alphabet): string {
  if (alphabet === "protein") {
    if (code === "X") return "[A-Z*]";
    if (code === "*") return "\\*";
    return `[${RESIDUES_FOR[code] ?? code}]`;
  }
  const allowed = STANDS_FOR[code] ?? "";
  const matched = Object.entries(STANDS_FOR)
    .filter(([, bases]) => Array.from(bases, (b) => allowed.includes(b)).every(Boolean))
    .map(([letter]) => letter);
  return `[${matched.join("")}]`;
}

export interface Match {
  /** From 1, inclusive; an end before the start runs across a circle's origin. */
  start: number;
  end: number;
  /** The strand it reads on, 5′ to 3′: both, for a motif that's its own reverse complement
   * (a palindromic site such as EcoRI's GAATTC). */
  strand: "forward" | "reverse" | "both";
}

/** Where a motif is along a sequence: overlapping matches each count; on both strands if
 * asked (the reverse strand's are where the motif's reverse complement is on the forward
 * one); and across the origin of a circle if asked. In order along the sequence. At most
 * `MOST_MATCHES`, and whether there were more. */
export function findMotif(
  sequence: string,
  motif: string,
  options: { alphabet: Alphabet; bothStrands: boolean; circular: boolean },
): { matches: Match[]; more: boolean } {
  const length = sequence.length;
  const { alphabet } = options;
  if (!motif || motif.length > length) return { matches: [], more: false };
  const upper = sequence.toUpperCase();
  // Round the origin, the motif's length less one more of its start.
  const read = options.circular ? upper + upper.slice(0, motif.length - 1) : upper;
  const found = new Map<number, Match>();
  let more = false;
  const scan = (wanted: string, strand: "forward" | "reverse") => {
    const pattern = Array.from(wanted, (c) => classOf(c, alphabet)).join("");
    const expression = new RegExp(`(?=${pattern})`, "g");
    for (const hit of read.matchAll(expression)) {
      if (hit.index >= length) break;
      const start = hit.index + 1;
      const was = found.get(start);
      if (was) {
        if (was.strand !== strand) was.strand = "both";
        continue;
      }
      if (found.size >= MOST_MATCHES) {
        more = true;
        break;
      }
      const end = ((hit.index + motif.length - 1) % length) + 1;
      found.set(start, { start, end, strand });
    }
  };
  scan(motif, "forward");
  if (options.bothStrands && alphabet !== "protein") {
    scan(reverseComplement(motif, alphabet), "reverse");
  }
  const matches = [...found.values()].sort((a, b) => a.start - b.start);
  return { matches, more };
}

/** The match a step from the caret goes to: the first starting after `place` (a place
 * between bases), or, going back, the last starting before it; round past either end, as
 * find on a page does. Its index, and whether it went round. */
export function stepTo(
  matches: Match[],
  place: number,
  direction: 1 | -1,
): { index: number; wrapped: boolean } | null {
  if (!matches.length) return null;
  if (direction === 1) {
    const index = matches.findIndex((m) => m.start - 1 > place);
    return index < 0 ? { index: 0, wrapped: true } : { index, wrapped: false };
  }
  let index = -1;
  for (const [i, m] of matches.entries()) if (m.start - 1 < place) index = i;
  return index < 0 ? { index: matches.length - 1, wrapped: true } : { index, wrapped: false };
}
