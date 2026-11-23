// The DNA plugin's Changes tab: a site-directed mutagenesis read change by change, with
// the oligos to order (ADR 0204).
//
// The record's fields say what was asked for and what was made -- the changes as stated
// and as HGVS names them, what they do to the protein, the primers designed -- but a
// change is a place on a molecule, and where along the template the changes fall is the
// first thing a reader wants: two changes 30 bases apart can share a primer pair, two
// 3,000 apart cannot. So this draws the template as a line with a mark where each change
// lies, wide where the change is wide, and then the changes themselves in a table.
//
// Nothing is told by colour alone: the strip is hidden from screen readers and said in
// words above it, and every change, every protein consequence and every primer is in a
// table beneath, which is what makes a design checkable rather than asserted.
//
// The primers are the table a person actually copies out of, so they carry their bases in
// one monospaced piece, how much of each matches the template, and the measure the
// protocol is written around under its own name -- never as "the Tm", which belongs to
// the registered primer's own nearest-neighbour value.
//
// Until the changes have been made it says which action makes them, and offers that
// action where the person viewing it may run it (ADR 0170).

//: The action that makes the changes, which the tab offers while there is nothing drawn.
const APPLY = "iki/dna.apply_mutagenesis";

const STYLE = `
  :host { display: block; color: CanvasText; background: Canvas; padding: 1rem; border-radius: 8px;
    font: 14px/1.45 system-ui, sans-serif; }
  p { margin: 0 0 0.75rem; max-width: 70ch; }
  p[role="status"] { margin-top: 0.75rem; }
  svg { display: block; width: 100%; height: auto; max-width: 52rem; }
  .axis { stroke: color-mix(in srgb, CanvasText 28%, Canvas); }
  .tick-label { fill: color-mix(in srgb, CanvasText 70%, Canvas); font-size: 9px; }
  .change { fill: var(--change); }
  .strip { margin: 0 0 0.75rem; }
  .key { display: flex; flex-wrap: wrap; gap: 0.25rem 1rem; margin: 0 0 0.75rem;
    color: color-mix(in srgb, CanvasText 70%, Canvas); font-size: 0.8rem; }
  .swatch { display: inline-block; width: 0.7rem; height: 0.7rem; border-radius: 2px;
    margin-right: 0.4rem; vertical-align: -1px; }
  .swatch.change { background: var(--change); }
  table { border-collapse: collapse; font-size: 0.85rem; max-width: 100%; }
  caption { text-align: left; font-weight: 600; font-size: 0.95rem;
    padding: 1.25rem 0 0.35rem; }
  th, td { text-align: left; padding: 0.2rem 1.25rem 0.2rem 0; vertical-align: top;
    border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, Canvas); }
  th { font-weight: 600; }
  tbody th { font-weight: 400; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums;
    white-space: nowrap; }
  td.bases, th.bases, td.name { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  td.bases { white-space: nowrap; }
  td.name { white-space: nowrap; }
  /* An oligo is 40 letters that can't be hyphenated, and six columns of a primer table
     don't fit a phone. The table scrolls inside its own region rather than taking the
     page sideways, and the region is focusable so it can be scrolled from the keyboard. */
  .scroll { overflow-x: auto; max-width: 100%; }
  /* Inside it the table keeps its own width, so a row is one line to read across rather
     than six letters of an oligo stacked twenty deep. */
  .scroll table { min-width: max-content; }
  .scroll:focus-visible { outline: 2px solid CanvasText; outline-offset: 2px; }
  dl { display: grid; grid-template-columns: max-content auto; gap: 0.25rem 1rem;
    margin: 0 0 1rem; }
  dt { font-weight: 600; }
  dd { margin: 0; font-variant-numeric: tabular-nums; }
  .note { color: color-mix(in srgb, CanvasText 70%, Canvas); font-variant-numeric: normal; }
  .run { font: inherit; color: Canvas; background: CanvasText; cursor: pointer;
    padding: 0.375rem 0.875rem; border: 1px solid CanvasText; border-radius: 6px; }
  .run:focus-visible { outline: 2px solid CanvasText; outline-offset: 2px; }
`;

//: The mark for a change, readable on Canvas in light and in dark; the template itself is
//: drawn in the page's own ink, so the two never need the same hue to be told apart, and
//: every change is in the table as well.
const CHANGE = ["#1f6feb", "#6cb0ff"];

//: How the strip is drawn: a lane of marks over the template's own line, in a 1,000-wide
//: viewBox so it scales to whatever width the tab has.
const WIDTH = 1000;
const HEIGHT = 86;
const TOP = 10;
const LANE = 46;
const LEFT = 4;

//: The narrowest a mark is drawn, so a single base changed in a 10 kb plasmid is still
//: something a reader can see.
const LEAST = 3;

