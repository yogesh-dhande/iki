import { disclosure, element } from "./molecular-dom.js";
import {
  absorbanceAtOneGramPerLitre,
  formulaAtoms,
  withDisulfides,
  type CompositionMass,
} from "./composition-mass.js";
export type { DetailsChange } from "./molecular-dom.js";

export interface ComplexPart {
  name: string;
  role?: string | null;
  copies?: number;
}
export interface BondEndpoint {
  part: string;
  copy?: number;
  residue: number;
}
export interface ComplexBond {
  kind: string;
  from: BondEndpoint;
  to: BondEndpoint;
}
export interface ComplexCompositionData {
  parts: readonly ComplexPart[];
  bonds?: readonly ComplexBond[];
  chainCount?: number | null;
  reduced?: CompositionMass | null;
  partDisulfides?: number | null;
  bondMap?: "incomplete" | "predominant" | "complete" | null;
  partBondsComplete?: boolean | null;
  emptyMessage?: string;
  canCompute?: boolean;
  uncountedPartsMessage?: string;
}

const STYLE = `
  :host { display: block; color: var(--text, CanvasText); background: var(--surface, Canvas); padding: 1rem; border-radius: 8px; font: 14px/1.45 var(--font, system-ui, sans-serif); min-width: 0; overflow-x: auto; }
  h2 { font-size: 1rem; margin: 1rem 0 0.5rem; }
  h2:first-child { margin-top: 0; }
  p { margin: 0 0 0.75rem; max-width: 70ch; }
  table { border-collapse: collapse; font-size: 0.875rem; margin: 0 0 0.5rem; }
  caption { text-align: left; font-weight: 600; padding: 0 0 0.25rem; }
  th, td { text-align: left; padding: 0.25rem 1rem 0.25rem 0; vertical-align: top;
    border-bottom: 1px solid color-mix(in srgb, CanvasText 15%, transparent); }
  td.number, th.number { text-align: right; font-variant-numeric: tabular-nums; }
  .formula { font-family: ui-monospace, monospace; overflow-wrap: anywhere; }
  .muted { color: color-mix(in srgb, CanvasText 72%, Canvas); }
  .warning { border-left: 3px solid color-mix(in srgb, CanvasText 60%, Canvas); padding-left: 0.5rem; }
  details { margin-top: 0.5rem; }
  summary { cursor: pointer; }
  summary:focus-visible { outline: 2px solid Highlight; outline-offset: 2px; }
  button { font: inherit; color: Canvas; background: CanvasText; cursor: pointer;
    padding: 0.375rem 0.875rem; border: 1px solid CanvasText; border-radius: 6px; }
  button:focus-visible { outline: 2px solid CanvasText; outline-offset: 2px; }
`;

/** A formula with its counts as subscripts, for the eye; the text stays the formula. */
function formulaNode(formula: string) {
  const node = element("span", { class: "formula" });
  for (const { symbol, count: n } of formulaAtoms(formula)) {
    node.append(symbol);
    if (n !== 1) node.append(element("sub", {}, String(n)));
  }
  return node;
}

