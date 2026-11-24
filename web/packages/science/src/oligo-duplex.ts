import { disclosure, element } from "./molecular-dom.js";
export type { DetailsChange } from "./molecular-dom.js";

export interface DuplexPair {
  sense: number;
  antisense: number;
  paired: boolean;
}
export interface OligoDuplexData {
  /** Both strands in their own 5′ to 3′ direction. */
  sense: string;
  antisense: string;
  pairs: readonly DuplexPair[];
}

const STYLE = `
  :host { display: block; color: var(--text, CanvasText); background: var(--surface, Canvas); padding: 1rem; border-radius: 8px; font: 14px/1.45 var(--font, system-ui, sans-serif); min-width: 0; overflow-x: auto; }
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
  .bad { color: var(--error, var(--bad, #a10000)); font-weight: 700; }
  @media (prefers-color-scheme: dark) { :host { --bad: #ff8080; } }
  dl { display: grid; grid-template-columns: max-content auto; gap: 0.25rem 1rem; margin: 0; }
  dt { font-weight: 600; }
  dd { margin: 0; overflow-wrap: anywhere; }
  details summary { cursor: pointer; }
  summary:focus-visible { outline: 2px solid var(--focus, Highlight); outline-offset: 2px; }
`;

function plural(n: number, one: string, many = `${one}s`) {
  return `${String(n)} ${n === 1 ? one : many}`;
}

/** The drawing: three rows of columns, sense over the pairs over the antisense reversed. */
function drawing(pairing: OligoDuplexData, first: DuplexPair) {
  const sense = pairing.sense;
  const antisense = pairing.antisense;
  // Sense position i (from 0) faces antisense position offset - i.
  const offset = first.sense - 1 + (first.antisense - 1);
  const start = Math.min(0, offset - (antisense.length - 1));
  const end = Math.max(sense.length - 1, offset);
  const inDuplex = new Map(pairing.pairs.map((p) => [p.sense - 1, p.paired]));
  const top = element("div", { class: "row" }, element("span", { class: "label" }, "Sense 5′"));
  const middle = element("div", { class: "row" }, element("span", { class: "label" }, ""));
  const bottom = element(
    "div",
    { class: "row" },
    element("span", { class: "label" }, "Antisense 3′"),
  );
  for (let column = start; column <= end; column++) {
    const s = sense[column] ?? " ";
    const j = offset - column;
    const a = antisense[j] ?? " ";
    const paired = inDuplex.get(column);
    top.append(element("span", { class: paired === undefined ? "over" : "" }, s));
    middle.append(
      element(
        "span",
        { class: paired === false ? "bad" : "" },
        paired === undefined ? " " : paired ? "|" : "·",
      ),
    );
    bottom.append(element("span", { class: paired === undefined ? "over" : "" }, a));
  }
  top.append(element("span", { class: "label end" }, "3′"));
  bottom.append(element("span", { class: "label end" }, "5′"));
  return element("div", { class: "pairing", "aria-hidden": "true" }, top, middle, bottom);
}

export class OligoDuplex extends HTMLElement {
  readonly #root = this.attachShadow({ mode: "open" });
  #data: OligoDuplexData | null = null;

  connectedCallback() {
    this.render();
  }
  get data() {
    return this.#data;
  }
  set data(value: OligoDuplexData | null) {
    this.#data = value;
    this.render();
  }

  private render() {
    const style = element("style", {}, STYLE);
    const pairing = this.#data;
    const first = pairing?.pairs[0];
    if (!pairing || !first) {
      const why = pairing
        ? "Its strands don't pair: no stretch of their bases, antiparallel, is complementary."
        : (this.getAttribute("empty-message") ?? "No pairing supplied.");
      this.#root.replaceChildren(style, element("p", { role: "status" }, why));
      return;
    }
    // A drawing uses a single ungapped antiparallel alignment. Validate its bounds
    // before using caller-provided coordinates to allocate display columns.
    const valid = pairing.pairs.every(
      (pair, index) =>
        Number.isSafeInteger(pair.sense) &&
        Number.isSafeInteger(pair.antisense) &&
        pair.sense >= 1 &&
        pair.sense <= pairing.sense.length &&
        pair.antisense >= 1 &&
        pair.antisense <= pairing.antisense.length &&
        pair.sense === first.sense + index &&
        pair.antisense === first.antisense - index,
    );
    if (!valid) {
      this.#root.replaceChildren(
        style,
        element(
          "p",
          { role: "status" },
          "Pair coordinates must form one antiparallel region within both strands.",
        ),
      );
      return;
    }
    const lastSense = first.sense + pairing.pairs.length - 1;
    const firstAntisense = first.antisense - pairing.pairs.length + 1;
    const mismatches = pairing.pairs.filter((p) => !p.paired).length;
    const described =
      `${plural(pairing.pairs.length, "base pair")}, ` +
      `${mismatches ? `${plural(mismatches, "mismatch", "mismatches")} or G·U wobble${mismatches === 1 ? "" : "s"}` : "no mismatches"}. ` +
      `Sense strand: ${String(first.sense - 1)}-nucleotide 5′ and ${String(pairing.sense.length - lastSense)}-nucleotide 3′ overhang. ` +
      `Antisense strand: ${String(firstAntisense - 1)}-nucleotide 5′ and ${String(pairing.antisense.length - first.antisense)}-nucleotide 3′ overhang.`;
    const facts = element(
      "dl",
      {},
      element("dt", {}, "Sense strand, 5′ to 3′"),
      element("dd", {}, element("code", {}, pairing.sense)),
      element("dt", {}, "Antisense strand, 5′ to 3′"),
      element("dd", {}, element("code", {}, pairing.antisense)),
      element("dt", {}, "Duplex region"),
      element(
        "dd",
        {},
        `sense ${String(first.sense)}–${String(lastSense)}, antisense ${String(firstAntisense)}–${String(first.antisense)}`,
      ),
    );
    this.#root.replaceChildren(
      style,
      element("h2", {}, "Pairing"),
      element("p", {}, described),
      element(
        "p",
        {},
        "Drawn below: the sense strand 5′ to 3′ over the antisense strand 3′ to 5′; | is a Watson–Crick pair, · a mismatch, and faded bases overhang.",
      ),
      drawing(pairing, first),
      disclosure("strands", "Strand details", facts, true),
    );
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "iki-oligo-duplex": OligoDuplex;
  }
}
if (!customElements.get("iki-oligo-duplex")) customElements.define("iki-oligo-duplex", OligoDuplex);
