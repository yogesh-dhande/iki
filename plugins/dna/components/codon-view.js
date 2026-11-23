// The DNA plugin's Codons tab: a codon optimization read codon by codon (ADR 0203).
//
// The record's fields say how far the design moved the gene as a whole -- the codon
// adaptation index and GC content before and after, how many codons changed -- and
// `codon_changes` holds a row per codon rewritten, which for a real gene is hundreds of
// rows. Neither tells a reader the thing they want to know first: where along the gene
// the design worked and where it left the template alone. So this draws a strip, one mark
// per codon, each mark as tall as the host's relative adaptiveness for the codon now
// written there, with the rewritten codons marked apart from the ones kept.
//
// Nothing is told by colour alone: the strip is hidden from screen readers and said in
// words above it, and every codon that changed is in the table beneath with its position,
// its residue, the codon that went and the one that came, and the host's relative
// adaptiveness for each -- which is what makes the change checkable rather than asserted.
//
// A constraint no synonymous codon could meet is in its own table, placed, because a
// design that could not avoid something is the design a reader most needs to see.
//
// Until the design has been worked out it says which action works it out, and offers that
// action where the person viewing it may run it (ADR 0170).

//: The action that works a design out, which the tab offers while there is nothing drawn.
const OPTIMIZE = "iki/dna.optimize_codons";

const STYLE = `
  :host { display: block; color: CanvasText; background: Canvas; padding: 1rem; border-radius: 8px;
    font: 14px/1.45 system-ui, sans-serif; }
  h2 { font-size: 1rem; margin: 0 0 0.5rem; }
  p { margin: 0 0 0.75rem; max-width: 70ch; }
  p[role="status"] { margin-top: 0.75rem; }
  svg { display: block; width: 100%; height: auto; max-width: 52rem; }
  .axis { stroke: color-mix(in srgb, CanvasText 28%, Canvas); }
  .tick-label { fill: color-mix(in srgb, CanvasText 70%, Canvas); font-size: 9px; }
  .kept { fill: color-mix(in srgb, CanvasText 32%, Canvas); }
  .changed { fill: var(--changed); }
  .strip { margin: 0 0 0.75rem; }
  .key { display: flex; flex-wrap: wrap; gap: 0.25rem 1rem; margin: 0 0 0.75rem;
    color: color-mix(in srgb, CanvasText 70%, Canvas); font-size: 0.8rem; }
  .swatch { display: inline-block; width: 0.7rem; height: 0.7rem; border-radius: 2px;
    margin-right: 0.4rem; vertical-align: -1px; }
  .swatch.kept { background: color-mix(in srgb, CanvasText 32%, Canvas); }
  .swatch.changed { background: var(--changed); }
  table { border-collapse: collapse; font-size: 0.85rem; max-width: 100%; }
  caption { text-align: left; font-weight: 600; font-size: 0.95rem;
    padding: 1.25rem 0 0.35rem; }
  th, td { text-align: left; padding: 0.2rem 1.25rem 0.2rem 0; vertical-align: top;
    border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, Canvas); }
  th { font-weight: 600; }
  tbody th { font-weight: 400; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums;
    white-space: nowrap; }
  td.codon { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; white-space: nowrap; }
  dl { display: grid; grid-template-columns: max-content auto; gap: 0.25rem 1rem;
    margin: 0 0 1rem; }
  dt { font-weight: 600; }
  dd { margin: 0; font-variant-numeric: tabular-nums; }
  .note { color: color-mix(in srgb, CanvasText 70%, Canvas); font-variant-numeric: normal; }
  .more { color: color-mix(in srgb, CanvasText 70%, Canvas); font-size: 0.85rem; margin: 0.35rem 0 0; }
  .run { font: inherit; color: Canvas; background: CanvasText; cursor: pointer;
    padding: 0.375rem 0.875rem; border: 1px solid CanvasText; border-radius: 6px; }
  .run:focus-visible { outline: 2px solid CanvasText; outline-offset: 2px; }
`;

