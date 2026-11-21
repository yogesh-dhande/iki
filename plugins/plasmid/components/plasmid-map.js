// The plasmid plugin's Map tab: the plasmid drawn as the molecule it is, with each
// annotated part an arrow pointing the way its strand runs. Iki's own sequence viewer
// draws a sequence in rows of 60, which is how a gene is read and not how a plasmid is;
// a plasmid is read as a map, so this draws one. It draws the `annotated_features` the
// plugin's actions find along the plasmid's sequence (ADR 0084); Iki sets `entity` to the
// plasmid, as the person viewing it may read it.
//
// A circle is drawn round a ring, and a linear plasmid along a line, as SnapGene and
// Benchling draw each (ADR 0168). A linear molecule opens on the line, and has no ring
// view: it has no origin to go round. A circle opens on the ring and can be seen as a
// line too, cut at base 1, which is how a map is read beside a gel or a sequencing read.
//
// Three layers can lie along it, each what a run of the plugin found and each a layer the
// person turns off: the library's parts, the open reading frames, and where the project's
// primers bind (ADR 0172). Each layer has its own ring or lanes and its own table beneath,
// so they are never read as one another, and each is turned on and off by a button that
// says whether it is showing.
//
// The drawing is described in words for screen readers, and every part is in the table
// below it with its coordinates, so nothing is told by colour alone. The two views are
// buttons in a group, reachable and operable from the keyboard like any button, each
// saying whether it is the view being shown (`aria-pressed`).
//
// Until the plasmid is annotated it says which action annotates it, and offers that
// action where the person viewing it may run it (ADR 0170): the trigger that annotates
// each new plasmid is each project's, and off until someone turns it on.

//: The action that annotates a plasmid, which the map offers while it isn't drawn.
const ANNOTATE = "iki/plasmid.annotate_plasmid";

const STYLE = `
  :host { display: block; color: CanvasText; background: Canvas; padding: 1rem; border-radius: 8px;
    font: 14px/1.45 system-ui, sans-serif; }
  h2 { font-size: 1rem; margin: 0 0 0.5rem; }
  p { margin: 0 0 0.75rem; max-width: 70ch; }
  .map { display: flex; flex-wrap: wrap; gap: 1.5rem; align-items: flex-start; }
  svg { flex: none; max-width: 100%; height: auto; }
  .ring { fill: none; stroke: color-mix(in srgb, CanvasText 18%, Canvas); }
  .tick { stroke: color-mix(in srgb, CanvasText 28%, Canvas); }
  .tick-label, .middle { fill: color-mix(in srgb, CanvasText 70%, Canvas); font-size: 10px; }
  .middle.name { fill: CanvasText; font-size: 13px; font-weight: 600; }
  .arc { fill: none; stroke-linecap: butt; }
  table { border-collapse: collapse; font-size: 0.85rem; min-width: 22rem; }
  caption { text-align: left; font-weight: 600; padding-bottom: 0.35rem; }
  th, td { text-align: left; padding: 0.2rem 0.6rem 0.2rem 0; vertical-align: top;
    border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, Canvas); }
  th { font-weight: 600; }
  td.where { font-variant-numeric: tabular-nums; white-space: nowrap; }
  .swatch { display: inline-block; width: 0.7rem; height: 0.7rem; border-radius: 2px;
    margin-right: 0.4rem; vertical-align: -1px; }
  .views { display: flex; gap: 0.5rem; margin: 0 0 0.75rem; }
  .views button { font: inherit; color: CanvasText; background: Canvas; cursor: pointer;
    padding: 0.25rem 0.75rem; border: 1px solid color-mix(in srgb, CanvasText 40%, Canvas);
    border-radius: 999px; }
  .views button[aria-pressed="true"] { background: color-mix(in srgb, CanvasText 12%, Canvas);
    border-color: CanvasText; font-weight: 600; }
  .views button:focus-visible { outline: 2px solid CanvasText; outline-offset: 2px; }
  .line { stroke: color-mix(in srgb, CanvasText 18%, Canvas); }
  .cap { stroke: color-mix(in srgb, CanvasText 55%, Canvas); }
  .end-label { fill: color-mix(in srgb, CanvasText 70%, Canvas); font-size: 10px; }
  dl { display: grid; grid-template-columns: max-content auto; gap: 0.25rem 1rem; margin: 0.75rem 0 0; }
  dt { font-weight: 600; }
  dd { margin: 0; }
  .note { color: color-mix(in srgb, CanvasText 70%, Canvas); }
  .run { font: inherit; color: Canvas; background: CanvasText; cursor: pointer;
    padding: 0.375rem 0.875rem; border: 1px solid CanvasText; border-radius: 6px; }
  .run:focus-visible { outline: 2px solid CanvasText; outline-offset: 2px; }
  .link { font: inherit; color: CanvasText; background: none; border: 0; padding: 0;
    text-decoration: underline; cursor: pointer; text-align: left; }
  .link:focus-visible { outline: 2px solid CanvasText; outline-offset: 2px; }
  td.protein { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.8rem;
    word-break: break-all; max-width: 22rem; }
`;