function grouped(value: number, digits: number) {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** A mass in unified atomic mass units, and in kilo-units when it's large. */
function massText(value: number, digits: number) {
  const u = `${grouped(value, digits)} u`;
  return value >= 10_000 ? `${u} (${grouped(value / 1000, 2)} ku)` : u;
}

function partsTable(components: readonly ComplexPart[]) {
  const body = element("tbody");
  for (const row of components) {
    body.append(
      element(
        "tr",
        {},
        element("th", { scope: "row" }, row.name),
        element("td", {}, row.role ?? "—"),
        element("td", { class: "number" }, String(row.copies ?? 1)),
      ),
    );
  }
  return element(
    "table",
    {},
    element("caption", {}, "Parts"),
    element(
      "thead",
      {},
      element(
        "tr",
        {},
        element("th", { scope: "col" }, "Part"),
        element("th", { scope: "col" }, "Role"),
        element("th", { scope: "col", class: "number" }, "Copies"),
      ),
    ),
    body,
  );
}

function massesTable(free: CompositionMass, disulfides: number, theirs: number) {
  const head = element(
    "tr",
    {},
    element("td"),
    element("th", { scope: "col" }, "Its parts as they are"),
  );
  const columns = [free];
  if (disulfides > 0) {
    head.append(
      element(
        "th",
        { scope: "col" },
        `With its ${String(disulfides)} disulfide${disulfides === 1 ? "" : "s"}`,
      ),
    );
    columns.push(withDisulfides(free, disulfides));
  }
  if (theirs > 0) {
    head.append(element("th", { scope: "col" }, `With its parts' ${String(theirs)} as well`));
    columns.push(withDisulfides(free, disulfides + theirs));
  }
  function row(
    label: string,
    cell: (data: CompositionMass) => string | Node | null,
    numeric = true,
  ) {
    return element(
      "tr",
      {},
      element("th", { scope: "row" }, label),
      ...columns.map((column) =>
        element("td", { class: numeric ? "number" : "" }, cell(column) ?? "—"),
      ),
    );
  }
  return element(
    "table",
    {},
    element("caption", {}, "Formula, masses and extinction coefficient"),
    element("thead", {}, head),
    element(
      "tbody",
      {},
      row("Formula", (m) => formulaNode(m.formula), false),
      row("Average mass", (m) => (m.average === null ? null : massText(m.average, 2))),
      row("Monoisotopic mass", (m) =>
        m.monoisotopic === null ? null : massText(m.monoisotopic, 4),
      ),
      row("Molar extinction coefficient, ε280", (m) =>
        m.extinctionCoefficient === null
          ? null
          : `${grouped(m.extinctionCoefficient, 0)} L/(mol·cm)`,
      ),
      row("Absorbance at 280 nm of 1 g/L, 1 cm", (m) => {
        const absorbance = absorbanceAtOneGramPerLitre(m);
        return absorbance === null ? null : grouped(absorbance, 3);
      }),
    ),
  );
}

function bondEnd(end: BondEndpoint) {
  return `${end.part}, copy ${String(end.copy ?? 1)}, residue ${String(end.residue)}`;
}

function bondsTable(bonds: readonly ComplexBond[]) {
  const body = element("tbody");
  bonds.forEach((bond, index) => {
    body.append(
      element(
        "tr",
        {},
        element("th", { scope: "row" }, String(index + 1)),
        element("td", {}, bond.kind),
        element("td", {}, bondEnd(bond.from)),
        element("td", {}, bondEnd(bond.to)),
      ),
    );
  });
  return disclosure(
    "bonds",
    `Its ${String(bonds.length)} bond${bonds.length === 1 ? "" : "s"}`,
    element(
      "table",
      {},
      element(
        "thead",
        {},
        element(
          "tr",
          {},
          element("th", { scope: "col" }, "Bond"),
          element("th", { scope: "col" }, "Kind"),
          element("th", { scope: "col" }, "From"),
          element("th", { scope: "col" }, "To"),
        ),
      ),
      body,
    ),
  );
}

// Reasons that a reported bond count may be incomplete.
const NOT_COMPLETE = {
  incomplete: "Its bonds don't include every bond it has",
  predominant:
    "Its bonds are only its predominant form's, while some of its molecules are bonded otherwise",
};

/** Why what's worked out with its disulfides may count too few or too many, or null. */
function uncounted(values: ComplexCompositionData) {
  const why = [];
  if (values.bondMap === "incomplete" || values.bondMap === "predominant")
    why.push(NOT_COMPLETE[values.bondMap]);
  if (values.partDisulfides === null || values.partDisulfides === undefined) {
    why.push(values.uncountedPartsMessage ?? "its parts' own disulfides have not been counted");
  } else if (values.partBondsComplete === false) {
    why.push("not all of its parts' own disulfides are counted");
  }
  if (!why.length) return null;
  const text = why.join("; ");
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}, so what's worked out with its disulfides may count too few or too many.`;
}

export class ComplexComposition extends HTMLElement {
  readonly #root = this.attachShadow({ mode: "open" });
  #data: ComplexCompositionData = { parts: [] };

  connectedCallback() {
    this.render();
  }
  get data() {
    return this.#data;
  }
  set data(value: ComplexCompositionData) {
    this.#data = value;
    this.render();
  }

  private empty(values: ComplexCompositionData) {
    const out = [
      element("p", { role: "status" }, values.emptyMessage ?? "No composition supplied."),
    ];
    if (values.canCompute) {
      const button = element("button", { type: "button" }, "Compute composition");
      button.addEventListener("click", () => {
        this.dispatchEvent(new CustomEvent("compute-request", { bubbles: true, composed: true }));
      });
      out.push(element("p", {}, button));
    }
    return out;
  }

  private render() {
    const values = this.#data;
    const style = element("style", {}, STYLE);
    const components = values.parts;
    const bonds = values.bonds ?? [];
    const disulfides = bonds.filter((b) => b.kind === "disulfide").length;
    const chains = values.chainCount;
    const copies = components.reduce((n, row) => n + (row.copies ?? 1), 0);
    const summary = element(
      "p",
      {},
      `${String(components.length)} part${components.length === 1 ? "" : "s"}, ${String(copies)} cop${copies === 1 ? "y" : "ies"} in all` +
        (chains !== null && chains !== undefined
          ? `, ${String(chains)} chain${chains === 1 ? "" : "s"}.`
          : "."),
    );
    const out: HTMLElement[] = [
      style,
      element("h2", {}, "Composition"),
      summary,
      partsTable(components),
    ];
    if (!values.reduced) {
      out.push(...this.empty(values));
    } else {
      const theirs = values.partDisulfides ?? 0;
      try {
        out.push(massesTable(values.reduced, disulfides, theirs));
      } catch (error) {
        if (!(error instanceof RangeError)) throw error;
        out.push(element("p", { role: "status" }, error.message));
      }
      const missing = uncounted(values);
      if (missing) out.push(element("p", { role: "note", class: "warning" }, missing));
      const others = bonds.length - disulfides;
      out.push(
        element(
          "p",
          { class: "muted" },
          "Its parts as they are have each cysteine's thiol free; each disulfide takes two hydrogens off and, as a cystine, adds 125 L/(mol·cm) to ε280 (Pace's values, which weigh only proteins: a complex with another kind of part, such as a heme, has none)." +
            (others > 0
              ? ` Its ${String(others)} other bond${others === 1 ? "" : "s"} aren't weighed: what each takes off depends on its chemistry.`
              : ""),
        ),
      );
    }
    if (bonds.length) out.push(bondsTable(bonds));
    this.#root.replaceChildren(...out);
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "iki-complex-composition": ComplexComposition;
  }
}
if (!customElements.get("iki-complex-composition"))
  customElements.define("iki-complex-composition", ComplexComposition);
