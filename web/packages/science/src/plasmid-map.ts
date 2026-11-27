import { rangesOf, stretchLength } from "./sequence.js";

/** Coordinates are one-based, inclusive. An end before the start crosses the origin. */
export interface MapSpan {
  start: number;
  end: number;
  /** `none` has no strand, and `mixed` has parts on both: neither is drawn with an
   * arrowhead. Unsaid, it's forward. */
  strand?: "forward" | "reverse" | "none" | "mixed";
  qualifiers?: { label?: string; role?: string; identity?: string };
}
export interface MapFeature extends MapSpan {
  type: string;
  name: string;
}
export interface MapOrf extends MapSpan {
  frame: number;
  length: number;
  amino_acids: number;
  start_codon?: string;
  translation?: string;
}
export interface MapBindingSite extends MapSpan {
  primer: string;
  name: string;
  three_prime: number;
  annealed: number;
  tail: number;
  melting_temperature?: number | null;
  mismatches?: { primer: string; template: string; from_three_prime: number }[];
}

/** A difference of a sequenced consensus from the plasmid's record, as HGVS names it: its
 * reference bases from `start` to `end` (an insertion's are the two bases either side). */
export interface MapDifference extends MapSpan {
  hgvs: string;
  kind:
    "substitution" | "deletion" | "insertion" | "duplication" | "inversion" | "deletion_insertion";
  /** The bases it replaces and those it has, each at most its first 200: a long change's
   * are only a preview, and its lengths are what it is. */
  reference: string;
  observed: string;
  length_change: number;
  /** How many bases it replaces, and how many it has, in full. */
  reference_length: number;
  observed_length: number;
  homopolymer?: number;
  features?: readonly string[];
  depth?: number;
  support?: number;
  forward?: number;
  reverse?: number;
  fraction?: number;
}

/** The depth of reads along the plasmid, in bins of `bin_size` bases from base 1: each
 * bin's mean and least depth. */
export interface MapCoverage {
  bin_size: number;
  mean: readonly number[];
  min: readonly number[];
  /** The fewest reads a base needs to be called; under it, the stretches in `low`. */
  minDepth?: number;
  meanDepth?: number;
  leastDepth?: number;
  low?: readonly MapSpan[] | undefined;
  low_count?: number;
  source?: string;
}

export interface MapAnnotation<T extends MapSpan> {
  items: readonly T[];
  source?: string;
  /** The molecule's own features, rather than the parts a library found along it. */
  own?: boolean;
}

export interface PlasmidMapData {
  name: string;
  length: number;
  topology: "linear" | "circular";
  parts?: MapAnnotation<MapFeature>;
  orfs?: MapAnnotation<MapOrf>;
  sites?: MapAnnotation<MapBindingSite>;
  /** Sequencing's evidence along the plasmid: the depth of reads, and the consensus's
   * differences from the record. */
  coverage?: MapCoverage;
  differences?: MapAnnotation<MapDifference>;
  /** Only IDs offered here become buttons in the binding-site table. */
  linkedPrimerIds?: readonly string[];
  emptyMessage?: string;
  emptyAction?: string;
  /** What else to say beneath the description, a paragraph each. */
  notes?: readonly string[];
}
type View = "line" | "ring";
type LayerKey = "parts" | "orfs" | "primers" | "coverage" | "differences";
//: Layers drawn as tracks of their own rather than as parts on lanes.
const TRACKS: readonly LayerKey[] = ["coverage", "differences"];
type Interval = [number, number];
interface Layer {
  key: LayerKey;
  label: string;
  colour: string | null;
  items: MapSpan[];
  count: number;
  /** What its button says, where a count alone wouldn't. */
  button?: string;
  shown: boolean;
  describe: () => string;
  table: () => HTMLTableElement;
}
const STYLE = `
  :host { display: block; color: CanvasText; background: Canvas; padding: 1rem; border-radius: 8px;
    font: 14px/1.45 system-ui, sans-serif; }
  h2 { font-size: 1rem; margin: 0 0 0.5rem; }
  p { margin: 0 0 0.75rem; max-width: 70ch; }
  /* A name with no break in it, such as a long insertion's HGVS name, wraps where it must
     rather than pushing the page wider than a phone, whatever the font. Tables scroll in
     their own container instead. */
  p, dd, caption { overflow-wrap: anywhere; }
  .map { display: flex; flex-wrap: wrap; gap: 1.5rem; align-items: flex-start; }
  svg { flex: none; max-width: 100%; height: auto; }
  .ring { fill: none; stroke: color-mix(in srgb, CanvasText 18%, Canvas); }
  .tick { stroke: color-mix(in srgb, CanvasText 28%, Canvas); }
  .tick-label, .middle { fill: color-mix(in srgb, CanvasText 70%, Canvas); font-size: 10px; }
  .middle.name { fill: CanvasText; font-size: 13px; font-weight: 600; }
  .arc { fill: none; stroke-linecap: butt; }
  .tables { max-width: 100%; overflow-x: auto; }
  table { border-collapse: collapse; font-size: 0.85rem; min-width: 22rem; }
  caption { text-align: left; font-weight: 600; padding-bottom: 0.35rem; }
  th, td { text-align: left; padding: 0.2rem 0.6rem 0.2rem 0; vertical-align: top;
    border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, Canvas); }
  th { font-weight: 600; }
  td.where { font-variant-numeric: tabular-nums; white-space: nowrap; }
  .swatch { display: inline-block; width: 0.7rem; height: 0.7rem; border-radius: 2px;
    margin-right: 0.4rem; vertical-align: -1px; }
  .views { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0 0 0.75rem; }
  .views button { font: inherit; color: CanvasText; background: Canvas; cursor: pointer;
    padding: 0.25rem 0.75rem; border: 1px solid color-mix(in srgb, CanvasText 40%, Canvas);
    border-radius: 999px; }
  .views button[aria-pressed="true"] { background: color-mix(in srgb, CanvasText 12%, Canvas);
    border-color: CanvasText; font-weight: 600; }
  .views button:focus-visible { outline: 2px solid CanvasText; outline-offset: 2px; }
  .line { stroke: color-mix(in srgb, CanvasText 18%, Canvas); }
  .cap { stroke: color-mix(in srgb, CanvasText 55%, Canvas); }
  .end-label { fill: color-mix(in srgb, CanvasText 70%, Canvas); font-size: 10px; }
  dl { display: grid; grid-template-columns: minmax(0, max-content) minmax(0, 1fr); gap: 0.25rem 1rem;
    margin: 0.75rem 0 0; }
  dt { font-weight: 600; }
  dd { margin: 0; }
  .note { color: color-mix(in srgb, CanvasText 70%, Canvas); }
  .run { font: inherit; color: Canvas; background: CanvasText; cursor: pointer;
    padding: 0.375rem 0.875rem; border: 1px solid CanvasText; border-radius: 6px; }
  .run:focus-visible { outline: 2px solid CanvasText; outline-offset: 2px; }
  .link { font: inherit; color: CanvasText; background: none; border: 0; padding: 0;
    text-decoration: underline; cursor: pointer; text-align: left; }
  .link:focus-visible { outline: 2px solid CanvasText; outline-offset: 2px; }
  .hgvs { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.8rem;
    word-break: break-all; min-width: 9rem; max-width: 18rem; }
  .depth { stroke: none; fill-opacity: 0.3; }
  .depth-edge { fill: none; stroke-width: 1.5; stroke-linejoin: round; }
  .threshold { fill: none; stroke-dasharray: 3 3; stroke-width: 1; }
  .low { fill: none; stroke-dasharray: 4 3; }
  td.protein { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.8rem;
    word-break: break-all; max-width: 22rem; }
`;

