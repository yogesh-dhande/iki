// Iki's sandbox serves the module alone. Fallbacks follow iki.css in each color scheme.
const STYLE = `
  :host { display: block; padding: .5rem; overflow-wrap: anywhere; font: 14px/1.5 var(--font, system-ui, sans-serif); color: var(--text, #1d1f21); background: var(--surface, #fff); }
  h2, h3, h4, p, ul, dl, dd { margin: 0; }
  h2, h3 { font-size: 1rem; }
  h4 { font-size: .875rem; }
  section { margin-top: .85rem; }
  dl { display: grid; grid-template-columns: fit-content(40%) 1fr; gap: .15rem .75rem; }
  dt { color: var(--muted, #5d6064); }
  code { font: .95em var(--mono, ui-monospace, monospace); white-space: pre-wrap; }
  .notation { padding: .45rem .6rem; background: var(--surface-2, #f6f6f4); border-radius: var(--radius-sm, 6px); }
  .segment { margin: .4rem 0; padding-left: .75rem; border-left: .2rem solid var(--accent, #0b6e5f); }
  ul { padding-left: 1.25rem; }
  li + li { margin-top: .2rem; }
  button { font: inherit; margin-top: .4rem; padding: .3rem .65rem; cursor: pointer; border-radius: var(--radius-sm, 6px); border: 1px solid transparent; background: var(--accent, #0b6e5f); color: var(--accent-text, white); }
  button:focus-visible { outline: 2px solid var(--focus, #0b6e5f); outline-offset: 2px; }
  @media (prefers-color-scheme: dark) {
    :host { color: var(--text, #e8e9eb); background: var(--surface, #1a1b1e); }
    dt { color: var(--muted, #a3a6ab); }
    .notation { background: var(--surface-2, #212226); }
    .segment { border-color: var(--accent, #3fb8ad); }
    button { background: var(--accent, #3fb8ad); color: var(--accent-text, #06201e); }
    button:focus-visible { outline-color: var(--focus, #5cc8be); }
  }
`;

function node(tag, ...children) {
  const el = document.createElement(tag);
  el.append(...children);
  return el;
}

// A stochastic object is a set of possible fragments. Within it, rows list the allowed
// repeat units and optional implicit caps. The submitted string keeps its explicit ends.
// https://olsenlabmit.github.io/BigSMILES/docs/line_notation.html
function segments(values) {
  const objects = new Map();
  for (const [field, label] of [
    ["repeat_units", "Repeat unit"],
    ["implicit_end_groups", "Implicit end group"],
  ]) {
    for (const row of values[field] ?? []) {
      const object = row.object;
      if (!objects.has(object)) objects.set(object, node("dl"));
      objects.get(object).append(node("dt", label), node("dd", node("code", row.fragment)));
    }
  }
  return [...objects].map(([index, fragments]) => {
    const group = node("div", node("h4", `Stochastic object ${index}`), fragments);
    group.className = "segment";
    return group;
  });
}

function compound(entity, id) {
  const ref = entity.refs?.[id];
  if (!ref) return "Unavailable reference";
  return ref.title && ref.title !== ref.hrid ? `${ref.hrid} · ${ref.title}` : ref.hrid;
}

const INSPECT = "iki/polymer.inspect_bigsmiles";
const STATUS = {
  checked: "Inspected",
  invalid: "Invalid notation",
  unsupported: "Outside the supported notation",
};

function section(title, id, ...children) {
  const heading = node("h3", title);
  heading.id = id;
  const group = node("section", heading, ...children);
  group.setAttribute("aria-labelledby", id);
  return group;
}

class PolymerStructure extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set entity(entity) {
    this.record = entity;
    this.render();
  }

  set place(place) {
    this.runs = place?.runs;
    if (this.record) this.render();
  }

  render() {
    const entity = this.record;
    if (!entity) {
      this.shadowRoot.replaceChildren();
      return;
    }
    const values = entity.values ?? {};
    const design = node("dl");
    for (const [field, label] of [
      ["architecture", "Architecture"],
      ["sequence_distribution", "Sequence distribution"],
      ["end_groups", "Declared end groups"],
      ["design_detail", "Design detail"],
    ]) {
      if (values[field]) design.append(node("dt", label), node("dd", values[field]));
    }

    const raw = node("p", node("code", values.bigsmiles ?? ""));
    raw.className = "notation";
    const notation = section("BigSMILES", "notation", raw);
    const status = STATUS[values.notation_status] ?? "Awaiting inspection";
    notation.append(node("p", status));
    if (values.notation_note) notation.append(node("p", values.notation_note));
    if (values.notation_status === "checked") {
      notation.append(...segments(values));
    } else if (this.runs?.includes(INSPECT)) {
      const button = node("button", "Inspect BigSMILES");
      button.type = "button";
      button.addEventListener("click", () => {
        this.dispatchEvent(new CustomEvent("iki-run", { detail: { action: INSPECT } }));
      });
      notation.append(button);
    }

    const monomers = node("ul");
    for (const id of values.monomers ?? []) monomers.append(node("li", compound(entity, id)));
    this.shadowRoot.replaceChildren(
      node("style", STYLE),
      node("h2", "Polymer ensemble"),
      design,
      notation,
      section("Source monomers", "monomers", monomers),
    );
  }
}

customElements.define("polymer-structure", PolymerStructure);
