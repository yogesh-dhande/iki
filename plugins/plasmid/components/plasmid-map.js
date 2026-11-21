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
// The drawing is described in words for screen readers, and every part is in the table
// below it with its coordinates, so nothing is told by colour alone. The two views are
// buttons in a group, reachable and operable from the keyboard like any button, each
// saying whether it is the view being shown (`aria-pressed`).
//
// Until the plasmid is annotated it says which action annotates it, and offers that
// action where the person viewing it may run it (ADR 0169): the trigger that annotates
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

const SIZE = 340;
const CENTRE = SIZE / 2;
const RADIUS = 120;
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

function drawing(length, features) {
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
const SPINE = LANES * LANE + 26;
const TALL = SPINE + LANES * LANE + 30;

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

function lineDrawing(length, features, circular) {
  const parts = svg("svg", {
    viewBox: `0 0 ${WIDE} ${TALL}`,
    width: WIDE,
    height: TALL,
    // Hidden from screen readers: the paragraph above it says what it draws, in words,
    // and the table below it gives every part.
    "aria-hidden": "true",
  });
  parts.append(
    svg("line", {
      class: "line",
      x1: LEFT,
      y1: SPINE,
      x2: RIGHT,
      y2: SPINE,
      "stroke-width": 8,
    }),
  );
  // A linear molecule's two ends are its own, so they are drawn as ends; a circle drawn
  // as a line is cut at base 1, which the words beneath say.
  if (!circular) {
    for (const x of [LEFT, RIGHT]) {
      parts.append(
        svg("line", {
          class: "cap",
          x1: x,
          y1: SPINE - 12,
          x2: x,
          y2: SPINE + 12,
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
        y1: SPINE + 8,
        x2: x.toFixed(1),
        y2: SPINE + 14,
        "stroke-width": 1,
      }),
    );
    const label = svg("text", {
      class: "tick-label",
      x: x.toFixed(1),
      y: SPINE + 26,
      "text-anchor": "middle",
      "dominant-baseline": "middle",
    });
    label.textContent = tickLabel(place, step);
    parts.append(label);
  }
  const above = Array.from({ length: LANES }, () => []);
  const below = Array.from({ length: LANES }, () => []);
  for (const feature of features) {
    const forward = feature.strand !== "reverse";
    const pieces = piecesOf(feature, length);
    const covers = [pieces[0][0], pieces[pieces.length - 1][1]];
    const lane = laneFor(forward ? above : below, covers);
    const y = forward ? SPINE - 20 - lane * LANE : SPINE + 34 + lane * LANE;
    const colour = colourOf(feature.qualifiers?.role);
    // The arrowhead goes on the stretch that holds the part's 3' end.
    const tipped = forward ? pieces.length - 1 : 0;
    pieces.forEach(([from, to], i) => {
      for (const drawn of bar(from, to, length, y, forward, i === tipped)) {
        drawn.setAttribute(drawn.tagName === "line" ? "stroke" : "fill", colour);
        parts.append(drawn);
      }
    });
  }
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

/** What the drawing shows, in words, so a screen reader is told everything the picture
 * tells: the molecule, which way it is drawn, and every part along it. */
function describe(length, features, circular, view) {
  const bp = `${length.toLocaleString("en")} base pairs`;
  const shape = circular
    ? view === "line"
      ? `A circle of ${bp}, drawn as a line cut at base 1, so a part across its origin is drawn in two pieces`
      : `A circle of ${bp}, drawn as a ring with base 1 at the top, going clockwise`
    : `A linear molecule of ${bp}, drawn as a line from base 1 at the left to base ${length.toLocaleString("en")} at the right, with an end at each`;
  if (!features.length) return `${shape}, with none of the library's parts found along it.`;
  const each = features
    .map(
      (f) =>
        `${f.qualifiers?.label || f.name}, ${f.start.toLocaleString("en")} to ${f.end.toLocaleString("en")} on the ${f.strand === "reverse" ? "reverse" : "forward"} strand`,
    )
    .join("; ");
  const count = `${features.length} ${features.length === 1 ? "part" : "parts"}`;
  return `${shape}, with ${count} of the library found along it: ${each}.`;
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

  draw() {
    const entity = this.held;
    const style = element("style");
    style.textContent = STYLE;
    const values = entity?.values ?? {};
    const features = Array.isArray(values.annotated_features) ? values.annotated_features : [];
    const length = Number(values.length) || 0;
    const circular = values.topology !== "linear";
    // Whether this project's record has been annotated, which is what the map draws.
    // Its length is its registry entry's and the same for every project (ADR 0115), so
    // a second project has the length as soon as the record exists, before its own
    // annotation has run: `annotated_with` is written by that run, with the features.
    const annotated =
      features.length > 0 ||
      (typeof values.annotated_with === "string" && values.annotated_with !== "");
    if (!length || !annotated) {
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
    const kept = features
      .filter((f) => Number.isInteger(f.start) && Number.isInteger(f.end) && f.start <= length)
      .sort((a, b) => a.start - b.start);
    const name = values.name ?? entity?.hrid ?? "";
    let picture;
    if (circular && this.view === "ring") {
      picture = drawing(length, kept);
      middle(picture, name, length, kept.length);
    } else {
      picture = lineDrawing(length, kept, circular);
      const heading = svg("text", { class: "middle name", x: LEFT, y: 14 });
      heading.textContent = [name, `${length.toLocaleString("en")} bp`].filter(Boolean).join(", ");
      picture.append(heading);
    }
    const children = [style, element("h2", {}, "Map")];
    if (circular) children.push(views(this.view, (view) => this.choose(view)));
    children.push(
      element("p", {}, describe(length, kept, circular, this.view)),
      element("div", { className: "map" }, picture, kept.length ? table(kept, length) : ""),
    );
    if (values.annotated_with) {
      children.push(
        element(
          "dl",
          {},
          element("dt", {}, "Annotated with"),
          element("dd", { className: "note" }, values.annotated_with),
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
}

customElements.define("plasmid-map", PlasmidMap);