// A colour per role, each readable on Canvas in both light and dark, and each part also
// named in the table, so the map never tells anything by colour alone.
const COLOURS: Record<string, [string, string]> = {
  origin: ["#1f6feb", "#6cb0ff"],
  resistance: ["#8250df", "#c297ff"],
  selection: ["#bc4c00", "#ff9c6b"],
  promoter: ["#0e7c66", "#4fd1b0"],
  terminator: ["#57606a", "#9aa4b1"],
  tag: ["#9a6700", "#e3b341"],
  viral: ["#a40e26", "#ff8095"],
  recombination: ["#6e4b00", "#d4a72c"],
  regulatory: ["#4f4f8f", "#a9a9e6"],
  orf: ["#3f6212", "#a3e635"],
  primer: ["#9f1239", "#fda4af"],
  depth: ["#0550ae", "#79c0ff"],
  difference: ["#cf222e", "#ff7b72"],
  low: ["#57606a", "#c9d1d9"],
};
const ROLES: Record<string, string> = {
  origin: "Origin of replication",
  resistance: "Bacterial resistance",
  selection: "Selection marker",
  promoter: "Promoter",
  terminator: "Terminator",
  tag: "Tag",
  viral: "Viral element",
  recombination: "Recombination site",
  regulatory: "Regulatory",
};

const SIZE = 420;
const CENTRE = SIZE / 2;
const RADIUS = 140;
//: The rings the ORFs and the primer binding sites are drawn on, inside and outside the
//: backbone, so a layer is never read as another.
const ORF_RINGS = [RADIUS - 52, RADIUS - 72];
const SITE_RING = RADIUS + 52;
const SVG = "http://www.w3.org/2000/svg";

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attributes: Record<string, string> = {},
  ...children: (Node | string)[]
) {
  const out = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "className") out.className = value;
    else out.setAttribute(name, value);
  }
  out.append(...children);
  return out;
}

function svg<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attributes: Record<string, string | number> = {},
  ...children: (Node | string)[]
) {
  const out = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attributes)) out.setAttribute(name, String(value));
  out.append(...children);
  return out;
}

function dark() {
  return (
    typeof globalThis.matchMedia === "function" &&
    globalThis.matchMedia("(prefers-color-scheme: dark)").matches
  );
}

function colourOf(role?: string) {
  const pair = COLOURS[role ?? ""] ?? ["#57606a", "#9aa4b1"];
  return dark() ? pair[1] : pair[0];
}

/** Where base `at` of a plasmid `length` long lies, `radius` out from the centre: base 1
 * at the top, going clockwise, as a plasmid map is drawn. */
function point(at: number, length: number, radius: number): Interval {
  const angle = (2 * Math.PI * ((at - 1) % length)) / length - Math.PI / 2;
  return [CENTRE + radius * Math.cos(angle), CENTRE + radius * Math.sin(angle)];
}

/** The bases a feature covers, which wraps the origin when its end is before its start. */
function spanOf(feature: MapSpan, length: number) {
  return stretchLength(feature.start, feature.end, length);
}

/** Whether a strand has a direction to draw: not `none`, nor `mixed`. */
function directed(strand?: MapSpan["strand"]) {
  return strand !== "none" && strand !== "mixed";
}

/** A strand in words: as a table writes it, and as a sentence does. */
function strandWord(strand?: MapSpan["strand"]) {
  return strand === "reverse"
    ? "reverse"
    : strand === "none"
      ? "neither"
      : strand === "mixed"
        ? "both"
        : "forward";
}
function onStrand(strand?: MapSpan["strand"]) {
  return strand === "none"
    ? "on neither strand"
    : strand === "mixed"
      ? "on both strands"
      : `on the ${strandWord(strand)} strand`;
}

/** An arc from base `from` for `span` bases, as an SVG path, with an arrowhead at the end
 * it runs to. */
function arc(
  from: number,
  span: number,
  length: number,
  radius: number,
  strand?: MapSpan["strand"],
): [string, string] {
  const head = directed(strand) ? Math.min(span / 2, (14 * length) / (2 * Math.PI * radius)) : 0;
  const forward = strand !== "reverse";
  const bodyFrom = forward ? from : from + head;
  const bodySpan = span - head;
  const [sx, sy] = point(bodyFrom, length, radius);
  const [ex, ey] = point(bodyFrom + bodySpan, length, radius);
  const sweep = bodySpan / length > 0.5 ? 1 : 0;
  const body = `M ${sx.toFixed(2)} ${sy.toFixed(2)} A ${String(radius)} ${String(radius)} 0 ${String(sweep)} 1 ${ex.toFixed(2)} ${ey.toFixed(2)}`;
  const tipAt = forward ? from + span : from;
  const [tx, ty] = point(tipAt, length, radius);
  const [bx, by] = point(forward ? from + bodySpan : from + head, length, radius - 5);
  const [cx, cy] = point(forward ? from + bodySpan : from + head, length, radius + 5);
  const head3 = `M ${bx.toFixed(2)} ${by.toFixed(2)} L ${tx.toFixed(2)} ${ty.toFixed(2)} L ${cx.toFixed(2)} ${cy.toFixed(2)} Z`;
  return [body, head ? head3 : ""];
}

/** Ticks every 500, 1,000, 2,000 bases and so on: the first step that gives at most ten
 * of them. */
