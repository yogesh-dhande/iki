// The oligo plugin's Chemistry tab: an oligonucleotide's nucleotides 5′ to 3′, each its
// base over its sugar and linker, the modified ones marked, and the groups at its ends.
// It draws the `positions` the plugin's actions compute from its HELM and monomers; Iki
// sets `entity` to the oligonucleotide, as the person viewing it may read it (ADR 0084).
// Colour only repeats what the text says, so nothing depends on seeing it.

const STYLE = `
  :host { display: block; color: CanvasText; background: Canvas; padding: 1rem; border-radius: 8px; font: 14px/1.45 system-ui, sans-serif; }
  h2 { font-size: 1rem; margin: 0 0 0.5rem; }
  p { margin: 0 0 0.75rem; max-width: 70ch; }
  .strand { display: flex; flex-wrap: wrap; align-items: stretch; gap: 0.75rem 0;
    margin: 0 0 1rem; padding: 0; list-style: none; }
  .end { display: grid; place-items: center; padding: 0.25rem 0.5rem; margin: 0 0.25rem;
    border: 2px dashed currentColor; border-radius: 6px; font-size: 0.8rem; max-width: 12rem;
    text-align: center; }
  .nt { display: grid; grid-template-rows: auto auto auto; justify-items: center;
    min-width: 2.75rem; padding: 0.25rem 0.125rem 0.375rem; border-radius: 6px;
    border: 1px solid color-mix(in srgb, CanvasText 20%, transparent); margin-right: 2px; }
  .nt.modified { border: 2px solid var(--mod, #8a4b00); }
  .pos { font-size: 0.7rem; color: color-mix(in srgb, CanvasText 75%, Canvas); }
  .base { font: 700 1.25rem ui-monospace, monospace; }
  .sugar, .link { font: 0.72rem ui-monospace, monospace; }
  .sugar.modified, .link.modified, .base.modified { color: var(--mod, #8a4b00); }
  @media (prefers-color-scheme: dark) { :host { --mod: #ffb366; } }
  .legend { display: flex; flex-wrap: wrap; gap: 0.5rem 1.25rem; margin: 0 0 1rem; padding: 0;
    list-style: none; font-size: 0.85rem; }
  table { border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; padding: 0.25rem 0.75rem 0.25rem 0;
    border-bottom: 1px solid color-mix(in srgb, CanvasText 15%, transparent); }
  td.modified::after { content: " (modified)"; font-size: 0.75rem; }
  details { margin-top: 0.5rem; }
  summary { cursor: pointer; }
  summary:focus-visible { outline: 2px solid Highlight; outline-offset: 2px; }
`;

/** The text for one monomer: its symbol, as HELM writes it. */
function symbol(part) {
  if (!part) return "";
  return part.symbol.length > 1 ? `[${part.symbol}]` : part.symbol;
}

function element(tag, attributes = {}, ...children) {
  const out = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "className") out.className = value;
    else out.setAttribute(name, value);
  }
  out.append(...children.filter((c) => c !== null && c !== undefined));
  return out;
}

/** What a screen reader says for one nucleotide. */
function spoken(nt) {
  const parts = [`Position ${nt.position}`];
  const base = nt.nucleobase
    ? `${nt.nucleobase.name}${nt.nucleobase.modified ? ", modified" : ""}`
    : "no base, an abasic site";
  const sugar = nt.sugar ? `${nt.sugar.name}${nt.sugar.modified ? ", modified" : ""}` : "no sugar";
  parts.push(`${base} on ${sugar}`);
  if (nt.linker) parts.push(`then ${nt.linker.name}${nt.linker.modified ? ", modified" : ""}`);
  for (const other of nt.others ?? []) parts.push(other.name);
  return parts.join("; ");
}