//: The mark for a rewritten codon, readable on Canvas in light and in dark; the codons
//: kept are drawn in the page's own ink, so the two never need the same hue to be told
//: apart, and every change is in the table as well.
const CHANGED = ["#1f6feb", "#6cb0ff"];

//: At most this many rows of changed codons, so a 10,000-codon design doesn't put 3,000
//: rows on the page. The count above the table says how many there are in all, and the
//: record's own `codon_changes` field has every one.
const ROWS = 200;

//: How the strip is drawn: a lane of marks over a baseline, in a 1,000-wide viewBox so
//: it scales to whatever width the tab has.
const WIDTH = 1000;
const HEIGHT = 120;
const TOP = 10;
const LANE = 80;
const LEFT = 4;

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

function cell(text, className) {
  const out = element("td", className ? { className } : {});
  out.textContent = text;
  return out;
}

function head(text, className) {
  const out = element("th", { scope: "col", ...(className ? { className } : {}) });
  out.textContent = text;
  return out;
}

/** A row's own header, so a screen reader reading across a row says which codon it is on. */
function rowHead(text, className) {
  const out = element("th", { scope: "row", ...(className ? { className } : {}) });
  out.textContent = text;
  return out;
}

/** A number as the record keeps it, or an em dash where there is none: a codon adaptation
 * index is absent for a gene with no codon the index counts, which the record says with
 * null rather than with a nought. */
function said(value, digits = 2) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
}

/** The codons the design wrote, each with the relative adaptiveness of the codon now
 * there and whether it was rewritten, from `codons` and `codon_changes`: the changes name
 * the codons that moved, so every other position is a codon the template already had. */
function laneOf(total, changes) {
  const byCodon = new Map();
  for (const change of changes) {
    if (Number.isInteger(change.codon)) byCodon.set(change.codon, change);
  }
  const out = [];
  for (let at = 1; at <= total; at += 1) {
    const change = byCodon.get(at);
    out.push({
      at,
      changed: Boolean(change),
      // A kept codon's adaptiveness isn't recorded -- only the changes are -- so a mark
      // for one is drawn at full height and the table says nothing about it either.
      weight: change && typeof change.to_weight === "number" ? change.to_weight : null,
    });
  }
  return out;
}

/** Ticks every 10, 25, 50 codons and so on: the first step that gives at most ten. */
function tickEvery(total) {
  for (const step of [10, 25, 50, 100, 250, 500, 1000, 2500, 5000]) {
    if (total / step <= 10) return step;
  }
  return 10000;
}

/** The strip: one mark per codon over a baseline, the rewritten ones apart from the kept
 * ones, each as tall as the host's relative adaptiveness for the codon now written. It is
 * hidden from screen readers, which the paragraph above it and the table beneath serve. */
function drawing(lane) {
  const span = WIDTH - 2 * LEFT;
  // A mark is at most this wide, so a gene of a few codons is a strip rather than a
  // row of slabs; a long gene's marks are as thin as a tenth of a unit.
  const each = Math.min(Math.max(span / Math.max(lane.length, 1), 0.4), 12);
  // A gene too short to fill the strip is drawn in the width it needs, and the strip is
  // that much narrower on the page, rather than stretched across it.
  const used = Math.min(2 * LEFT + each * lane.length, WIDTH);
  const picture = svg("svg", {
    viewBox: `0 0 ${used.toFixed(2)} ${HEIGHT}`,
    "aria-hidden": "true",
    class: "strip",
    style: `max-width: ${((used / WIDTH) * 52).toFixed(2)}rem`,
  });
  const wide = Math.max(each - Math.min(each * 0.25, 1), 0.4);
  for (const codon of lane) {
    const height = LANE * (codon.weight === null ? 1 : Math.max(codon.weight, 0.04));
    picture.append(
      svg("rect", {
        x: (LEFT + (codon.at - 1) * each).toFixed(2),
        y: (TOP + LANE - height).toFixed(2),
        width: wide.toFixed(2),
        height: height.toFixed(2),
        class: codon.changed ? "changed" : "kept",
      }),
    );
  }
  picture.append(
    svg("line", {
      x1: LEFT,
      y1: TOP + LANE,
      x2: (used - LEFT).toFixed(2),
      y2: TOP + LANE,
      class: "axis",
    }),
  );
  const step = tickEvery(lane.length);
  for (let at = step; at <= lane.length; at += step) {
    const x = LEFT + (at - 0.5) * each;
    picture.append(
      svg("line", {
        x1: x.toFixed(2),
        y1: TOP + LANE,
        x2: x.toFixed(2),
        y2: TOP + LANE + 4,
        class: "axis",
      }),
    );
    const label = svg("text", {
      x: x.toFixed(2),
      y: TOP + LANE + 16,
      class: "tick-label",
      "text-anchor": "middle",
    });
    label.textContent = at.toLocaleString("en");
    picture.append(label);
  }
  return picture;
}