function tickEvery(length: number) {
  for (const step of [500, 1000, 2000, 5000, 10000, 20000, 50000]) {
    if (length / step <= 10) return step;
  }
  return 100000;
}

/** A tick's label: base 1, then thousands, to as many decimals as the step needs, so a
 * tick every 500 bases reads 0.5k, 1k, 1.5k rather than 1k, 1k, 2k. */
function tickLabel(at: number, step: number) {
  if (at === 1) return "1";
  const thousands = (at - 1) / 1000;
  return `${thousands.toFixed(step < 1000 ? 1 : 0).replace(/\.0$/, "")}k`;
}

/** The depth of reads round the ring, as a histogram outwards from `inner` up to
 * `inner + height` at the deepest bin, with the minimum depth as a dashed circle. */
function depthRing(coverage: MapCoverage, length: number, inner: number, height: number) {
  const deepest = Math.max(1, ...coverage.mean, coverage.minDepth ?? 0);
  const outline: string[] = [];
  coverage.mean.forEach((depth, i) => {
    const from = i * coverage.bin_size + 1;
    const to = Math.min((i + 1) * coverage.bin_size, length) + 1;
    const radius = inner + (height * depth) / deepest;
    for (const at of [from, to]) {
      const [x, y] = point(at, length, radius);
      outline.push(`${x.toFixed(2)} ${y.toFixed(2)}`);
    }
  });
  const [sx, sy] = point(1, length, inner);
  const [hx, hy] = point(1 + length / 2, length, inner);
  const r = String(inner);
  const base = `M ${sx.toFixed(2)} ${sy.toFixed(2)} A ${r} ${r} 0 1 0 ${hx.toFixed(2)} ${hy.toFixed(2)} A ${r} ${r} 0 1 0 ${sx.toFixed(2)} ${sy.toFixed(2)} Z`;
  const drawn: SVGElement[] = [
    svg("path", {
      class: "depth",
      d: `M ${outline.join(" L ")} Z ${base}`,
      fill: colourOf("depth"),
      "fill-rule": "evenodd",
    }),
    svg("path", {
      class: "depth-edge",
      d: `M ${outline.join(" L ")} Z`,
      stroke: colourOf("depth"),
    }),
  ];
  if (coverage.minDepth) {
    drawn.push(
      svg("circle", {
        class: "threshold",
        cx: CENTRE,
        cy: CENTRE,
        r: (inner + (height * coverage.minDepth) / deepest).toFixed(2),
        stroke: colourOf("low"),
      }),
    );
  }
  return drawn;
}

/** A mark for each difference: a line across the backbone and a diamond outside it, so
 * one base among thousands still shows. */
function differenceMarks(differences: MapDifference[], length: number) {
  const drawn: SVGElement[] = [];
  for (const difference of differences) {
    const at = difference.start + spanOf(difference, length) / 2;
    const [ix, iy] = point(at, length, RADIUS - 12);
    const [ox, oy] = point(at, length, RADIUS + 12);
    const [dx, dy] = point(at, length, RADIUS + 44);
    const colour = colourOf("difference");
    drawn.push(
      svg("line", { x1: ix, y1: iy, x2: ox, y2: oy, stroke: colour, "stroke-width": 2.5 }),
      svg("path", {
        d: `M ${dx.toFixed(2)} ${(dy - 5).toFixed(2)} l 5 5 l -5 5 l -5 -5 Z`,
        fill: colour,
      }),
    );
  }
  return drawn;
}

function drawing(
  length: number,
  features: MapSpan[],
  orfs: MapSpan[],
  sites: MapSpan[],
  coverage?: MapCoverage,
  differences: MapDifference[] = [],
) {
  const parts = svg("svg", {
    viewBox: `0 0 ${String(SIZE)} ${String(SIZE)}`,
    width: SIZE,
    height: SIZE,
    // Hidden from screen readers: the paragraph above it says what it draws, in words,
    // and the table below it gives every part.
    "aria-hidden": "true",
  });
  parts.append(
    svg("circle", { class: "ring", cx: CENTRE, cy: CENTRE, r: RADIUS, "stroke-width": 8 }),
  );
  // The stretches under the minimum depth, dashed over the backbone they leave uncalled.
  for (const low of coverage?.low ?? []) {
    const span = spanOf(low, length);
    const [body] = arc(low.start, span, length, RADIUS, "none");
    parts.append(
      svg("path", { class: "low", d: body, stroke: colourOf("low"), "stroke-width": 8 }),
    );
  }
  // Inside the parts' inner rings, and outside the name in the middle.
  if (coverage) parts.append(...depthRing(coverage, length, 64, 34));
  const step = tickEvery(length);
  for (let at = 1; at < length; at += step) {
    const [ix, iy] = point(at, length, RADIUS - 8);
    const [ox, oy] = point(at, length, RADIUS + 8);
    const [lx, ly] = point(at, length, RADIUS + 22);
    parts.append(svg("line", { class: "tick", x1: ix, y1: iy, x2: ox, y2: oy, "stroke-width": 1 }));
    const label = svg("text", {
      class: "tick-label",
      x: lx.toFixed(1),
      y: ly.toFixed(1),
      "text-anchor": "middle",
      "dominant-baseline": "middle",
    });
    label.textContent = tickLabel(at, step);
    parts.append(label);
  }
  // Features on rings outside and inside the backbone, so overlapping ones stay apart:
  // each takes the first ring that's free where it lies.
  const rings = [RADIUS + 14, RADIUS + 34, RADIUS - 14, RADIUS - 34];
  const taken = rings.map((): Interval[] => []);
  for (const feature of features) {
    const span = spanOf(feature, length);
    const ring = laneFor(taken, [feature.start, feature.start + span - 1]);
    const colour = colourOf(feature.qualifiers?.role);
    const [body, head] = arc(feature.start, span, length, rings[ring] ?? RADIUS, feature.strand);
    parts.append(svg("path", { class: "arc", d: body, stroke: colour, "stroke-width": 7 }));
    parts.append(svg("path", { d: head, fill: colour }));
  }
  // The ORFs, on rings of their own inside the backbone, and the binding sites on one
  // outside it: each layer apart, each pointing the way it is read. The depth of reads
  // takes the ORFs' rings where it's drawn, so they go further in.
  const orfRings = coverage ? ORF_RINGS.map((r) => r - 30) : ORF_RINGS;
  const orfLanes = orfRings.map((): Interval[] => []);
  for (const orf of orfs) {
    const span = spanOf(orf, length);
    const ring = laneFor(orfLanes, [orf.start, orf.start + span - 1]);
    const [body, head] = arc(orf.start, span, length, orfRings[ring] ?? RADIUS, orf.strand);
    parts.append(
      svg("path", { class: "arc", d: body, stroke: colourOf("orf"), "stroke-width": 5 }),
    );
    parts.append(svg("path", { d: head, fill: colourOf("orf") }));
  }
  for (const site of sites) {
    // A primer is 20 or so bases of a plasmid's thousands, so a bare arc would be a
    // point: each site is drawn as a mark across the ring with the arrowhead that says
    // which way its polymerase goes.
    const span = Math.max(spanOf(site, length), Math.ceil(length / 120));
    const [body, head] = arc(site.start, span, length, SITE_RING, site.strand);
    parts.append(
      svg("path", { class: "arc", d: body, stroke: colourOf("primer"), "stroke-width": 6 }),
    );
    parts.append(svg("path", { d: head, fill: colourOf("primer") }));
  }
  parts.append(...differenceMarks(differences, length));
  return parts;
}