//: How each kind of change is said, for the strip's words and the table.
const KINDS = {
  substitution: "a substitution",
  deletion: "a deletion",
  insertion: "an insertion",
  duplication: "a duplication",
  inversion: "an inversion",
  "deletion-insertion": "a deletion-insertion",
};

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

/** A row's own header, so a screen reader reading across a row says which change it is on. */
function rowHead(text, className) {
  const out = element("th", { scope: "row", ...(className ? { className } : {}) });
  out.textContent = text;
  return out;
}

/** A count said with its thousands separators, or an em dash where the record has none. */
function counted(value) {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("en") : "—";
}

/** How many bases of the template a change covers: a substitution one, a deletion as many
 * as it takes away, an insertion none at all -- HGVS gives an insertion the pair of bases
 * it goes between, so its range ends one before it starts, and it is drawn as the join it
 * is rather than as the whole molecule. A change running across a circular origin also
 * ends before it starts, and is drawn from where it starts to the end of the line. */
function spanOf(change, length) {
  const start = Number(change.start);
  const end = Number(change.end);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
  if (String(change.kind) === "insertion") return { start, bases: 0 };
  const over = end >= start ? end - start + 1 : length - start + 1;
  return { start, bases: Math.max(over, 1) };
}

/** Ticks every 100, 250, 500 bases and so on: the first step that gives at most ten. */
function tickEvery(total) {
  for (const step of [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000]) {
    if (total / step <= 10) return step;
  }
  return 100000;
}

/** The strip: the template as a line with a mark where each change lies, as wide as the
 * change is. It is hidden from screen readers, which the paragraph above it and the
 * table beneath serve. */
