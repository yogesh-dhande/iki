// The complex plugin's Composition tab: a complex's parts, each with its role and copies;
// its formula, masses and absorbance at 280 nm as compute_composition computed them from
// its parts (each cysteine's thiol free), and with its disulfides, each of which takes two
// hydrogens off and adds a cystine's absorbance: its own, and those its parts have of
// their own (a Fab's, in a complex of the Fab and its antigen), which compute_composition
// counted for this project's record; whether any may be missing; and its bonds. Its bonds
// are a registry fact that may be corrected, so what they change is worked out here, from
// them as they are now, not stored (ADR 0161). Iki sets `entity` to the complex, as the
// person viewing it may read it (ADR 0084).

const STYLE = `
  :host { display: block; color: CanvasText; background: Canvas; padding: 1rem; border-radius: 8px; font: 14px/1.45 system-ui, sans-serif; }
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
`;

// A cystine's molar absorbance at 280 nm in water, L/(mol.cm), as compute_composition
// weighs it (Pace et al., Protein Sci 4:2411, 1995): each disulfide adds one.
const CYSTINE = 125;

// Hydrogen's standard atomic weight, as compute_composition weighs it (Biopython's), and
// its most abundant isotope's mass (AME2020).
const HYDROGEN = { average: 1.00794, monoisotopic: 1.00782503223 };

function element(tag, attributes = {}, ...children) {
  const out = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "className") out.className = value;
    else out.setAttribute(name, value);
  }
  out.append(...children.filter((c) => c !== null && c !== undefined));
  return out;
}

/** A formula's atoms in its own order, with their counts: each an element (C) or an
 * isotope, as RDKit writes one ([13C], or D and T for hydrogen's). */
function elements(formula) {
  return [...formula.matchAll(/(\[\d+[A-Z][a-z]?\]|[A-Z][a-z]?)(\d*)/g)].map(([, symbol, n]) => [
    symbol,
    n ? Number(n) : 1,
  ]);
}

/** The formula with `count` more of `symbol` (fewer, for a negative count). */
function adjusted(formula, symbol, count) {
  return elements(formula)
    .map(([s, n]) => [s, s === symbol ? n + count : n])
    .filter(([, n]) => n > 0)
    .map(([s, n]) => `${s}${n === 1 ? "" : n}`)
    .join("");
}

/** A formula with its counts as subscripts, for the eye; the text stays the formula. */
function formulaNode(formula) {
  const node = element("span", { className: "formula" });
  for (const [symbol, n] of elements(formula)) {
    node.append(symbol);
    if (n !== 1) node.append(element("sub", {}, String(n)));
  }
  return node;
}