// The linear drawing: the molecule along a line from base 1 on the left, parts on lanes
// above it where they run forward and below where they run reverse, as SnapGene's linear
// view has them.
const WIDE = 680;
const LEFT = 36;
const RIGHT = WIDE - 36;
const LANES = 3;
const LANE = 18;
//: Each layer drawn takes its own lanes on each side of the line, so the molecule's own
//: height follows from how many layers are showing.
function spineAt(layers: number) {
  return layers * LANES * LANE + 26;
}

function tallAt(layers: number) {
  return spineAt(layers) + layers * LANES * LANE + 30;
}

/** Where base `at` lies along the line. */
function along(at: number, length: number) {
  const share = length > 1 ? (at - 1) / (length - 1) : 0;
  return LEFT + (RIGHT - LEFT) * Math.min(Math.max(share, 0), 1);
}

/** The stretches a part covers along the molecule: one, or two where it crosses a
 * circle's origin, which a linear molecule's parts never do. */
function piecesOf(feature: MapSpan, length: number): Interval[] {
  return rangesOf(feature.start, feature.end, length);
}

/** A lane for a part: the first one free where it lies, so overlapping parts stay apart. */
function laneFor(taken: Interval[][], covers: Interval) {
  for (const [i, lane] of taken.entries()) {
    if (!lane.some(([a, b]) => covers[0] <= b && a <= covers[1])) {
      lane.push(covers);
      return i;
    }
  }
  taken.at(-1)?.push(covers);
  return taken.length - 1;
}

/** One stretch of a part as a bar along the line, with an arrowhead where the part's 3'
 * end is: on the right running forward, on the left running reverse. */
function bar(
  from: number,
  to: number,
  length: number,
  y: number,
  forward: boolean,
  tipped: boolean,
) {
  const x1 = along(from, length);
  const x2 = along(to, length);
  const head = Math.min(7, Math.max(x2 - x1, 1));
  const body: Interval = tipped ? (forward ? [x1, x2 - head] : [x1 + head, x2]) : [x1, x2];
  const parts: SVGElement[] = [
    svg("line", {
      class: "arc",
      x1: body[0].toFixed(2),
      y1: y,
      x2: Math.max(body[1], body[0]).toFixed(2),
      y2: y,
      "stroke-width": 9,
    }),
  ];
  if (tipped) {
    const tip = forward ? x2 : x1;
    const back = forward ? x2 - head : x1 + head;
    parts.push(
      svg("path", {
        d: `M ${back.toFixed(2)} ${String(y - 6)} L ${tip.toFixed(2)} ${String(y)} L ${back.toFixed(2)} ${String(y + 6)} Z`,
      }),
    );
  }
  return parts;
}

/** The line view: the molecule along a line from base 1 on the left, each layer's parts
 * on lanes of its own -- above the line where they run forward and below where they run
 * reverse, as SnapGene's linear view has them. */
