// The DNA plugin's Guides tab: a CRISPR guide design read guide by guide (ADR 0205).
//
// The record's `guides` field is a row per site with that site's off-targets nested
// inside it, which no field display can show, and it says nothing about the thing a
// reader wants first: where along the target the guides fall. A region with its guides
// bunched into twenty bases is a different problem from one with them spread over a
// kilobase, and a region whose guides are all on one strand is a third. So this draws the
// target as a line with a mark where each guide cuts -- the forward-strand guides above
// it and the reverse-strand ones below, so the strands are told apart by where they are
// drawn and not by colour -- and then the guides themselves in tables.
//
// Nothing is told by colour alone: the strip is hidden from screen readers and said in
// words above it, and every guide, every flag and every off-target is in a table beneath,
// which is what makes a design checkable rather than asserted.
//
// A specificity is a number about a search, so the summary says what was searched. Where
// nothing was, the tab says so rather than letting an absent number read as a good one.
//
// Until the guides have been found it says which action finds them, and offers that
// action where the person viewing it may run it (ADR 0170).

//: The action that finds the guides, which the tab offers while there is nothing drawn.
const DESIGN = "iki/dna.design_guides";

const STYLE = `
  :host { display: block; color: CanvasText; background: Canvas; padding: 1rem; border-radius: 8px;
    font: 14px/1.45 system-ui, sans-serif; }
  p { margin: 0 0 0.75rem; max-width: 70ch; }
  p[role="status"] { margin-top: 0.75rem; }
  svg { display: block; width: 100%; height: auto; max-width: 52rem; }
  .axis { stroke: color-mix(in srgb, CanvasText 28%, Canvas); }
  .tick-label { fill: color-mix(in srgb, CanvasText 70%, Canvas); font-size: 9px; }
  .guide { fill: var(--guide); }
  .strip { margin: 0 0 0.75rem; }
  .key { display: flex; flex-wrap: wrap; gap: 0.25rem 1rem; margin: 0 0 0.75rem;
    color: color-mix(in srgb, CanvasText 70%, Canvas); font-size: 0.8rem; }
  .swatch { display: inline-block; width: 0.7rem; height: 0.7rem; border-radius: 2px;
    margin-right: 0.4rem; vertical-align: -1px; background: var(--guide); }
  table { border-collapse: collapse; font-size: 0.85rem; max-width: 100%; }
  caption { text-align: left; font-weight: 600; font-size: 0.95rem;
    padding: 1.25rem 0 0.35rem; }
  th, td { text-align: left; padding: 0.2rem 1.25rem 0.2rem 0; vertical-align: top;
    border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, Canvas); }
  th { font-weight: 600; }
  tbody th { font-weight: 400; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums;
    white-space: nowrap; }
  td.bases, th.bases { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    white-space: nowrap; }
  td.bases .pam { color: color-mix(in srgb, CanvasText 60%, Canvas); }
  dl { display: grid; grid-template-columns: max-content auto; gap: 0.25rem 1rem;
    margin: 0 0 1rem; }
  dt { font-weight: 600; }
  dd { margin: 0; font-variant-numeric: tabular-nums; }
  .note { color: color-mix(in srgb, CanvasText 70%, Canvas); font-variant-numeric: normal; }
  .more { color: color-mix(in srgb, CanvasText 70%, Canvas); font-size: 0.85rem;
    margin: 0.35rem 0 0; }
  /* A protospacer is 20 letters that can't be hyphenated and the off-target table has
     eight columns, which don't fit a phone. Each table scrolls inside its own region
     rather than taking the page sideways, and the region is focusable so it can be
     scrolled from the keyboard. */
  .scroll { overflow-x: auto; max-width: 100%; }
  .scroll table { min-width: max-content; }
  .scroll:focus-visible { outline: 2px solid CanvasText; outline-offset: 2px; }
  .run { font: inherit; color: Canvas; background: CanvasText; cursor: pointer;
    padding: 0.375rem 0.875rem; border: 1px solid CanvasText; border-radius: 6px; }
  .run:focus-visible { outline: 2px solid CanvasText; outline-offset: 2px; }
`;