/** What the strip draws, in words, for anyone who isn't looking at it. */
function describe(values, lane) {
  const changed = lane.filter((codon) => codon.changed).length;
  const where = [];
  const third = Math.ceil(lane.length / 3);
  for (const [from, name] of [
    [0, "its first third"],
    [third, "its middle third"],
    [2 * third, "its last third"],
  ]) {
    const part = lane.slice(from, from + third);
    const moved = part.filter((codon) => codon.changed).length;
    if (part.length) where.push(`${moved} of ${part.length} in ${name}`);
  }
  const index =
    typeof values.source_cai === "number" && typeof values.optimized_cai === "number"
      ? ` Its codon adaptation index against the host's table went from ${said(values.source_cai)} to ${said(values.optimized_cai)}.`
      : "";
  return (
    `${changed.toLocaleString("en")} of ${lane.length.toLocaleString("en")} codons were ` +
    `rewritten, ${where.join(", ")}, each still coding for the amino acid the template's ` +
    `codon coded for. Each mark below is one codon, left to right along the gene, as tall ` +
    `as the host's relative adaptiveness for the codon now written there.${index}`
  );
}

/** How far the design moved the gene, as the record keeps it. */
function summary(values) {
  const rows = [
    ["Codons", (Number(values.codons) || 0).toLocaleString("en")],
    [
      "Rewritten",
      `${(Number(values.codons_changed) || 0).toLocaleString("en")} of ${(Number(values.codons) || 0).toLocaleString("en")}`,
    ],
    ["Protein", `${(Number(values.protein_length) || 0).toLocaleString("en")} residues, unchanged`],
    ["Codon adaptation index", `${said(values.source_cai)} → ${said(values.optimized_cai)}`],
    [
      "GC content",
      `${said(values.source_gc_content, 1)}% → ${said(values.optimized_gc_content, 1)}%`,
    ],
  ];
  const list = element("dl", {});
  for (const [what, value] of rows) {
    list.append(element("dt", {}, what), element("dd", {}, value));
  }
  if (typeof values.cai_reference === "string" && values.cai_reference !== "") {
    list.append(
      element("dt", {}, "Index against"),
      element("dd", { className: "note" }, values.cai_reference),
    );
  }
  return list;
}

/** Every codon the design rewrote, with the host's relative adaptiveness for the codon
 * that went and the one that came: the change itself, checkable. */
function changeTable(changes) {
  const table = element("table", {});
  const caption = element("caption");
  caption.textContent = `Codons rewritten (${changes.length.toLocaleString("en")})`;
  const header = element("tr", {});
  header.append(
    head("Codon", "num"),
    head("Residue"),
    head("From"),
    head("w"),
    head("To"),
    head("w"),
  );
  const body = element("tbody", {});
  for (const change of changes.slice(0, ROWS)) {
    const row = element("tr", {});
    row.append(
      rowHead(Number(change.codon).toLocaleString("en"), "num"),
      cell(String(change.residue ?? "")),
      cell(String(change.from ?? ""), "codon"),
      cell(said(change.from_weight)),
      cell(String(change.to ?? ""), "codon"),
      cell(said(change.to_weight)),
    );
    body.append(row);
  }
  table.append(caption, element("thead", {}, header), body);
  return table;
}