function lineDrawing(
  length: number,
  layers: Layer[],
  circular: boolean,
  coverage?: MapCoverage,
  differences: MapDifference[] = [],
) {
  const shown = layers.filter((layer) => layer.items.length > 0 && !TRACKS.includes(layer.key));
  const count = Math.max(shown.length, 1);
  // The depth of reads above everything, then a row for the differences' marks.
  const depthTop = 26;
  const depthHeight = 40;
  const marksAt = (coverage ? depthTop + depthHeight + 14 : 26) + 4;
  const above = (coverage ? depthHeight + 14 : 0) + (differences.length ? 20 : 0);
  const spine = spineAt(count) + above;
  const tall = tallAt(count) + above;
  const parts = svg("svg", {
    viewBox: `0 0 ${String(WIDE)} ${String(tall)}`,
    width: WIDE,
    height: tall,
    // Hidden from screen readers: the paragraph above it says what it draws, in words,
    // and the tables below it give every part of every layer.
    "aria-hidden": "true",
  });
  parts.append(
    svg("line", { class: "line", x1: LEFT, y1: spine, x2: RIGHT, y2: spine, "stroke-width": 8 }),
  );
  for (const low of coverage?.low ?? []) {
    for (const [from, to] of piecesOf(low, length)) {
      parts.append(
        svg("line", {
          class: "low",
          x1: along(from, length).toFixed(2),
          y1: spine,
          x2: along(to, length).toFixed(2),
          y2: spine,
          stroke: colourOf("low"),
          "stroke-width": 8,
        }),
      );
    }
  }
  if (coverage) {
    const deepest = Math.max(1, ...coverage.mean, coverage.minDepth ?? 0);
    const floor = depthTop + depthHeight;
    const outline = coverage.mean.flatMap((depth, i) => {
      const y = (floor - (depthHeight * depth) / deepest).toFixed(2);
      const from = along(i * coverage.bin_size + 1, length).toFixed(2);
      const to = along(Math.min((i + 1) * coverage.bin_size, length), length).toFixed(2);
      return [`${from} ${y}`, `${to} ${y}`];
    });
    parts.append(
      svg("path", {
        class: "depth",
        d: `M ${String(LEFT)} ${String(floor)} L ${outline.join(" L ")} L ${String(RIGHT)} ${String(floor)} Z`,
        fill: colourOf("depth"),
      }),
      svg("path", {
        class: "depth-edge",
        d: `M ${outline.join(" L ")}`,
        stroke: colourOf("depth"),
      }),
    );
    if (coverage.minDepth) {
      const y = (floor - (depthHeight * coverage.minDepth) / deepest).toFixed(2);
      parts.append(
        svg("line", {
          class: "threshold",
          x1: LEFT,
          y1: y,
          x2: RIGHT,
          y2: y,
          stroke: colourOf("low"),
        }),
      );
    }
  }
  for (const difference of differences) {
    const x = along(difference.start + (spanOf(difference, length) - 1) / 2, length);
    const colour = colourOf("difference");
    parts.append(
      svg("line", {
        x1: x.toFixed(2),
        y1: spine - 12,
        x2: x.toFixed(2),
        y2: spine + 12,
        stroke: colour,
        "stroke-width": 2.5,
      }),
      svg("path", {
        d: `M ${x.toFixed(2)} ${String(marksAt - 5)} l 5 5 l -5 5 l -5 -5 Z`,
        fill: colour,
      }),
    );
  }
  // A linear molecule's two ends are its own, so they are drawn as ends; a circle drawn
  // as a line is cut at base 1, which the words beneath say.
  if (!circular) {
    for (const x of [LEFT, RIGHT]) {
      parts.append(
        svg("line", {
          class: "cap",
          x1: x,
          y1: spine - 12,
          x2: x,
          y2: spine + 12,
          "stroke-width": 3,
        }),
      );
    }
  }
  const step = tickEvery(length);
  for (let at = 1; at < length + step; at += step) {
    const place = Math.min(at, length);
    const x = along(place, length);
    parts.append(
      svg("line", {
        class: "tick",
        x1: x.toFixed(1),
        y1: spine + 8,
        x2: x.toFixed(1),
        y2: spine + 14,
        "stroke-width": 1,
      }),
    );
    const label = svg("text", {
      class: "tick-label",
      x: x.toFixed(1),
      y: spine + 26,
      "text-anchor": "middle",
      "dominant-baseline": "middle",
    });
    label.textContent = tickLabel(place, step);
    parts.append(label);
  }
  shown.forEach((layer, index) => {
    const above = Array.from({ length: LANES }, () => []);
    const below = Array.from({ length: LANES }, () => []);
    for (const item of layer.items) {
      const forward = item.strand !== "reverse";
      const pieces = piecesOf(item, length);
      const covers: Interval = pieces.length > 1 ? [1, length] : [item.start, item.end];
      const lane = index * LANES + laneFor(forward ? above : below, covers);
      const y = forward ? spine - 20 - lane * LANE : spine + 34 + lane * LANE;
      const colour = layer.colour ? colourOf(layer.colour) : colourOf(item.qualifiers?.role);
      // The arrowhead goes on the stretch that holds its 3' end.
      const tipped = !directed(item.strand) ? -1 : forward ? pieces.length - 1 : 0;
      pieces.forEach(([from, to], i) => {
        for (const drawn of bar(from, to, length, y, forward, i === tipped)) {
          drawn.setAttribute(drawn.tagName === "line" ? "stroke" : "fill", colour);
          parts.append(drawn);
        }
      });
    }
  });
  return parts;
}

function middle(parts: SVGSVGElement, name: string, length: number, count: number, own = false) {
  const noun = own ? "feature" : "part";
  const found = `${String(count)} ${noun}${count === 1 ? "" : "s"}`;
  for (const [i, text] of [name, `${length.toLocaleString("en")} bp`, found].entries()) {
    if (!text) continue;
    const line = svg("text", {
      class: i === 0 ? "middle name" : "middle",
      x: CENTRE,
      y: CENTRE + (i - 1) * 16,
      "text-anchor": "middle",
      "dominant-baseline": "middle",
    });
    line.textContent = text;
    parts.append(line);
  }
}

function tableHead(...labels: string[]) {
  const cells = labels.map((label) => element("th", { scope: "col" }, label));
  return element("thead", {}, element("tr", {}, ...cells));
}

function table(features: MapFeature[], length: number, own = false) {
  const rows = features.map((feature) => {
    const where =
      feature.end >= feature.start
        ? `${feature.start.toLocaleString("en")}–${feature.end.toLocaleString("en")}`
        : `${feature.start.toLocaleString("en")}–${length.toLocaleString("en")}, 1–${feature.end.toLocaleString("en")}`;
    const swatch = element("span", { className: "swatch" });
    swatch.style.background = colourOf(feature.qualifiers?.role);
    return element(
      "tr",
      {},
      element("th", { scope: "row" }, swatch, feature.qualifiers?.label ?? feature.name),
      element("td", { className: "where" }, where),
      element("td", {}, `${spanOf(feature, length).toLocaleString("en")} bp`),
      element("td", {}, strandWord(feature.strand)),
      element("td", {}, ROLES[feature.qualifiers?.role ?? ""] ?? feature.type),
      ...(own ? [] : [element("td", {}, feature.qualifiers?.identity ?? "")]),
    );
  });
  return element(
    "table",
    {},
    element("caption", {}, own ? "Its features" : "Parts found along it"),
    tableHead(
      own ? "Feature" : "Part",
      "Bases",
      "Length",
      "Strand",
      "What it is",
      ...(own ? [] : ["Matched"]),
    ),
    element("tbody", {}, ...rows),
  );
}

/** Where something lies, as the table writes it: two stretches where it crosses the
 * origin of a circle, which is where the line's drawing cuts it. */
function whereOf(item: MapSpan, length: number) {
  return item.end >= item.start
    ? `${item.start.toLocaleString("en")}\u2013${item.end.toLocaleString("en")}`
    : `${item.start.toLocaleString("en")}\u2013${length.toLocaleString("en")}, 1\u2013${item.end.toLocaleString("en")}`;
}

/** The frame an ORF is read in, as NCBI's ORFfinder writes it: +1 to +3 on the forward
 * strand and -1 to -3 on the reverse. */
function frameOf(orf: MapOrf) {
  const frame = orf.frame;
  return Number.isFinite(frame) ? (frame > 0 ? `+${String(frame)}` : String(frame)) : "";
}

/** The ORFs, each with its frame, where it lies, how long it is and the protein it codes
 * for: everything the drawing shows and the translation it can't. */
function orfTable(orfs: MapOrf[], length: number) {
  const rows = orfs.map((orf) => {
    const swatch = element("span", { className: "swatch" });
    swatch.style.background = colourOf("orf");
    return element(
      "tr",
      {},
      element("th", { scope: "row", className: "where" }, swatch, whereOf(orf, length)),
      element("td", {}, frameOf(orf)),
      element("td", {}, orf.strand === "reverse" ? "reverse" : "forward"),
      element("td", { className: "where" }, `${orf.length.toLocaleString("en")} bp`),
      element("td", { className: "where" }, `${orf.amino_acids.toLocaleString("en")} aa`),
      element("td", {}, orf.start_codon ?? ""),
      element("td", { className: "protein" }, orf.translation ?? ""),
    );
  });
  return element(
    "table",
    {},
    element("caption", {}, "Open reading frames"),
    tableHead("Bases", "Frame", "Strand", "Length", "Protein", "Start", "Translation"),
    element("tbody", {}, ...rows),
  );
}