function grouped(value, digits) {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** A mass in unified atomic mass units, and in kilo-units when it's large. */
function massText(value, digits) {
  const u = `${grouped(value, digits)} u`;
  return value >= 10_000 ? `${u} (${grouped(value / 1000, 2)} ku)` : u;
}

function partName(entity, id) {
  const ref = entity.refs?.[id];
  if (!ref) return "a part you can't read";
  return ref.title && ref.title !== ref.hrid ? `${ref.hrid} · ${ref.title}` : ref.hrid;
}

function partsTable(entity, components) {
  const body = element("tbody");
  for (const row of components) {
    body.append(
      element(
        "tr",
        {},
        element("th", { scope: "row" }, partName(entity, row.part)),
        element("td", {}, row.role ?? "—"),
        element("td", { className: "number" }, String(row.copies ?? 1)),
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
        element("th", { scope: "col", className: "number" }, "Copies"),
      ),
    ),
    body,
  );
}

/** Its formula, masses and absorbance with `n` disulfides. */
function bonded(free, n) {
  return {
    formula: adjusted(free.formula, "H", -2 * n),
    average: free.average - 2 * HYDROGEN.average * n,
    monoisotopic: free.monoisotopic - 2 * HYDROGEN.monoisotopic * n,
    absorbance:
      free.absorbance === null || free.absorbance === undefined
        ? null
        : free.absorbance + CYSTINE * n,
  };
}

function massesTable(values, disulfides, theirs) {
  const head = element(
    "tr",
    {},
    element("td", {}),
    element("th", { scope: "col" }, "Its parts as they are"),
  );
  const free = {
    formula: values.molecular_formula,
    average: values.molecular_weight,
    monoisotopic: values.monoisotopic_mass,
    absorbance: values.extinction_coefficient_reduced,
  };
  const columns = [free];
  if (disulfides > 0) {
    head.append(
      element(
        "th",
        { scope: "col" },
        `With its ${disulfides} disulfide${disulfides === 1 ? "" : "s"}`,
      ),
    );
    columns.push(bonded(free, disulfides));
  }
  if (theirs > 0) {
    head.append(element("th", { scope: "col" }, `With its parts' ${theirs} as well`));
    columns.push(bonded(free, disulfides + theirs));
  }
  const rows = [
    ["Formula", "formula", formulaNode, ""],
    ["Average mass", "average", (v) => massText(v, 2), "number"],
    ["Monoisotopic mass", "monoisotopic", (v) => massText(v, 4), "number"],
    ["Absorbance at 280 nm", "absorbance", (v) => `${grouped(v, 0)} L/(mol·cm)`, "number"],
  ];
  const body = element("tbody");
  for (const [label, key, shown, className] of rows) {
    const tr = element("tr", {}, element("th", { scope: "row" }, label));
    for (const column of columns) {
      const value = column[key];
      tr.append(
        element("td", { className }, value === null || value === undefined ? "—" : shown(value)),
      );
    }
    body.append(tr);
  }
  return element(
    "table",
    {},
    element("caption", {}, "Formula, masses and absorbance"),
    element("thead", {}, head),
    body,
  );
}

function bondEnd(entity, part, copy, residue) {
  return `${partName(entity, part)}, copy ${copy ?? 1}, residue ${residue}`;
}

function bondsTable(entity, bonds) {
  const body = element("tbody");
  bonds.forEach((bond, index) => {
    body.append(
      element(
        "tr",
        {},
        element("th", { scope: "row" }, String(index + 1)),
        element("td", {}, bond.kind),
        element("td", {}, bondEnd(entity, bond.part, bond.copy, bond.residue)),
        element("td", {}, bondEnd(entity, bond.partner, bond.partner_copy, bond.partner_residue)),
      ),
    );
  });
  return element(
    "details",
    {},
    element("summary", {}, `Its ${bonds.length} bond${bonds.length === 1 ? "" : "s"}`),
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

// What a bond map that isn't complete is, as compute_composition says it.
const NOT_COMPLETE = {
  incomplete: "Its bonds don't include every bond it has",
  predominant:
    "Its bonds are only its predominant form's, while some of its molecules are bonded otherwise",
};

/** Why what's worked out with its disulfides may count too few or too many, or null. */
function uncounted(values) {
  const why = [];
  if (NOT_COMPLETE[values.bond_map]) why.push(NOT_COMPLETE[values.bond_map]);
  if (values.part_disulfides === null || values.part_disulfides === undefined) {
    why.push("its parts' own disulfides aren't counted in this project yet");
  } else if (values.part_bonds_complete === false) {
    why.push("not all of its parts' own disulfides are counted");
  }
  if (!why.length) return null;
  const text = why.join("; ");
  return `${text[0].toUpperCase()}${text.slice(1)}, so what's worked out with its disulfides may count too few or too many.`;
}

class Composition extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set entity(entity) {
    const style = element("style");
    style.textContent = STYLE;
    const values = entity?.values ?? {};
    const components = values.components ?? [];
    const bonds = values.bonds ?? [];
    const disulfides = bonds.filter((b) => b.kind === "disulfide").length;
    const chains = values.chain_count;
    const copies = components.reduce((n, row) => n + (row.copies ?? 1), 0);
    const summary = element(
      "p",
      {},
      `${components.length} part${components.length === 1 ? "" : "s"}, ${copies} cop${copies === 1 ? "y" : "ies"} in all` +
        (chains !== null && chains !== undefined
          ? `, ${chains} chain${chains === 1 ? "" : "s"}.`
          : "."),
    );
    const out = [style, element("h2", {}, "Composition"), summary, partsTable(entity, components)];
    if (!values.molecular_formula) {
      out.push(
        element(
          "p",
          { role: "status" },
          `${entity?.hrid ?? "It"} has no formula computed yet: it's computed from its parts when it's registered.`,
        ),
      );
    } else {
      const theirs = values.part_disulfides ?? 0;
      out.push(massesTable(values, disulfides, theirs));
      const missing = uncounted(values);
      if (missing) out.push(element("p", { role: "note", className: "warning" }, missing));
      const others = bonds.length - disulfides;
      out.push(
        element(
          "p",
          { className: "muted" },
          "Its parts as they are have each cysteine's thiol free; each disulfide takes two hydrogens off and, as a cystine, adds 125 L/(mol·cm) at 280 nm (Pace's values, which weigh only proteins: a complex with another kind of part, such as a heme, has none)." +
            (others > 0
              ? ` Its ${others} other bond${others === 1 ? "" : "s"} aren't weighed: what each takes off depends on its chemistry.`
              : ""),
        ),
      );
    }
    if (bonds.length) out.push(bondsTable(entity, bonds));
    this.shadowRoot.replaceChildren(...out);
  }
}

customElements.define("complex-composition", Composition);