/** What no synonymous codon could meet, placed: the design is registered with it rather
 * than quietly breaking what was asked for. */
function unmetTable(unmet) {
  const table = element("table", {});
  const caption = element("caption");
  caption.textContent = `Constraints no synonymous codon could meet (${unmet.length.toLocaleString("en")})`;
  const header = element("tr", {});
  header.append(head("Bases", "num"), head("What"));
  const body = element("tbody", {});
  for (const violation of unmet.slice(0, ROWS)) {
    const row = element("tr", {});
    const from = Number(violation.start).toLocaleString("en");
    const to = Number(violation.end).toLocaleString("en");
    row.append(rowHead(`${from}–${to}`, "num"), cell(String(violation.detail ?? "")));
    body.append(row);
  }
  table.append(caption, element("thead", {}, header), body);
  return table;
}

class CodonView extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    //: Where it is, as Iki says: which actions the person may run here.
    this.at = null;
  }

  set place(place) {
    this.at = place;
    if (this.held) this.draw();
  }

  set entity(entity) {
    this.held = entity;
    this.draw();
  }

  draw() {
    const values = this.held?.values ?? {};
    const style = element("style");
    style.textContent = STYLE;
    const total = Number(values.codons) || 0;
    // Nothing is drawn until the action has been run: the design is a record of what was
    // asked for, and the codons, the index and the changes are all a run's.
    if (!total) {
      const children = [
        style,
        element(
          "p",
          { role: "status" },
          "Its codons aren't worked out yet: the design is read, and the sequence it " +
            "makes registered, by Rewrite the coding region in the host's codons.",
        ),
      ];
      if (this.at?.runs?.includes(OPTIMIZE)) {
        const button = element("button", { type: "button", className: "run" });
        button.textContent = "Run the optimization";
        button.addEventListener("click", () => {
          this.dispatchEvent(new CustomEvent("iki-run", { detail: { action: OPTIMIZE } }));
        });
        children.push(element("p", {}, button));
      }
      this.shadowRoot.replaceChildren(...children);
      return;
    }
    const changes = Array.isArray(values.codon_changes) ? values.codon_changes : [];
    const unmet = Array.isArray(values.unmet_constraints) ? values.unmet_constraints : [];
    const lane = laneOf(total, changes);
    const children = [
      style,
      element("h2", {}, "Codons"),
      summary(values),
      element("p", {}, describe(values, lane)),
    ];
    const key = element("p", { className: "key" });
    key.append(
      element("span", {}, element("span", { className: "swatch changed" }), "Rewritten"),
      element(
        "span",
        {},
        element("span", { className: "swatch kept" }),
        "Kept as the template had it",
      ),
    );
    children.push(key, drawing(lane));
    if (changes.length) {
      children.push(changeTable(changes));
      if (changes.length > ROWS) {
        const more = element("p", { className: "more" });
        more.textContent = `The first ${ROWS.toLocaleString("en")} of ${changes.length.toLocaleString("en")} are shown; the record's Codon changes field has every one.`;
        children.push(more);
      }
    }
    if (unmet.length) {
      children.push(unmetTable(unmet));
    } else {
      const met = element("p", { role: "status" });
      met.textContent = "Every constraint the design asked for was met.";
      children.push(met);
    }
    if (typeof values.optimized_with === "string" && values.optimized_with !== "") {
      children.push(
        element(
          "dl",
          {},
          element("dt", {}, "Optimized with"),
          element("dd", { className: "note" }, values.optimized_with),
        ),
      );
    }
    this.shadowRoot.host.style.setProperty("--changed", dark() ? CHANGED[1] : CHANGED[0]);
    this.shadowRoot.replaceChildren(...children);
  }
}

customElements.define("codon-view", CodonView);