/** How a site's mismatches read: each with how far it is from the primer's 3' end, since
 * that is what decides whether a polymerase extends there. */
function mismatchesOf(site: MapBindingSite) {
  const found = Array.isArray(site.mismatches) ? site.mismatches : [];
  if (!found.length) return "exact";
  return found
    .map(
      (m) => `${m.primer}\u2192${m.template}, ${String(m.from_three_prime)} from its 3\u2032 end`,
    )
    .join("; ");
}

/** Each linked primer is a button that reports which ID was chosen. */
function siteTable(
  sites: MapBindingSite[],
  length: number,
  primers: readonly string[],
  open: (id: string) => void,
) {
  const known = new Set(primers);
  const rows = sites.map((site) => {
    const swatch = element("span", { className: "swatch" });
    swatch.style.background = colourOf("primer");
    const primer = known.has(site.primer);
    let named;
    if (primer) {
      named = element("button", { type: "button", className: "link" });
      named.textContent = `${site.name} (${site.primer})`;
      named.addEventListener("click", () => {
        open(site.primer);
      });
    } else {
      named = element("span", {}, `${site.name} (${site.primer})`);
    }
    return element(
      "tr",
      {},
      element("th", { scope: "row" }, swatch, named),
      element("td", { className: "where" }, whereOf(site, length)),
      element("td", {}, site.strand === "reverse" ? "reverse" : "forward"),
      element("td", { className: "where" }, site.three_prime.toLocaleString("en")),
      element("td", { className: "where" }, `${String(site.annealed)} bp`),
      element("td", { className: "where" }, `${String(site.tail)} bp`),
      element("td", {}, mismatchesOf(site)),
      element(
        "td",
        { className: "where" },
        typeof site.melting_temperature === "number"
          ? `${site.melting_temperature.toFixed(1)} \u00b0C`
          : "\u2014",
      ),
    );
  });
  return element(
    "table",
    {},
    element("caption", {}, "Primer binding sites"),
    tableHead(
      "Primer",
      "Bases",
      "Strand",
      "3\u2032 end",
      "Annealed",
      "5\u2032 tail",
      "Pairing",
      "Tm (50 mM Na⁺)",
    ),
    element("tbody", {}, ...rows),
  );
}

function percentOf(part: number, whole: number) {
  return whole ? `${String(Math.round((100 * part) / whole))}%` : "\u2014";
}

/** The reads that agree with a difference, of all those there, and on each strand: a
 * difference only one strand's reads show is the sort a sequencing error makes. */
function readsOf(difference: MapDifference) {
  if (difference.depth === undefined || difference.support === undefined) return "\u2014";
  const strands =
    difference.forward !== undefined && difference.reverse !== undefined
      ? ` (${String(difference.forward)} forward, ${String(difference.reverse)} reverse)`
      : "";
  return `${String(difference.support)} of ${String(difference.depth)}, ${percentOf(difference.support, difference.depth)}${strands}`;
}

/** What a difference replaces and has: their bases where they're few enough to read, and
 * otherwise how many, from its full lengths, not its preview's. */
function changeOf(difference: MapDifference) {
  const side = (bases: string, length: number) =>
    !length
      ? "\u2014"
      : length <= 12 && bases.length === length
        ? bases
        : `${length.toLocaleString("en")} bp`;
  const was = side(difference.reference, difference.reference_length);
  const now = side(difference.observed, difference.observed_length);
  return `${difference.kind.replace("_", "-")}: ${was} \u2192 ${now}`;
}

/** Each difference: its HGVS name, where it lies, what changed, the features it lies in,
 * its reads, and the run of one base it touches. */
function differenceTable(differences: MapDifference[], length: number) {
  const rows = differences.map((difference) => {
    const swatch = element("span", { className: "swatch" });
    swatch.style.background = colourOf("difference");
    const run = difference.homopolymer ?? 1;
    return element(
      "tr",
      {},
      element("th", { scope: "row", className: "hgvs" }, swatch, difference.hgvs),
      element(
        "td",
        { className: "where" },
        difference.start === difference.end
          ? difference.start.toLocaleString("en")
          : whereOf(difference, length),
      ),
      element("td", {}, changeOf(difference)),
      element("td", {}, difference.features?.length ? difference.features.join(", ") : "\u2014"),
      element("td", {}, readsOf(difference)),
      element("td", { className: "where" }, run > 1 ? `${String(run)} bases` : "\u2014"),
    );
  });
  return element(
    "table",
    {},
    element("caption", {}, "Differences from the record"),
    tableHead("HGVS", "Bases", "Change", "In", "Reads agreeing", "Repeated base"),
    element("tbody", {}, ...rows),
  );
}

/** Summarize the recorded interval list alongside the minima and the full count. */
function lowCoverage(coverage: MapCoverage, low: MapCoverage["low"], length: number) {
  const minimum = coverage.minDepth ?? 1;
  const total = coverage.low_count;
  if (!low?.length) {
    if (
      !low ||
      total ||
      (coverage.leastDepth ?? minimum) < minimum ||
      coverage.min.some((value) => value < minimum)
    ) {
      return "interval details unavailable; see coverage.tsv";
    }
    return `every base at the minimum depth of ${String(minimum)} or more`;
  }
  const places = low.map((span) => whereOf(span, length)).join(", ");
  let limit = "";
  if (total !== undefined && total > low.length) {
    limit = `showing ${String(low.length)} of ${String(total)} intervals`;
  } else if (total === undefined && low.length === 200) {
    limit = "first 200 intervals";
  }
  const detail = limit ? ` (${limit}; see coverage.tsv for all)` : "";
  return `under the minimum depth of ${String(minimum)} at ${places}${detail}`;
}

/** The depth of reads in figures, with each stretch under the minimum depth. */
function coverageTable(coverage: MapCoverage, under: string) {
  const row = (what: string, value: string) =>
    element("tr", {}, element("th", { scope: "row" }, what), element("td", {}, value));
  const rows = [];
  if (coverage.meanDepth !== undefined)
    rows.push(row("Mean depth", `${String(coverage.meanDepth)} reads`));
  if (coverage.leastDepth !== undefined)
    rows.push(row("Least depth", `${String(coverage.leastDepth)} reads`));
  if (coverage.minDepth !== undefined)
    rows.push(row("Called at", `${String(coverage.minDepth)} reads or more`));
  rows.push(row("Under it", under));
  return element("table", {}, element("caption", {}, "Read depth"), element("tbody", {}, ...rows));
}