//: The mark for a guide, readable on Canvas in light and in dark. The two strands are
//: told apart by which side of the line they are drawn on, never by hue.
const GUIDE = ["#1f6feb", "#6cb0ff"];

//: At most this many rows of off-targets, so a guide searched against a genome doesn't
//: put thousands on the page. The count above the table says how many there are.
const ROWS = 200;

//: How the strip is drawn: two lanes either side of the target's own line, in a
//: 1,000-wide viewBox so it scales to whatever width the tab has.
const WIDTH = 1000;
const HEIGHT = 110;
const TOP = 8;
const LANE = 32;
const LEFT = 4;
//: The narrowest a guide is drawn, so one in a 10 kb target is still something to see.
const LEAST = 3;

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

/** A row's own header, so a screen reader reading across a row says which guide it is on. */
function rowHead(text, className) {
  const out = element("th", { scope: "row", ...(className ? { className } : {}) });
  out.textContent = text;
  return out;
}

/** A table in a region that scrolls sideways on its own, focusable so a keyboard can
 * scroll it: the page itself never goes sideways, however long a protospacer is. */
function scrolling(table, label) {
  return element(
    "div",
    { className: "scroll", tabindex: "0", role: "group", "aria-label": label },
    table,
  );
}

function counted(value) {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("en") : "—";
}

/** A number the record may not have. It matters that this is not `Number(value)`: that
 * turns a recorded null into 0, and a specificity of nought reads as a guide that cuts
 * everywhere, which is the opposite of a guide nobody computed a specificity for. */
