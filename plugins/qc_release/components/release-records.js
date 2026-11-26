const STYLE = `
  :host {
    display: block; min-width: 0; padding: .75rem; overflow-wrap: anywhere;
    color-scheme: light dark;
    --text: light-dark(#1d1f21, #e8e9eb); --surface: light-dark(#fff, #1a1b1e);
    --muted: light-dark(#5d6064, #a3a6ab); --border: light-dark(#e4e4df, #2d2f33);
    --accent: light-dark(#0b6e5f, #5cc8be);
    color: var(--text); background: var(--surface); font: 14px/1.45 system-ui, sans-serif;
  }
  ul, p { margin: 0; }
  ul { padding: 0; list-style: none; }
  li { display: flex; align-items: center; gap: .65rem; flex-wrap: wrap;
    padding: .55rem 0; border-bottom: 1px solid var(--border); }
  li p { flex: 1 1 12rem; }
  small { display: block; color: var(--muted); font-size: .85rem; }
  .actions { margin-top: .65rem; display: flex; gap: .4rem; flex-wrap: wrap; }
  button { font: inherit; background: var(--surface); color: var(--accent);
    padding: .3rem .65rem; border: 1px solid var(--muted); border-radius: 6px; cursor: pointer; }
  button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
`;

const OPEN = "iki/qc_release.open_release";
const ACTIONS = {
  [OPEN]: "Open release testing",
  "iki/qc_release.record_test": "Record a release test",
  "iki/qc_release.prepare_certificate": "Prepare a certificate of analysis",
};

function node(tag, ...contents) {
  const element = document.createElement(tag);
  element.append(...contents);
  return element;
}

function summary(item) {
  const v = item.values;
  if (v.attribute) {
    const out = v.outcome === "pass" ? "Pass" : "Out of specification";
    // An investigational test informs its investigation and replaces nothing.
    const role = v.purpose === "investigational" ? " · Investigational" : "";
    return [v.attribute, `${v.value} ${v.unit} · ${out}${role} · ${v.method}`];
  }
  if (v.version) {
    const state = v.sealed == null ? "" : v.sealed ? " · Certificate prepared" : " · Testing open";
    return [item.title, `Specification ${v.version} · ${v.testing_on}${state}`];
  }
  const decisions = {
    invalidated: "Invalidated",
    confirmed: "Confirmed",
    inconclusive: "Inconclusive",
  };
  return [item.title, decisions[v.resolution] ?? "Investigation open"];
}

class ReleaseRecords extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.records = { items: [], loading: true };
  }

  set entities(records) {
    this.records = records;
    this.draw();
  }

  set entity(entity) {
    this.sealed = entity.values.sealed === true;
    this.draw();
  }

  set place(place) {
    this.runs = place.runs;
    this.draw();
  }

  button(label, event, detail) {
    const button = node("button", label);
    button.type = "button";
    button.addEventListener("click", () => this.dispatchEvent(new CustomEvent(event, { detail })));
    return button;
  }

  row(item) {
    const [title, sub] = summary(item);
    const open = this.button(`Open ${item.hrid}`, "iki-open", { entity: item.hrid });
    return node("li", node("p", title, node("small", sub)), open);
  }

  draw() {
    const { items, loading, error, limited, more } = this.records;
    const body = [node("style", STYLE)];
    if (items.length) {
      const list = node("ul", ...items.map((item) => this.row(item)));
      list.setAttribute("role", "list");
      body.push(list);
    }
    if (loading || error || !items.length || limited) {
      const message =
        error || (loading ? "Loading…" : limited ? "Record limit reached." : "No records yet.");
      const status = node("p", message);
      status.setAttribute("role", "status");
      body.push(status);
    }
    const actions = [];
    if (more && !loading && !limited && !error) {
      actions.push(this.button("Show more", "iki-more"));
    }
    if (!this.sealed && !loading && !error) {
      for (const action of this.runs ?? []) {
        if (action === OPEN && items.length) continue;
        if (ACTIONS[action]) actions.push(this.button(ACTIONS[action], "iki-run", { action }));
      }
    }
    if (actions.length) {
      const controls = node("div", ...actions);
      controls.className = "actions";
      body.push(controls);
    }
    this.shadowRoot.replaceChildren(...body);
  }
}

customElements.define("lot-release", class extends ReleaseRecords {});
customElements.define("release-tests", class extends ReleaseRecords {});
customElements.define("case-certificate", class extends ReleaseRecords {});
customElements.define("test-investigations", class extends ReleaseRecords {});