function strand(positions) {
  const list = element("ol", { className: "strand", "aria-label": "Nucleotides, 5′ to 3′" });
  const ends = positions.ends ?? {};
  for (const group of ends["5′"] ?? []) {
    list.append(
      element(
        "li",
        { className: "end", "aria-label": `5′ end: ${group.name}` },
        `5′ ${symbol(group)}`,
      ),
    );
  }
  for (const nt of positions.nucleotides ?? []) {
    const modified = [nt.sugar, nt.nucleobase, nt.linker].some((p) => p?.modified);
    const item = element("li", {
      className: modified ? "nt modified" : "nt",
      "aria-label": spoken(nt),
    });
    const cls = (p, name) => (p?.modified ? `${name} modified` : name);
    item.append(
      element("span", { className: "pos", "aria-hidden": "true" }, String(nt.position)),
      element(
        "span",
        { className: cls(nt.nucleobase, "base"), "aria-hidden": "true" },
        nt.base ?? "–",
      ),
      element(
        "span",
        { className: cls(nt.sugar, "sugar"), "aria-hidden": "true" },
        symbol(nt.sugar) || "–",
      ),
      element(
        "span",
        { className: cls(nt.linker, "link"), "aria-hidden": "true" },
        symbol(nt.linker) || " ",
      ),
    );
    list.append(item);
  }
  for (const group of ends["3′"] ?? []) {
    list.append(
      element(
        "li",
        { className: "end", "aria-label": `3′ end: ${group.name}` },
        `3′ ${symbol(group)}`,
      ),
    );
  }
  return list;
}

function legend(positions) {
  const seen = new Map();
  for (const nt of positions.nucleotides ?? []) {
    for (const part of [nt.sugar, nt.nucleobase, nt.linker, ...(nt.others ?? [])]) {
      if (part) seen.set(part.symbol, part);
    }
  }
  const list = element("ul", { className: "legend", "aria-label": "Monomers" });
  for (const part of [...seen.values()].sort((a, b) => a.symbol.localeCompare(b.symbol))) {
    list.append(
      element(
        "li",
        {},
        element("code", { className: part.modified ? "sugar modified" : "sugar" }, symbol(part)),
        ` ${part.name}${part.modified ? " (modified)" : ""}`,
      ),
    );
  }
  return list;
}

function table(positions) {
  const head = element(
    "tr",
    {},
    ...["Position", "Base", "Nucleobase", "Sugar", "Linker"].map((t) =>
      element("th", { scope: "col" }, t),
    ),
  );
  const body = element("tbody");
  for (const nt of positions.nucleotides ?? []) {
    const cell = (part) =>
      element("td", part?.modified ? { className: "modified" } : {}, part ? part.name : "—");
    body.append(
      element(
        "tr",
        {},
        element("th", { scope: "row" }, String(nt.position)),
        element("td", {}, nt.base ?? "—"),
        cell(nt.nucleobase),
        cell(nt.sugar),
        cell(nt.linker),
      ),
    );
  }
  return element(
    "details",
    {},
    element("summary", {}, "Each position"),
    element("table", {}, element("thead", {}, head), body),
  );
}

class Chemistry extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set entity(entity) {
    const style = element("style");
    style.textContent = STYLE;
    const positions = entity?.values?.positions;
    if (!positions?.nucleotides?.length) {
      const note = element(
        "p",
        { role: "status" },
        `${entity?.hrid ?? "It"} has no positions computed yet: its properties are computed from its monomers when it's registered.`,
      );
      this.shadowRoot.replaceChildren(style, note);
      return;
    }
    const count = positions.nucleotides.length;
    const modified = positions.nucleotides.filter((nt) =>
      [nt.sugar, nt.nucleobase, nt.linker].some((p) => p?.modified),
    ).length;
    const summary = element(
      "p",
      {},
      `${count} nucleotide${count === 1 ? "" : "s"}, 5′ to 3′; ${modified} modified, outlined, with what's modified in colour. Each shows its base's natural analogue, its sugar and the linker after it.`,
    );
    this.shadowRoot.replaceChildren(
      style,
      element("h2", {}, "Nucleotides"),
      summary,
      strand(positions),
      legend(positions),
      table(positions),
    );
  }
}

customElements.define("oligo-chemistry", Chemistry);
