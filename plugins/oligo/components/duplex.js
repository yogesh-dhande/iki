// The oligo plugin's Pairing tab: a duplex's strands antiparallel, the sense strand 5′ to
// 3′ over the antisense strand 3′ to 5′, a bar for each Watson–Crick pair and a dot for
// each mismatch in the duplex region. It draws the `pairing` the plugin's actions find
// from the strands' bases; Iki sets `entity` to the duplex (ADR 0084). The drawing is
// described in words for screen readers, and its letters don't depend on colour.

const STYLE = `
  :host { display: block; color: CanvasText; background: Canvas; padding: 1rem; border-radius: 8px; font: 14px/1.45 system-ui, sans-serif; }
  h2 { font-size: 1rem; margin: 0 0 0.5rem; }
  p { margin: 0 0 0.75rem; max-width: 70ch; }
  .pairing { overflow-x: auto; margin: 0 0 1rem; padding: 0.5rem 0; }
  .row { display: flex; align-items: center; white-space: pre;
    font: 1.05rem/1.6 ui-monospace, monospace; }
  .row > span { display: inline-block; flex: none; width: 1.4ch; text-align: center; }
  .row > .label { width: 7rem; flex: none; text-align: right; padding-right: 0.5rem;
    font: 0.8rem system-ui, sans-serif; color: color-mix(in srgb, CanvasText 75%, Canvas); }
  .row > .label.end { width: auto; text-align: left; padding: 0 0 0 0.5rem; }
  .over { color: color-mix(in srgb, CanvasText 62%, Canvas); }
  .bad { color: var(--bad, #a10000); font-weight: 700; }
  @media (prefers-color-scheme: dark) { :host { --bad: #ff8080; } }
  dl { display: grid; grid-template-columns: max-content auto; gap: 0.25rem 1rem; margin: 0; }
  dt { font-weight: 600; }
  dd { margin: 0; }
`;

function element(tag, attributes = {}, ...children) {
  const out = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "className") out.className = value;
    else out.setAttribute(name, value);
  }
  out.append(...children);
  return out;
}

function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

/** The drawing: three rows of columns, sense over the pairs over the antisense reversed. */
function drawing(pairing) {
  const sense = pairing.sense.bases;
  const antisense = pairing.antisense.bases;
  // Sense position i (from 0) faces antisense position offset - i.
  const first = pairing.pairs[0];
  const offset = first.sense - 1 + (first.antisense - 1);
  const start = Math.min(0, offset - (antisense.length - 1));
  const end = Math.max(sense.length - 1, offset);
  const inDuplex = new Map(pairing.pairs.map((p) => [p.sense - 1, p.paired]));
  const top = element(
    "div",
    { className: "row" },
    element("span", { className: "label" }, "Sense 5′"),
  );
  const middle = element("div", { className: "row" }, element("span", { className: "label" }, ""));
  const bottom = element(
    "div",
    { className: "row" },
    element("span", { className: "label" }, "Antisense 3′"),
  );
  for (let column = start; column <= end; column++) {
    const s = column >= 0 && column < sense.length ? sense[column] : " ";
    const j = offset - column;
    const a = j >= 0 && j < antisense.length ? antisense[j] : " ";
    const paired = inDuplex.get(column);
    top.append(element("span", { className: paired === undefined ? "over" : "" }, s));
    middle.append(
      element(
        "span",
        { className: paired === false ? "bad" : "" },
        paired === undefined ? " " : paired ? "|" : "·",
      ),
    );
    bottom.append(element("span", { className: paired === undefined ? "over" : "" }, a));
  }
  top.append(element("span", { className: "label end" }, "3′"));
  bottom.append(element("span", { className: "label end" }, "5′"));
  return element("div", { className: "pairing", "aria-hidden": "true" }, top, middle, bottom);
}

class Duplex extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set entity(entity) {
    const style = element("style");
    style.textContent = STYLE;
    const v = entity?.values ?? {};
    const pairing = v.pairing;
    if (!pairing?.pairs?.length) {
      const why = pairing
        ? "Its strands don't pair: no stretch of their bases, antiparallel, is complementary."
        : "Its pairing isn't found yet: it's found from its strands when it's registered.";
      this.shadowRoot.replaceChildren(style, element("p", { role: "status" }, why));
      return;
    }
    const mismatches = v.mismatches ?? pairing.mismatches ?? 0;
    const described =
      `${plural(v.duplex_length ?? pairing.length, "base pair")}, ` +
      `${mismatches ? `${plural(mismatches, "mismatch", "mismatches")} or G·U wobble${mismatches === 1 ? "" : "s"}` : "no mismatches"}. ` +
      `Sense strand: ${pairing.sense_overhang_5}-nucleotide 5′ and ${pairing.sense_overhang_3}-nucleotide 3′ overhang. ` +
      `Antisense strand: ${pairing.antisense_overhang_5}-nucleotide 5′ and ${pairing.antisense_overhang_3}-nucleotide 3′ overhang.`;
    const facts = element(
      "dl",
      {},
      element("dt", {}, "Sense strand, 5′ to 3′"),
      element("dd", {}, element("code", {}, pairing.sense.bases)),
      element("dt", {}, "Antisense strand, 5′ to 3′"),
      element("dd", {}, element("code", {}, pairing.antisense.bases)),
      element("dt", {}, "Duplex region"),
      element(
        "dd",
        {},
        `sense ${pairing.sense.from}–${pairing.sense.to}, antisense ${pairing.antisense.from}–${pairing.antisense.to}`,
      ),
    );
    this.shadowRoot.replaceChildren(
      style,
      element("h2", {}, "Pairing"),
      element("p", {}, described),
      element(
        "p",
        {},
        "Drawn below: the sense strand 5′ to 3′ over the antisense strand 3′ to 5′; | is a Watson–Crick pair, · a mismatch, and faded bases overhang.",
      ),
      drawing(pairing),
      facts,
    );
  }
}

customElements.define("oligo-duplex", Duplex);