// A colour per role, each readable on Canvas in both light and dark, and each part also
// named in the table, so the map never tells anything by colour alone.
const COLOURS = {
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
};
const ROLES = {
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

function element(tag, attributes = {}, ...children) {
  const out = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "className") out.className = value;
    else out.setAttribute(name, value);
  }
  out.append(...children);
  return out;
}

function svg(tag, attributes = {}, ...children) {
  const out = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attributes)) out.setAttribute(name, value);
  out.append(...children);
  return out;
}

function dark() {
  return globalThis.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

function colourOf(role) {
  const pair = COLOURS[role] ?? ["#57606a", "#9aa4b1"];
  return dark() ? pair[1] : pair[0];
}

/** Where base `at` of a plasmid `length` long lies, `radius` out from the centre: base 1
 * at the top, going clockwise, as a plasmid map is drawn. */
function point(at, length, radius) {
  const angle = (2 * Math.PI * ((at - 1) % length)) / length - Math.PI / 2;
  return [CENTRE + radius * Math.cos(angle), CENTRE + radius * Math.sin(angle)];
}

/** The bases a feature covers, which wraps the origin when its end is before its start. */
function spanOf(feature, length) {
  return feature.end >= feature.start
    ? feature.end - feature.start + 1
    : length - feature.start + 1 + feature.end;
}

/** An arc from base `from` for `span` bases, as an SVG path, with an arrowhead at the end
 * it runs to. */
function arc(from, span, length, radius, strand) {
  const head = Math.min(span / 2, (14 * length) / (2 * Math.PI * radius));
  const forward = strand !== "reverse";
  const bodyFrom = forward ? from : from + head;
  const bodySpan = span - head;
  const [sx, sy] = point(bodyFrom, length, radius);
  const [ex, ey] = point(bodyFrom + bodySpan, length, radius);
  const sweep = bodySpan / length > 0.5 ? 1 : 0;
  const body = `M ${sx.toFixed(2)} ${sy.toFixed(2)} A ${radius} ${radius} 0 ${sweep} 1 ${ex.toFixed(2)} ${ey.toFixed(2)}`;
  const tipAt = forward ? from + span : from;
  const [tx, ty] = point(tipAt, length, radius);
  const [bx, by] = point(forward ? from + bodySpan : from + head, length, radius - 5);
  const [cx, cy] = point(forward ? from + bodySpan : from + head, length, radius + 5);
  const head3 = `M ${bx.toFixed(2)} ${by.toFixed(2)} L ${tx.toFixed(2)} ${ty.toFixed(2)} L ${cx.toFixed(2)} ${cy.toFixed(2)} Z`;
  return [body, head3];
}

/** Ticks every 500, 1,000, 2,000 bases and so on: the first step that gives at most ten
 * of them. */
function tickEvery(length) {
  for (const step of [500, 1000, 2000, 5000, 10000, 20000, 50000]) {
    if (length / step <= 10) return step;
  }
  return 100000;
}

/** A tick's label: base 1, then thousands, to as many decimals as the step needs, so a
 * tick every 500 bases reads 0.5k, 1k, 1.5k rather than 1k, 1k, 2k. */
function tickLabel(at, step) {
  if (at === 1) return "1";
  const thousands = (at - 1) / 1000;
  return `${thousands.toFixed(step < 1000 ? 1 : 0).replace(/\.0$/, "")}k`;
}

/** An ORF or a binding site, as a span to draw: they carry the same start, end and
 * strand a library part does, so one drawing serves all three layers. */
function spans(items) {
  return (items ?? []).filter((i) => Number.isInteger(i.start) && Number.isInteger(i.end));
}

function drawing(length, features, orfs, sites) {
  const parts = svg("svg", {
    viewBox: `0 0 ${SIZE} ${SIZE}`,
    width: SIZE,
    height: SIZE,
    // Hidden from screen readers: the paragraph above it says what it draws, in words,
    // and the table below it gives every part.
    "aria-hidden": "true",
  });
  parts.append(
    svg("circle", { class: "ring", cx: CENTRE, cy: CENTRE, r: RADIUS, "stroke-width": 8 }),
  );
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
  const taken = rings.map(() => []);
  for (const feature of features) {
    const span = spanOf(feature, length);
    const covers = [feature.start, feature.start + span];
    let ring = rings.length - 1;
    for (let i = 0; i < rings.length; i++) {
      if (!taken[i].some(([a, b]) => covers[0] < b && a < covers[1])) {
        ring = i;
        break;
      }
    }
    taken[ring].push(covers);
    const colour = colourOf(feature.qualifiers?.role);
    const [body, head] = arc(feature.start, span, length, rings[ring], feature.strand);
    parts.append(svg("path", { class: "arc", d: body, stroke: colour, "stroke-width": 7 }));
    parts.append(svg("path", { d: head, fill: colour }));
  }
  // The ORFs, on rings of their own inside the backbone, and the binding sites on one
  // outside it: each layer apart, each pointing the way it is read.
  const orfLanes = ORF_RINGS.map(() => []);
  for (const orf of spans(orfs)) {
    const span = spanOf(orf, length);
    const covers = [orf.start, orf.start + span];
    let ring = ORF_RINGS.length - 1;
    for (let i = 0; i < ORF_RINGS.length; i++) {
      if (!orfLanes[i].some(([a, b]) => covers[0] < b && a < covers[1])) {
        ring = i;
        break;
      }
    }
    orfLanes[ring].push(covers);
    const [body, head] = arc(orf.start, span, length, ORF_RINGS[ring], orf.strand);
    parts.append(
      svg("path", { class: "arc", d: body, stroke: colourOf("orf"), "stroke-width": 5 }),
    );
    parts.append(svg("path", { d: head, fill: colourOf("orf") }));
  }
  for (const site of spans(sites)) {
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
function spineAt(layers) {
  return layers * LANES * LANE + 26;
}

function tallAt(layers) {
  return spineAt(layers) + layers * LANES * LANE + 30;
}

/** Where base `at` lies along the line. */
function along(at, length) {
  const share = length > 1 ? (at - 1) / (length - 1) : 0;
  return LEFT + (RIGHT - LEFT) * Math.min(Math.max(share, 0), 1);
}

/** The stretches a part covers along the molecule: one, or two where it crosses a
 * circle's origin, which a linear molecule's parts never do. */
function piecesOf(feature, length) {
  return feature.end >= feature.start
    ? [[feature.start, feature.end]]
    : [
        [feature.start, length],
        [1, feature.end],
      ];
}

/** A lane for a part: the first one free where it lies, so overlapping parts stay apart. */
function laneFor(taken, covers) {
  for (let i = 0; i < taken.length; i++) {
    if (!taken[i].some(([a, b]) => covers[0] <= b && a <= covers[1])) {
      taken[i].push(covers);
      return i;
    }
  }
  taken[taken.length - 1].push(covers);
  return taken.length - 1;
}

/** One stretch of a part as a bar along the line, with an arrowhead where the part's 3'
 * end is: on the right running forward, on the left running reverse. */
function bar(from, to, length, y, forward, tipped) {
  const x1 = along(from, length);
  const x2 = along(to, length);
  const head = Math.min(7, Math.max(x2 - x1, 1));
  const body = tipped ? (forward ? [x1, x2 - head] : [x1 + head, x2]) : [x1, x2];
  const parts = [
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
        d: `M ${back.toFixed(2)} ${y - 6} L ${tip.toFixed(2)} ${y} L ${back.toFixed(2)} ${y + 6} Z`,
      }),
    );
  }
  return parts;
}