function drawing(changes, length) {
  const span = WIDTH - 2 * LEFT;
  const each = span / Math.max(length, 1);
  const picture = svg("svg", {
    viewBox: `0 0 ${WIDTH} ${HEIGHT}`,
    "aria-hidden": "true",
    class: "strip",
  });
  picture.append(
    svg("line", { x1: LEFT, y1: TOP + LANE, x2: WIDTH - LEFT, y2: TOP + LANE, class: "axis" }),
  );
  for (const change of changes) {
    const where = spanOf(change, length);
    if (where === null) continue;
    const wide = Math.max(where.bases * each, LEAST);
    picture.append(
      svg("rect", {
        x: Math.min(LEFT + (where.start - 1) * each, WIDTH - LEFT - wide).toFixed(2),
        y: TOP,
        width: wide.toFixed(2),
        height: LANE,
        class: "change",
      }),
    );
  }
  const step = tickEvery(length);
  for (let at = step; at <= length; at += step) {
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
function describe(changes, length, product) {
  const where = changes
    .map((change) => {
      const kind = KINDS[String(change.kind)] ?? "a change";
      const at = Number(change.start);
      return `${kind} at ${Number.isFinite(at) ? at.toLocaleString("en") : "an unrecorded base"}`;
    })
    .join(", ");
  const net = product - length;
  const grew =
    net === 0
      ? "and the mutant is the template's own length"
      : `and the mutant is ${Math.abs(net).toLocaleString("en")} bases ${net > 0 ? "longer" : "shorter"}`;
  return (
    `${changes.length.toLocaleString("en")} change${changes.length === 1 ? "" : "s"} along ` +
    `the template's ${length.toLocaleString("en")} bases: ${where}, ${grew}. Each mark ` +
    `below is one change, placed along the template left to right and as wide as the ` +
    `change is.`
  );
}

/** What the design made, as the record keeps it. */
function summary(values, changes) {
  const list = element("dl", {});
  const rows = [
    ["Changes", counted(changes.length)],
    ["Mutant", `${counted(Number(values.product_length))} bp`],
    ["Primers designed", counted(Number(values.primers_designed))],
  ];
  for (const [what, value] of rows) {
    list.append(element("dt", {}, what), element("dd", {}, value));
  }
  if (typeof values.designed_with === "string" && values.designed_with !== "") {
    list.append(
      element("dt", {}, "Designed with"),
      element("dd", { className: "note" }, values.designed_with),
    );
  }
  return list;
}

/** Every change, as it was stated and as HGVS names it where it lies: the two differ
 * wherever the bases let the change slide 3', and a reader comparing a design with a
 * sequencing report needs both. */
function changeTable(changes) {
  const table = element("table", {});
  const caption = element("caption");
  caption.textContent = `Changes (${changes.length.toLocaleString("en")})`;
  const header = element("tr", {});
  header.append(head("Stated"), head("As HGVS names it"), head("What"), head("Bases", "num"));
  const body = element("tbody", {});
  for (const change of changes) {
    const row = element("tr", {});
    const difference = Number(change.difference);
    row.append(
      rowHead(String(change.stated ?? ""), "name"),
      cell(String(change.name ?? ""), "name"),
      cell(KINDS[String(change.kind)] ?? String(change.kind ?? "")),
      cell(
        Number.isFinite(difference) && difference !== 0
          ? `${difference > 0 ? "+" : "−"}${Math.abs(difference).toLocaleString("en")}`
          : "no change",
        "num",
      ),
    );
    body.append(row);
  }
  table.append(caption, element("thead", {}, header), body);
  return table;
}

/** What the changes do to the protein, where the record says where the coding region is. */
function proteinTable(protein) {
  const table = element("table", {});
  const caption = element("caption");
  caption.textContent = "What it does to the protein";
  const header = element("tr", {});
  header.append(head("Protein change"), head("What"));
  const body = element("tbody", {});
  for (const change of protein) {
    const row = element("tr", {});
    row.append(
      rowHead(String(change.name ?? ""), "name"),
      cell(String(change.effect ?? "").replaceAll("_", " ")),
    );
    body.append(row);
  }
  table.append(caption, element("thead", {}, header), body);
  return table;
}

/** The oligos to order: the bases in one piece, how much of each matches the template,
 * and the protocol's own acceptance measure under its own name. */
function primerTable(primers) {
  const table = element("table", {});
  const caption = element("caption");
  caption.textContent = `Primers to order (${primers.length.toLocaleString("en")})`;
  const header = element("tr", {});
  header.append(
    head("For"),
    head("Part"),
    head("Bases, 5' to 3'", "bases"),
    head("Length", "num"),
    head("Matching", "num"),
    head("Measured"),
  );
  const body = element("tbody", {});
  for (const primer of primers) {
    const row = element("tr", {});
    const measures = Array.isArray(primer.measures) ? primer.measures : [];
    row.append(
      rowHead(String(primer.change ?? ""), "name"),
      cell(String(primer.role ?? "")),
      cell(String(primer.bases ?? ""), "bases"),
      cell(counted(Number(primer.length)), "num"),
      cell(counted(Number(primer.matched)), "num"),
      cell(
        measures
          .map((measure) => `${measure.what} ${Number(measure.value).toFixed(1)} °C`)
          .join("; "),
      ),
    );
    body.append(row);
  }
  table.append(caption, element("thead", {}, header), body);
  return table;
}

/** A table in a region that scrolls sideways on its own, focusable so a keyboard can
 * scroll it: the page itself never goes sideways, however long an oligo is. */
function scrolling(table, label) {
  return element(
    "div",
    { className: "scroll", tabindex: "0", role: "group", "aria-label": label },
    table,
  );
}

class MutagenesisView extends HTMLElement {
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
    const changes = Array.isArray(values.changes_made) ? values.changes_made : [];
    // Nothing is drawn until the action has been run: the design is a record of what was
    // asked for, and where the changes landed, the mutant and the primers are all a run's.
    if (!changes.length) {
      const children = [
        style,
        element(
          "p",
          { role: "status" },
          "Its changes aren't made yet: they are checked against the template, made, and " +
            "the mutant and primers registered, by Make the changes and design the primers.",
        ),
      ];
      if (this.at?.runs?.includes(APPLY)) {
        const button = element("button", { type: "button", className: "run" });
        button.textContent = "Make the changes";
        button.addEventListener("click", () => {
          this.dispatchEvent(new CustomEvent("iki-run", { detail: { action: APPLY } }));
        });
        children.push(element("p", {}, button));
      }
      this.shadowRoot.replaceChildren(...children);
      return;
    }
    const product = Number(values.product_length) || 0;
    // The template's own length, which the record doesn't keep: the mutant's, less what
    // the changes added or took away. A design's changes are stated against the template,
    // so that is the ruler the strip is drawn on.
    const added = changes.reduce((total, change) => total + (Number(change.difference) || 0), 0);
    const length = Math.max(product - added, 1);
    const protein = Array.isArray(values.protein_changes) ? values.protein_changes : [];
    const primers = Array.isArray(values.primers) ? values.primers : [];
    const children = [
      style,
      summary(values, changes),
      element("p", {}, describe(changes, length, product)),
    ];
    const key = element("p", { className: "key" });
    key.append(
      element("span", {}, element("span", { className: "swatch change" }), "Changed"),
      element("span", { className: "note" }, "The line is the template, base 1 at its left."),
    );
    children.push(key, drawing(changes, length), scrolling(changeTable(changes), "Changes"));
    if (protein.length) {
      children.push(scrolling(proteinTable(protein), "What it does to the protein"));
    }
    if (primers.length) {
      children.push(scrolling(primerTable(primers), "Primers to order"));
    } else {
      const none = element("p", { role: "status" });
      none.textContent =
        "No primers were designed: the design asks for none, or a change runs across the " +
        "template's origin, which is designed against the registered mutant instead.";
      children.push(none);
    }
    this.shadowRoot.host.style.setProperty("--change", dark() ? CHANGE[1] : CHANGE[0]);
    this.shadowRoot.replaceChildren(...children);
  }
}

customElements.define("mutagenesis-view", MutagenesisView);