/** The buttons that turn each layer on and off: one for each layer a run has found, each
 * saying whether it is showing and how many it holds. */
function layerButtons(layers: Layer[], toggle: (key: LayerKey) => void) {
  const group = element("div", { className: "views", role: "group", "aria-label": "Layers" });
  for (const layer of layers) {
    const button = element("button", {
      type: "button",
      "aria-pressed": String(layer.shown),
    });
    button.dataset["layer"] = layer.key;
    button.textContent = layer.button ?? `${layer.label} (${layer.count.toLocaleString("en")})`;
    button.addEventListener("click", () => {
      toggle(layer.key);
    });
    group.append(button);
  }
  return group;
}

/** What the drawing shows, in words, so a screen reader is told everything the picture
 * tells: the molecule, which way it is drawn, and each layer showing along it. */
function describe(length: number, layers: Layer[], circular: boolean, view: View) {
  const bp = `${length.toLocaleString("en")} base pairs`;
  const shape = circular
    ? view === "line"
      ? `A circle of ${bp}, drawn as a line cut at base 1, so a part across its origin is drawn in two pieces`
      : `A circle of ${bp}, drawn as a ring with base 1 at the top, going clockwise`
    : `A linear molecule of ${bp}, drawn as a line from base 1 at the left to base ${length.toLocaleString("en")} at the right, with an end at each`;
  const said = layers.filter((layer) => layer.shown).map((layer) => layer.describe());
  if (!said.length) {
    const ran = layers.length
      ? "with no layer showing."
      : "with none of the library's parts found along it.";
    return `${shape}, ${ran}`;
  }
  return `${shape}, ${said.join(", and ")}.`;
}

/** Each of the three layers in words: what it holds and where each of them lies. */
function eachOf<T extends MapSpan>(items: T[], how: (item: T) => string) {
  return items
    .map(
      (i) =>
        `${how(i)}, ${i.start.toLocaleString("en")} to ${i.end.toLocaleString("en")} ${onStrand(i.strand)}`,
    )
    .join("; ");
}

function describeParts(items: MapFeature[], own = false) {
  if (own) {
    if (!items.length) return "with no features of its own";
    const count = `${String(items.length)} ${items.length === 1 ? "feature" : "features"}`;
    return `with ${count} of its own: ${eachOf(items, (i) => i.qualifiers?.label ?? i.name)}`;
  }
  if (!items.length) return "with none of the library's parts found along it";
  const count = `${String(items.length)} ${items.length === 1 ? "part" : "parts"}`;
  return `with ${count} of the library found along it: ${eachOf(items, (i) => i.qualifiers?.label ?? i.name)}`;
}

function describeOrfs(items: MapOrf[]) {
  if (!items.length) return "with no open reading frame found in any of its six frames";
  const count = `${String(items.length)} open reading ${items.length === 1 ? "frame" : "frames"}`;
  return `with ${count}: ${eachOf(items, (i) => `${String(i.amino_acids)} amino acids in frame ${frameOf(i)}`)}`;
}

function describeCoverage(coverage: MapCoverage, under: string) {
  const mean =
    coverage.meanDepth !== undefined ? `${String(coverage.meanDepth)} reads deep on average` : "";
  const least =
    coverage.leastDepth !== undefined ? `${String(coverage.leastDepth)} at the least` : "";
  const depth = [mean, least].filter(Boolean).join(", ");
  return `with the depth of reads drawn along it${depth ? `, ${depth}` : ""}, and ${under}`;
}

function describeDifferences(items: MapDifference[]) {
  if (!items.length) return "with no differences from the record";
  const count = `${String(items.length)} ${items.length === 1 ? "difference" : "differences"} from the record`;
  const each = items
    .map((i) => {
      const where = i.features?.length ? ` in ${i.features.join(" and ")}` : "";
      return `${i.hgvs.length > 40 ? `${i.hgvs.slice(0, 40)}\u2026` : i.hgvs} at ${i.start.toLocaleString("en")}${i.end !== i.start ? ` to ${i.end.toLocaleString("en")}` : ""}${where}`;
    })
    .join("; ");
  return `with ${count}: ${each}`;
}

function describeSites(items: MapBindingSite[]) {
  if (!items.length) return "with none of the primers binding it";
  const count = `${String(items.length)} primer binding ${items.length === 1 ? "site" : "sites"}`;
  const each = items
    .map(
      (i) =>
        `${i.name}, ${i.start.toLocaleString("en")} to ${i.end.toLocaleString("en")} on the ${i.strand === "reverse" ? "reverse" : "forward"} strand, its 3\u2032 end at base ${i.three_prime.toLocaleString("en")}`,
    )
    .join("; ");
  return `with ${count}: ${each}`;
}

/** The buttons that choose the view, for a circle, which can be read either way. A
 * linear molecule has no ring view, so it is given none. */
function views(chosen: View, choose: (view: View) => void) {
  const group = element("div", { className: "views", role: "group", "aria-label": "Map view" });
  for (const [view, label] of [
    ["ring", "Circular"],
    ["line", "Linear"],
  ] as const) {
    const button = element("button", {
      type: "button",
      "aria-pressed": String(view === chosen),
    });
    button.textContent = label;
    button.addEventListener("click", () => {
      choose(view);
    });
    group.append(button);
  }
  return group;
}

function onMap(item: unknown, length: number): boolean {
  if (!item || typeof item !== "object" || !("start" in item) || !("end" in item)) return false;
  return [item.start, item.end].every(
    (n) => typeof n === "number" && Number.isInteger(n) && n >= 1 && n <= length,
  );
}

const LABELS: Record<LayerKey, string> = {
  parts: "Library parts",
  orfs: "Open reading frames",
  primers: "Primer binding sites",
  coverage: "Read depth",
  differences: "Differences",
};

/** A read-only diagram of the annotations supplied by its host. */
export class PlasmidMap extends HTMLElement {
  readonly #root = this.attachShadow({ mode: "open" });
  #value: PlasmidMapData = { name: "", length: 0, topology: "circular" };
  #view: View = "ring";
  readonly #hidden = new Set<LayerKey>();