function maybe(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** The guide's bases with its PAM beside them, the PAM set apart so a reader can see
 * which part the guide is and which part the DNA must already have. Cas12a reads its PAM
 * before the protospacer and Cas9 after it, which is why the order isn't fixed. */
function withPam(guide, pamFirst, tag = "td") {
  const out =
    tag === "th"
      ? element("th", { scope: "row", className: "bases" })
      : element("td", { className: "bases" });
  const pam = element("span", { className: "pam" });
  pam.textContent = String(guide.pam ?? "");
  const spacer = document.createTextNode(String(guide.protospacer ?? ""));
  if (pamFirst) out.append(pam, document.createTextNode(" "), spacer);
  else out.append(spacer, document.createTextNode(" "), pam);
  return out;
}

/** Ticks every 100, 250, 500 bases and so on: the first step that gives at most ten. */
function tickEvery(total) {
  for (const step of [10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000]) {
    if (total / step <= 10) return step;
  }
  return 100000;
}

/** Where along the drawn stretch a guide lies, and how wide it is. */
function placeOf(guide, first) {
  const start = Number(guide.start);
  const end = Number(guide.end);
  if (!Number.isFinite(start)) return null;
  // A guide across a circular target's origin ends before it starts; it is drawn from
  // where it starts to the end of the line, which is where it is.
  const bases = Number.isFinite(end) && end >= start ? end - start + 1 : 1;
  return { at: start - first, bases };
}

/** Guides stacked into rows so none covers another: the first row that has nothing where
 * this guide goes takes it. Guides 20 bases long in a 100-base region overlap almost
 * every one of their neighbours, and drawn in one row they merge into a slab that says
 * nothing about how many there are. */
function stacked(guides, first) {
  const rows = [];
  const out = [];
  // Guides arrive most particular first, which is the order to read them in but not the
  // order to pack them in: taken left to right, each row holds as many as it can.
  const along = guides
    .map((guide) => placeOf(guide, first))
    .filter((where) => where !== null)
    .sort((one, other) => one.at - other.at);
  for (const where of along) {
    const ends = where.at + where.bases;
    let row = rows.findIndex((taken) => taken <= where.at);
    if (row < 0) row = rows.push(0) - 1;
    rows[row] = ends;
    out.push({ ...where, row });
  }
  return { placed: out, rows: Math.max(rows.length, 1) };
}

/** The strip: the target as a line, each guide a mark on its own strand's side of it --
 * forward above, reverse below, stacked within each side so none hides another. It is
 * hidden from screen readers, which the paragraph above it and the tables beneath serve.
 */
function drawing(guides, first, span) {
  const each = (WIDTH - 2 * LEFT) / Math.max(span, 1);
  const middle = TOP + LANE;
  const picture = svg("svg", {
    viewBox: `0 0 ${WIDTH} ${HEIGHT}`,
    "aria-hidden": "true",
    class: "strip",
  });
  picture.append(
    svg("line", { x1: LEFT, y1: middle, x2: WIDTH - LEFT, y2: middle, class: "axis" }),
  );
  for (const side of ["forward", "reverse"]) {
    const mine = guides.filter((guide) =>
      side === "forward" ? String(guide.strand) !== "reverse" : String(guide.strand) === "reverse",
    );
    const { placed, rows } = stacked(mine, first);
    // The rows share the strand's own band, with a hairline of Canvas between them.
    const high = Math.max((LANE - 1) / rows - 1, 2);
    for (const where of placed) {
      const wide = Math.max(where.bases * each, LEAST);
      const top =
        side === "forward"
          ? middle - 1 - (where.row + 1) * ((LANE - 1) / rows)
          : middle + 1 + where.row * ((LANE - 1) / rows);
      picture.append(
        svg("rect", {
          x: Math.min(Math.max(LEFT + where.at * each, LEFT), WIDTH - LEFT - wide).toFixed(2),
          y: top.toFixed(2),
          width: wide.toFixed(2),
          height: high.toFixed(2),
          class: "guide",
        }),
      );
    }
  }
  const step = tickEvery(span);
  const from = Math.ceil(first / step) * step;
  for (let at = from; at <= first + span; at += step) {
    const x = LEFT + (at - first) * each;
    picture.append(
      svg("line", {
        x1: x.toFixed(2),
        y1: middle + LANE + 1,
        x2: x.toFixed(2),
        y2: middle + LANE + 5,
        class: "axis",
      }),
    );
    const label = svg("text", {
      x: x.toFixed(2),
      y: middle + LANE + 17,
      class: "tick-label",
      "text-anchor": "middle",
    });
    label.textContent = at.toLocaleString("en");
    picture.append(label);
  }
  picture.append(
    svg("line", {
      x1: LEFT,
      y1: middle + LANE,
      x2: WIDTH - LEFT,
      y2: middle + LANE,
      class: "axis",
    }),
  );
  return picture;
}

/** What the strip draws, in words, for anyone who isn't looking at it. */
function describe(values, guides, first, last) {
  const forward = guides.filter((guide) => String(guide.strand) !== "reverse").length;
  const found = Number(values.guides_found);
  const shown =
    Number.isFinite(found) && found > guides.length
      ? `${guides.length.toLocaleString("en")} of ${found.toLocaleString("en")} guides`
      : `${guides.length.toLocaleString("en")} guide${guides.length === 1 ? "" : "s"}`;
  const flagged = guides.filter((guide) => (guide.flags || []).length).length;
  const elsewhere = guides.filter((guide) => Number(guide.off_targets) > 0).length;
  return (
    `${shown}, between bases ${first.toLocaleString("en")} and ${last.toLocaleString("en")} ` +
    `of the target: ${forward.toLocaleString("en")} on the forward strand, drawn above ` +
    `the line, and ${(guides.length - forward).toLocaleString("en")} on the reverse ` +
    `strand, drawn below it. ${flagged.toLocaleString("en")} of them have something ` +
    `known to make a guide work less well, and ${elsewhere.toLocaleString("en")} could ` +
    `cut somewhere else in what was searched. Each mark is one guide, as wide as its ` +
    `protospacer.`
  );
}

/** What the design found, as the record keeps it. */
function summary(values, guides) {
  const list = element("dl", {});
  const rows = [
    ["Guides found", counted(maybe(values.guides_found))],
    ["Shown", counted(guides.length)],
  ];
  for (const [what, value] of rows) {
    list.append(element("dt", {}, what), element("dd", {}, value));
  }
  if (typeof values.searched === "string" && values.searched !== "") {
    list.append(
      element("dt", {}, "Searched in"),
      element("dd", { className: "note" }, values.searched),
    );
  }
  if (typeof values.designed_with === "string" && values.designed_with !== "") {
    list.append(
      element("dt", {}, "Found with"),
      element("dd", { className: "note" }, values.designed_with),
    );
  }
  return list;
}

/** Every guide, with what is known about it. A specificity is a number about a search,
 * so where there was none it is an em dash, not a nought and not a hundred. */
function guideTable(guides, pamFirst) {
  const table = element("table", {});
  const caption = element("caption");
  caption.textContent = `Guides (${guides.length.toLocaleString("en")})`;
  const header = element("tr", {});
  header.append(
    head("Protospacer and PAM", "bases"),
    head("Strand"),
    head("Cuts after", "num"),
    head("GC", "num"),
    head("Specificity", "num"),
    head("Elsewhere", "num"),
  );
  const body = element("tbody", {});
  for (const guide of guides) {
    const row = element("tr", {});
    const cuts = Array.isArray(guide.cuts) ? guide.cuts : [];
    const specificity = maybe(guide.specificity);
    const gc = maybe(guide.gc);
    row.append(
      withPam(guide, pamFirst, "th"),
      cell(String(guide.strand ?? "")),
      cell(cuts.map((at) => Number(at).toLocaleString("en")).join(" and ") || "—", "num"),
      cell(gc === null ? "—" : `${gc.toFixed(0)}%`, "num"),
      cell(specificity === null ? "—" : `${specificity.toFixed(0)}%`, "num"),
      cell(counted(maybe(guide.off_targets)), "num"),
    );
    body.append(row);
  }
  table.append(caption, element("thead", {}, header), body);
  return table;
}

/** What is known to make a guide work less well, each under the work that says so: not a
 * score, since the published efficiency models' weights were never published (ADR 0205). */
function flagTable(guides) {
  const rows = [];
  for (const guide of guides) {
    for (const flag of guide.flags || []) rows.push([guide.protospacer, flag]);
  }
  const table = element("table", {});
  const caption = element("caption");
  caption.textContent = `What may make a guide work less well (${rows.length.toLocaleString("en")})`;
  const header = element("tr", {});
  header.append(head("Guide", "bases"), head("What"));
  const body = element("tbody", {});
  for (const [guide, flag] of rows) {
    const row = element("tr", {});
    row.append(rowHead(String(guide), "bases"), cell(String(flag)));
    body.append(row);
  }
  table.append(caption, element("thead", {}, header), body);
  return table;
}

/** Everywhere else the guides could cut, with where the mismatches lie: the positions are
 * counted from the end furthest from the PAM, which is how the weights behind the score
 * are numbered (ADR 0205). */
function hitTable(guides, pamFirst) {
  const rows = [];
  for (const guide of guides) {
    for (const hit of guide.hits || []) rows.push([guide.protospacer, hit]);
  }
  const table = element("table", {});
  const caption = element("caption");
  caption.textContent = `Where else they could cut (${rows.length.toLocaleString("en")})`;
  const header = element("tr", {});
  header.append(
    head("Guide", "bases"),
    head("In"),
    head("At", "num"),
    head("Strand"),
    head("What is there", "bases"),
    head("Mismatched at", "num"),
    head("Score", "num"),
  );
  const body = element("tbody", {});
  for (const [guide, hit] of rows.slice(0, ROWS)) {
    const row = element("tr", {});
    const places = Array.isArray(hit.mismatched) ? hit.mismatched : [];
    const score = maybe(hit.score);
    row.append(
      rowHead(String(guide), "bases"),
      cell(String(hit.where ?? "")),
      cell(counted(maybe(hit.start)), "num"),
      cell(String(hit.strand ?? "")),
      withPam(hit, pamFirst),
      cell(places.length ? places.join(", ") : "nowhere: it matches exactly"),
      cell(score === null ? "—" : score.toFixed(1), "num"),
    );
    body.append(row);
  }
  table.append(caption, element("thead", {}, header), body);
  return { table, total: rows.length };
}

class GuideView extends HTMLElement {
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
    const guides = Array.isArray(values.guides) ? values.guides : [];
    // Nothing is drawn until the action has been run: the design is a record of what was
    // asked for, and the guides, their flags and their off-targets are all a run's.
    if (!guides.length) {
      const children = [
        style,
        element(
          "p",
          { role: "status" },
          "Its guides aren't found yet: both strands are read for the nuclease's own " +
            "motif, and every guide searched for where else it could cut, by Find the " +
            "guides and where else they could cut.",
        ),
      ];
      if (this.at?.runs?.includes(DESIGN)) {
        const button = element("button", { type: "button", className: "run" });
        button.textContent = "Find the guides";
        button.addEventListener("click", () => {
          this.dispatchEvent(new CustomEvent("iki-run", { detail: { action: DESIGN } }));
        });
        children.push(element("p", {}, button));
      }
      this.shadowRoot.replaceChildren(...children);
      return;
    }
    // Cas12a reads its PAM before the protospacer; every Cas9 here reads it after. The
    // record says which nuclease, and the tables write the two parts in that order.
    const pamFirst = String(values.nuclease ?? "").endsWith("cas12a");
    const starts = guides.map((guide) => Number(guide.start)).filter(Number.isFinite);
    const ends = guides.map((guide) => Number(guide.end)).filter(Number.isFinite);
    const first = Math.min(...starts);
    const last = Math.max(...ends, ...starts);
    const span = Math.max(last - first + 1, 1);
    const children = [
      style,
      summary(values, guides),
      element("p", {}, describe(values, guides, first, last)),
    ];
    const key = element("p", { className: "key" });
    key.append(
      element("span", {}, element("span", { className: "swatch" }), "A guide"),
      element(
        "span",
        { className: "note" },
        "Above the line: on the forward strand. Below it: on the reverse strand.",
      ),
    );
    children.push(key, drawing(guides, first, span));
    // Said before the table rather than after it, since it is what the table's empty
    // specificity column means: nobody looked, which is not a guide that cuts everywhere.
    const searchedNothing =
      typeof values.searched === "string" && values.searched.startsWith("nothing");
    if (searchedNothing) {
      const none = element("p", { role: "status" });
      none.textContent =
        "Nothing was searched, so no off-target was looked for: these guides have no " +
        "specificity, which is not the same as being particular.";
      children.push(none);
    }
    children.push(scrolling(guideTable(guides, pamFirst), "Guides"));
    if (guides.some((guide) => (guide.flags || []).length)) {
      children.push(scrolling(flagTable(guides), "What may make a guide work less well"));
    } else {
      const none = element("p", { role: "status" });
      none.textContent =
        "Nothing known to make a guide work less well applies to any of these guides.";
      children.push(none);
    }
    const hits = hitTable(guides, pamFirst);
    if (hits.total) {
      children.push(scrolling(hits.table, "Where else they could cut"));
      if (hits.total > ROWS) {
        const more = element("p", { className: "more" });
        more.textContent = `The first ${ROWS.toLocaleString("en")} of ${hits.total.toLocaleString("en")} are shown; the record's Guides field has every one.`;
        children.push(more);
      }
    } else if (!searchedNothing) {
      const none = element("p", { role: "status" });
      none.textContent = "None of these guides matches anywhere else in what was searched.";
      children.push(none);
    }
    this.shadowRoot.host.style.setProperty("--guide", dark() ? GUIDE[1] : GUIDE[0]);
    this.shadowRoot.replaceChildren(...children);
  }
}

customElements.define("guide-view", GuideView);