/** The line view: the molecule along a line from base 1 on the left, each layer's parts
 * on lanes of its own -- above the line where they run forward and below where they run
 * reverse, as SnapGene's linear view has them. */
function lineDrawing(length, layers, circular) {
  const shown = layers.filter((layer) => layer.items.length > 0);
  const count = Math.max(shown.length, 1);
  const spine = spineAt(count);
  const tall = tallAt(count);
  const parts = svg("svg", {
    viewBox: `0 0 ${WIDE} ${tall}`,
    width: WIDE,
    height: tall,
    // Hidden from screen readers: the paragraph above it says what it draws, in words,
    // and the tables below it give every part of every layer.
    "aria-hidden": "true",
  });
  parts.append(
    svg("line", { class: "line", x1: LEFT, y1: spine, x2: RIGHT, y2: spine, "stroke-width": 8 }),
  );
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
      const covers = [pieces[0][0], pieces[pieces.length - 1][1]];
      const lane = index * LANES + laneFor(forward ? above : below, covers);
      const y = forward ? spine - 20 - lane * LANE : spine + 34 + lane * LANE;
      const colour = layer.colour ? colourOf(layer.colour) : colourOf(item.qualifiers?.role);
      // The arrowhead goes on the stretch that holds its 3' end.
      const tipped = forward ? pieces.length - 1 : 0;
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

function middle(parts, name, length, count) {
  const found = `${count} ${count === 1 ? "part" : "parts"}`;
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

function table(features, length) {
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
      element("th", { scope: "row" }, swatch, feature.qualifiers?.label || feature.name),
      element("td", { className: "where" }, where),
      element("td", {}, `${spanOf(feature, length).toLocaleString("en")} bp`),
      element("td", {}, feature.strand === "reverse" ? "reverse" : "forward"),
      element("td", {}, ROLES[feature.qualifiers?.role] ?? feature.type),
      element("td", {}, feature.qualifiers?.identity ?? ""),
    );
  });
  return element(
    "table",
    {},
    element("caption", {}, "Parts found along it"),
    element(
      "thead",
      {},
      element(
        "tr",
        {},
        element("th", { scope: "col" }, "Part"),
        element("th", { scope: "col" }, "Bases"),
        element("th", { scope: "col" }, "Length"),
        element("th", { scope: "col" }, "Strand"),
        element("th", { scope: "col" }, "What it is"),
        element("th", { scope: "col" }, "Matched"),
      ),
    ),
    element("tbody", {}, ...rows),
  );
}