  get data(): PlasmidMapData {
    return this.#value;
  }
  set data(value: PlasmidMapData) {
    if (value.name !== this.#value.name || value.topology !== this.#value.topology) {
      this.#view = value.topology === "linear" ? "line" : "ring";
    }
    this.#value = value;
    this.draw();
  }
  connectedCallback() {
    this.draw();
  }
  private emit(name: string, detail: unknown) {
    this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true, composed: true }));
  }
  private layers(length: number): Layer[] {
    const made: Layer[] = [];
    const add = <T extends MapSpan>(
      key: LayerKey,
      colour: string | null,
      layer: MapAnnotation<T> | undefined,
      describe: (items: T[]) => string,
      table: (items: T[]) => HTMLTableElement,
    ) => {
      if (!layer) return;
      const found = layer.items
        .filter((item) => onMap(item, length))
        .sort((a, b) => a.start - b.start);
      const shown = !this.#hidden.has(key);
      made.push({
        key,
        colour,
        label: LABELS[key],
        count: found.length,
        shown,
        items: shown ? found : [],
        describe: () => describe(found),
        table: () => table(found),
      });
    };
    const data = this.#value;
    const own = data.parts?.own === true;
    add(
      "parts",
      null,
      data.parts,
      (items) => describeParts(items, own),
      (items) => table(items, length, own),
    );
    if (own) {
      const parts = made.find((layer) => layer.key === "parts");
      if (parts) parts.label = "Features";
    }
    add("orfs", "orf", data.orfs, describeOrfs, (items) => orfTable(items, length));
    add("primers", "primer", data.sites, describeSites, (items) =>
      siteTable(items, length, data.linkedPrimerIds ?? [], (id) => {
        this.emit("primer-open", { id });
      }),
    );
    const coverage = data.coverage;
    if (coverage && coverage.bin_size > 0 && coverage.mean.length) {
      const low = coverage.low?.filter((item) => onMap(item, length));
      const under = lowCoverage(coverage, low, length);
      const shown = !this.#hidden.has("coverage");
      made.push({
        key: "coverage",
        colour: "depth",
        label: LABELS.coverage,
        count: coverage.low_count ?? low?.length ?? 0,
        button: LABELS.coverage,
        shown,
        items: shown ? (low ?? []) : [],
        describe: () => describeCoverage(coverage, under),
        table: () => coverageTable(coverage, under),
      });
    }
    add("differences", "difference", data.differences, describeDifferences, (items) =>
      differenceTable(items, length),
    );
    return made;
  }

  private draw() {
    const data = this.#value;
    const style = element("style");
    style.textContent = STYLE;
    const length = data.length;
    const circular = data.topology === "circular";
    const layers = Number.isSafeInteger(length) && length > 0 ? this.layers(length) : [];
    if (!layers.length) {
      const children = [
        style,
        element("p", { role: "status" }, data.emptyMessage ?? "No annotations to display."),
      ];
      if (data.emptyAction) {
        const button = element("button", { type: "button", className: "run" }, data.emptyAction);
        button.addEventListener("click", () => {
          this.emit("empty-action", null);
        });
        children.push(element("p", {}, button));
      }
      this.#root.replaceChildren(...children);
      return;
    }
    const of = (key: LayerKey) => layers.find((layer) => layer.key === key)?.items ?? [];
    const name = data.name;
    const showing = (key: LayerKey) => layers.some((layer) => layer.key === key && layer.shown);
    const coverage =
      showing("coverage") && data.coverage ? { ...data.coverage, low: of("coverage") } : undefined;
    const differences = of("differences") as MapDifference[];
    let picture;
    if (circular && this.#view === "ring") {
      picture = drawing(length, of("parts"), of("orfs"), of("primers"), coverage, differences);
      middle(picture, name, length, of("parts").length, data.parts?.own === true);
    } else {
      picture = lineDrawing(length, layers, circular, coverage, differences);
      const heading = svg("text", { class: "middle name", x: LEFT, y: 14 });
      heading.textContent = [name, `${length.toLocaleString("en")} bp`].filter(Boolean).join(", ");
      picture.append(heading);
    }
    const children: HTMLElement[] = [style, element("h2", {}, "Map")];
    if (circular)
      children.push(
        views(this.#view, (view) => {
          this.choose(view);
        }),
      );
    if (layers.length > 1) {
      children.push(
        layerButtons(layers, (key) => {
          this.toggle(key);
        }),
      );
    }
    const shown = layers.filter(
      (layer) => layer.shown && (layer.items.length || layer.key === "coverage"),
    );
    children.push(
      element("p", {}, describe(length, layers, circular, this.#view)),
      ...(data.notes ?? []).map((note) => element("p", { className: "note" }, note)),
      element(
        "div",
        { className: "map" },
        picture,
        ...shown.map((layer) =>
          element(
            "div",
            { className: "tables", tabindex: "0", role: "region", "aria-label": layer.label },
            layer.table(),
          ),
        ),
      ),
    );
    const how = [
      ["Annotated with", data.parts?.source],
      ["ORFs found with", data.orfs?.source],
      ["Binding sites found with", data.sites?.source],
      ["Read depth from", data.coverage?.source],
      ["Differences found with", data.differences?.source],
    ] as const;
    const notes = how.filter(([, said]) => Boolean(said));
    if (notes.length) {
      children.push(
        element(
          "dl",
          {},
          ...notes.flatMap(([what, said]) => [
            element("dt", {}, what),
            element("dd", { className: "note" }, said ?? ""),
          ]),
        ),
      );
    }
    this.#root.replaceChildren(...children);
  }

  /** Shows the other drawing, and leaves the keyboard on the button that was pressed. */
  private choose(view: View) {
    if (view === this.#view) return;
    this.#view = view;
    this.draw();
    const buttons = this.#root.querySelectorAll<HTMLButtonElement>(".views button");
    const chosen = [...buttons].find((b) => b.getAttribute("aria-pressed") === "true");
    chosen?.focus();
    this.emit("view-change", { view });
  }

  /** Turns a layer on or off, and leaves the keyboard on the button that was pressed. */
  private toggle(key: LayerKey) {
    if (this.#hidden.has(key)) this.#hidden.delete(key);
    else this.#hidden.add(key);
    this.draw();
    const group = this.#root.querySelector('[aria-label="Layers"]');
    const buttons = group ? [...group.querySelectorAll("button")] : [];
    buttons.find((b) => b.dataset["layer"] === key)?.focus();
    this.emit("layer-change", { key, shown: !this.#hidden.has(key) });
  }
}

if (!customElements.get("iki-plasmid-map")) customElements.define("iki-plasmid-map", PlasmidMap);
declare global {
  interface HTMLElementTagNameMap {
    "iki-plasmid-map": PlasmidMap;
  }
}