/** Where something lies, as the table writes it: two stretches where it crosses the
 * origin of a circle, which is where the line's drawing cuts it. */
function whereOf(item, length) {
  return item.end >= item.start
    ? `${item.start.toLocaleString("en")}\u2013${item.end.toLocaleString("en")}`
    : `${item.start.toLocaleString("en")}\u2013${length.toLocaleString("en")}, 1\u2013${item.end.toLocaleString("en")}`;
}

/** The frame an ORF is read in, as NCBI's ORFfinder writes it: +1 to +3 on the forward
 * strand and -1 to -3 on the reverse. */
function frameOf(orf) {
  const frame = Number(orf.frame);
  return Number.isFinite(frame) ? (frame > 0 ? `+${String(frame)}` : String(frame)) : "";
}

/** The ORFs, each with its frame, where it lies, how long it is and the protein it codes
 * for: everything the drawing shows and the translation it can't. */
function orfTable(orfs, length) {
  const rows = orfs.map((orf) => {
    const swatch = element("span", { className: "swatch" });
    swatch.style.background = colourOf("orf");
    return element(
      "tr",
      {},
      element("th", { scope: "row", className: "where" }, swatch, whereOf(orf, length)),
      element("td", {}, frameOf(orf)),
      element("td", {}, orf.strand === "reverse" ? "reverse" : "forward"),
      element("td", { className: "where" }, `${Number(orf.length).toLocaleString("en")} bp`),
      element("td", { className: "where" }, `${Number(orf.amino_acids).toLocaleString("en")} aa`),
      element("td", {}, orf.start_codon ?? ""),
      element("td", { className: "protein" }, orf.translation ?? ""),
    );
  });
  return element(
    "table",
    {},
    element("caption", {}, "Open reading frames"),
    element(
      "thead",
      {},
      element(
        "tr",
        {},
        element("th", { scope: "col" }, "Bases"),
        element("th", { scope: "col" }, "Frame"),
        element("th", { scope: "col" }, "Strand"),
        element("th", { scope: "col" }, "Length"),
        element("th", { scope: "col" }, "Protein"),
        element("th", { scope: "col" }, "Start"),
        element("th", { scope: "col" }, "Translation"),
      ),
    ),
    element("tbody", {}, ...rows),
  );
}

/** How a site's mismatches read: each with how far it is from the primer's 3' end, since
 * that is what decides whether a polymerase extends there. */
function mismatchesOf(site) {
  const found = Array.isArray(site.mismatches) ? site.mismatches : [];
  if (!found.length) return "exact";
  return found
    .map(
      (m) =>
        `${String(m.primer)}\u2192${String(m.template)}, ${String(m.from_three_prime)} from its 3\u2032 end`,
    )
    .join("; ");
}

/** The primer binding sites, each row naming its primer -- a button that opens the
 * primer's record where Iki gave this map the project's primers, and its ID in plain
 * words where it didn't. */
function siteTable(sites, length, primers, open) {
  const known = new Map((primers ?? []).map((p) => [p.hrid, p]));
  const rows = sites.map((site) => {
    const swatch = element("span", { className: "swatch" });
    swatch.style.background = colourOf("primer");
    const primer = known.get(site.primer);
    let named;
    if (primer) {
      named = element("button", { type: "button", className: "link" });
      named.textContent = `${site.name} (${site.primer})`;
      named.addEventListener("click", () => open(site.primer));
    } else {
      named = element("span", {}, `${site.name} (${site.primer})`);
    }
    return element(
      "tr",
      {},
      element("th", { scope: "row" }, swatch, named),
      element("td", { className: "where" }, whereOf(site, length)),
      element("td", {}, site.strand === "reverse" ? "reverse" : "forward"),
      element("td", { className: "where" }, Number(site.three_prime).toLocaleString("en")),
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
    element(
      "thead",
      {},
      element(
        "tr",
        {},
        element("th", { scope: "col" }, "Primer"),
        element("th", { scope: "col" }, "Bases"),
        element("th", { scope: "col" }, "Strand"),
        element("th", { scope: "col" }, "3\u2032 end"),
        element("th", { scope: "col" }, "Annealed"),
        element("th", { scope: "col" }, "5\u2032 tail"),
        element("th", { scope: "col" }, "Pairing"),
        element("th", { scope: "col" }, "Tm"),
      ),
    ),
    element("tbody", {}, ...rows),
  );
}

/** The buttons that turn each layer on and off: one for each layer a run has found, each
 * saying whether it is showing and how many it holds. */
function layerButtons(layers, toggle) {
  const group = element("div", { className: "views", role: "group", "aria-label": "Layers" });
  for (const layer of layers) {
    const button = element("button", {
      type: "button",
      "aria-pressed": String(layer.shown),
    });
    button.textContent = `${layer.label} (${layer.found.length.toLocaleString("en")})`;
    button.addEventListener("click", () => toggle(layer.key));
    group.append(button);
  }
  return group;
}

/** What the drawing shows, in words, so a screen reader is told everything the picture
 * tells: the molecule, which way it is drawn, and each layer showing along it. */
function describe(length, layers, circular, view) {
  const bp = `${length.toLocaleString("en")} base pairs`;
  const shape = circular
    ? view === "line"
      ? `A circle of ${bp}, drawn as a line cut at base 1, so a part across its origin is drawn in two pieces`
      : `A circle of ${bp}, drawn as a ring with base 1 at the top, going clockwise`
    : `A linear molecule of ${bp}, drawn as a line from base 1 at the left to base ${length.toLocaleString("en")} at the right, with an end at each`;
  const said = layers.filter((layer) => layer.shown).map((layer) => layer.describe(layer.items));
  if (!said.length) {
    const ran = layers.length
      ? "with no layer showing."
      : "with none of the library's parts found along it.";
    return `${shape}, ${ran}`;
  }
  return `${shape}, ${said.join(", and ")}.`;
}

/** Each of the three layers in words: what it holds and where each of them lies. */
function eachOf(items, how) {
  return items
    .map(
      (i) =>
        `${how(i)}, ${i.start.toLocaleString("en")} to ${i.end.toLocaleString("en")} on the ${i.strand === "reverse" ? "reverse" : "forward"} strand`,
    )
    .join("; ");
}

function describeParts(items) {
  if (!items.length) return "with none of the library's parts found along it";
  const count = `${items.length} ${items.length === 1 ? "part" : "parts"}`;
  return `with ${count} of the library found along it: ${eachOf(items, (i) => i.qualifiers?.label || i.name)}`;
}

function describeOrfs(items) {
  if (!items.length) return "with no open reading frame found in any of its six frames";
  const count = `${items.length} open reading ${items.length === 1 ? "frame" : "frames"}`;
  return `with ${count}: ${eachOf(items, (i) => `${String(i.amino_acids)} amino acids in frame ${frameOf(i)}`)}`;
}

function describeSites(items) {
  if (!items.length) return "with none of the primers binding it";
  const count = `${items.length} primer binding ${items.length === 1 ? "site" : "sites"}`;
  const each = items
    .map(
      (i) =>
        `${String(i.name)}, ${i.start.toLocaleString("en")} to ${i.end.toLocaleString("en")} on the ${i.strand === "reverse" ? "reverse" : "forward"} strand, its 3\u2032 end at base ${Number(i.three_prime).toLocaleString("en")}`,
    )
    .join("; ");
  return `with ${count}: ${each}`;
}

/** The buttons that choose the view, for a circle, which can be read either way. A
 * linear molecule has no ring view, so it is given none. */
function views(chosen, choose) {
  const group = element("div", { className: "views", role: "group", "aria-label": "Map view" });
  for (const [view, label] of [
    ["ring", "Circular"],
    ["line", "Linear"],
  ]) {
    const button = element("button", {
      type: "button",
      "aria-pressed": String(view === chosen),
    });
    button.textContent = label;
    button.addEventListener("click", () => choose(view));
    group.append(button);
  }
  return group;
}

class PlasmidMap extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    //: Which drawing is shown: the ring, or the line. A circle opens on its ring.
    this.view = "ring";
    //: Where it is, as Iki says: which actions the person may run here.
    this.at = null;
    //: Which layers are showing. Each is on once a run has found it: the person asked for
    //: those ORFs or those binding sites by running the action, so the map shows them, and
    //: a button takes any of them away again.
    this.hidden_layers = new Set();
    //: The project's primers, as Iki gives them (ADR 0117): what makes each binding site's
    //: row a way to its primer's record.
    this.primers = [];
  }

  set place(place) {
    this.at = place;
    if (this.held) this.draw();
  }

  set entity(entity) {
    this.held = entity;
    const values = entity?.values ?? {};
    // A plasmid's topology is its registry entry's, from its sequence (ADR 0168). A
    // record whose topology hasn't been filled yet is read as a circle, which every
    // plasmid record was until linear ones were held here.
    this.view = values.topology === "linear" ? "line" : "ring";
    this.draw();
  }

  set entities(entities) {
    this.primers = entities?.items ?? [];
    if (this.held) this.draw();
  }

  /** The three layers, each as what a run found, in the order they are drawn. A layer
   * whose run hasn't been is not a layer: nothing says whether it would find anything. */
  layersOf(values, length) {
    const within = (items) =>
      items
        .filter((i) => Number.isInteger(i.start) && Number.isInteger(i.end) && i.start <= length)
        .sort((a, b) => a.start - b.start);
    const ran = (field, found) =>
      found.length > 0 || (typeof values[field] === "string" && values[field] !== "");
    const made = [];
    const parts = within(Array.isArray(values.annotated_features) ? values.annotated_features : []);
    if (ran("annotated_with", parts)) {
      made.push({
        key: "parts",
        label: "Library parts",
        found: parts,
        colour: null,
        describe: describeParts,
        table: (items) => table(items, length),
      });
    }
    const orfs = within(Array.isArray(values.orfs) ? values.orfs : []);
    if (ran("orfs_found_with", orfs)) {
      made.push({
        key: "orfs",
        label: "Open reading frames",
        found: orfs,
        colour: "orf",
        describe: describeOrfs,
        table: (items) => orfTable(items, length),
      });
    }
    const sites = within(
      Array.isArray(values.primer_binding_sites) ? values.primer_binding_sites : [],
    );
    if (ran("primer_binding_sites_found_with", sites)) {
      made.push({
        key: "primers",
        label: "Primer binding sites",
        found: sites,
        colour: "primer",
        describe: describeSites,
        table: (items) => siteTable(items, length, this.primers, (hrid) => this.open(hrid)),
      });
    }
    for (const layer of made) {
      layer.shown = !this.hidden_layers.has(layer.key);
      layer.items = layer.shown ? layer.found : [];
    }
    return made;
  }

  draw() {
    const entity = this.held;
    const style = element("style");
    style.textContent = STYLE;
    const values = entity?.values ?? {};
    const length = Number(values.length) || 0;
    const circular = values.topology !== "linear";
    const layers = length ? this.layersOf(values, length) : [];
    // Nothing of the molecule is drawn until a run has read it: its length is its registry
    // entry's and the same for every project (ADR 0115), so a second project has the
    // length as soon as the record exists, before any run of its own has been. Each
    // layer's own `*_found_with` is what its run writes, with what it found.
    if (!layers.length) {
      const children = [
        style,
        element(
          "p",
          { role: "status" },
          "Its map isn't drawn yet: it's drawn once the plasmid is annotated from the library, by Annotate a plasmid from the library. A project can run it on each plasmid it gets, with its trigger Annotate each new plasmid from the library.",
        ),
      ];
      if (this.at?.runs?.includes(ANNOTATE)) {
        const button = element("button", { type: "button", className: "run" });
        button.textContent = "Run annotation";
        button.addEventListener("click", () => {
          this.dispatchEvent(new CustomEvent("iki-run", { detail: { action: ANNOTATE } }));
        });
        children.push(element("p", {}, button));
      }
      this.shadowRoot.replaceChildren(...children);
      return;
    }
    const of = (key) => layers.find((layer) => layer.key === key)?.items ?? [];
    const name = values.name ?? entity?.hrid ?? "";
    let picture;
    if (circular && this.view === "ring") {
      picture = drawing(length, of("parts"), of("orfs"), of("primers"));
      middle(picture, name, length, of("parts").length);
    } else {
      picture = lineDrawing(length, layers, circular);
      const heading = svg("text", { class: "middle name", x: LEFT, y: 14 });
      heading.textContent = [name, `${length.toLocaleString("en")} bp`].filter(Boolean).join(", ");
      picture.append(heading);
    }
    const children = [style, element("h2", {}, "Map")];
    if (circular) children.push(views(this.view, (view) => this.choose(view)));
    if (layers.length > 1) {
      children.push(layerButtons(layers, (key) => this.toggle(key)));
    }
    const shown = layers.filter((layer) => layer.shown && layer.items.length);
    children.push(
      element("p", {}, describe(length, layers, circular, this.view)),
      element(
        "div",
        { className: "map" },
        picture,
        ...shown.map((layer) => layer.table(layer.items)),
      ),
    );
    const how = [
      ["Annotated with", values.annotated_with],
      ["ORFs found with", values.orfs_found_with],
      ["Binding sites found with", values.primer_binding_sites_found_with],
    ].filter(([, said]) => typeof said === "string" && said !== "");
    if (how.length) {
      children.push(
        element(
          "dl",
          {},
          ...how.flatMap(([what, said]) => [
            element("dt", {}, what),
            element("dd", { className: "note" }, said),
          ]),
        ),
      );
    }
    this.shadowRoot.replaceChildren(...children);
  }

  /** Shows the other drawing, and leaves the keyboard on the button that was pressed. */
  choose(view) {
    if (view === this.view) return;
    this.view = view;
    this.draw();
    const buttons = this.shadowRoot.querySelectorAll(".views button");
    const chosen = [...buttons].find((b) => b.getAttribute("aria-pressed") === "true");
    chosen?.focus();
  }

  /** Turns a layer on or off, and leaves the keyboard on the button that was pressed. */
  toggle(key) {
    if (this.hidden_layers.has(key)) this.hidden_layers.delete(key);
    else this.hidden_layers.add(key);
    this.draw();
    const group = this.shadowRoot.querySelector('[aria-label="Layers"]');
    const buttons = group ? [...group.querySelectorAll("button")] : [];
    buttons.find((b) => b.textContent?.startsWith(labelFor(key)))?.focus();
  }

  /** Asks Iki to open an entity it gave this map: a primer whose binding site was clicked.
   * Iki checks that it is one of them and opens its page (ADR 0117); the map never
   * navigates itself. */
  open(hrid) {
    this.dispatchEvent(new CustomEvent("iki-open", { detail: { entity: hrid }, bubbles: true }));
  }
}

/** A layer's name, by its key: what its button reads, so the keyboard can find it again
 * after a redraw. */
function labelFor(key) {
  return { parts: "Library parts", orfs: "Open reading frames", primers: "Primer binding sites" }[
    key
  ];
}

customElements.define("plasmid-map", PlasmidMap);
