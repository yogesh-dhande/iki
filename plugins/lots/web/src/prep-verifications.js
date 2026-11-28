// The prep tab only receives verification records the viewer may read in this project.
// Iki opens a result's page and serves its evidence, keeping revision history and access
// checks on the host. No file or record is fetched by this frame.

import * as iki from "@ikisci/component";

const STYLE = `
  :host { display: block; padding: .5rem; overflow-wrap: anywhere; font: 14px/1.45 var(--font, system-ui, sans-serif); color: var(--text, #1d1f21); background: var(--surface, #fff); }
  h2, h3, dl, dd, ol, p { margin: 0; }
  h2 { font-size: 1rem; }
  h3 { font-size: .95rem; }
  ol { padding: 0; list-style: none; }
  li { padding: .65rem 0; border-bottom: 1px solid var(--border, #e4e4df); }
  dl { display: grid; grid-template-columns: fit-content(40%) minmax(0, 1fr); gap: .1rem .75rem; margin: .25rem 0; }
  dt { color: var(--muted, #5d6064); }
  p { margin-top: .3rem; }
  button { font: inherit; padding: .25rem .65rem; cursor: pointer; border: 1px solid var(--input-border, #8d9095); border-radius: var(--radius-sm, 6px); color: inherit; background: inherit; }
  button:focus-visible, summary:focus-visible { outline: 2px solid var(--focus, #0b6e5f); outline-offset: 2px; }
  .actions { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: .5rem; }
  @media (prefers-color-scheme: dark) {
    :host { color: var(--text, #e8e9eb); background: var(--surface, #1a1b1e); }
    dt { color: var(--muted, #a3a6ab); }
    li { border-color: var(--border, #2d2f33); }
    button { border-color: var(--input-border, #6c6f75); }
    button:focus-visible, summary:focus-visible { outline-color: var(--focus, #5cc8be); }
  }
`;
const RECORD = "iki/lots.record_plasmid_verification";
const SIGNED = new Set(["signed_off", "reviewed", "released"]);
const LABELS = {
  research: "Research",
  regulated_qc: "Regulated QC",
  recorded: "Recorded",
  signed_off: "Signed off",
  submitted: "Submitted",
  reviewed: "Reviewed",
  released: "Released",
  match: "Match",
  discrepancy: "Discrepancy",
  inconclusive: "Inconclusive",
};

function node(tag, ...children) {
  const el = document.createElement(tag);
  el.append(...children);
  return el;
}

function pair(list, label, value) {
  list.append(node("dt", label), node("dd", value));
}

function pinned(item, id, revision) {
  const ref = item.refs?.[id];
  const title = ref?.hrid ?? "Unavailable reference";
  return revision != null ? `${title} · revision ${revision}` : title;
}

function button(text, handler) {
  const el = node("button", text);
  el.type = "button";
  el.addEventListener("click", handler);
  return el;
}

class PrepVerifications extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.records = { items: [], loading: true };
  }

  set entities(entities) {
    this.records = entities;
    this.draw();
  }

  set place(place) {
    this.runs = place?.runs;
    this.draw();
  }

  row(item) {
    const values = item.values;
    const details = node("dl");
    pair(details, "Outcome", LABELS[values.outcome] ?? "Unrecorded");
    pair(details, "Review status", LABELS[values.review] ?? "Unrecorded");
    pair(details, "Review flow", LABELS[values.review_flow] ?? "Unrecorded");
    const reviewer = values.reviewer
      ? (item.people?.[values.reviewer]?.name ?? "Unavailable")
      : "Unassigned";
    pair(details, SIGNED.has(values.review) ? "Reviewer" : "Assigned reviewer", reviewer);
    pair(details, "Plasmid", pinned(item, values.plasmid, values.plasmid_revision));
    if (values.alignment) {
      pair(details, "Alignment", pinned(item, values.alignment, values.alignment_revision));
    }
    if (values.report) pair(details, "Report", "Attached");
    const open = button("Open verification and evidence", () => {
      iki.open(this, item.hrid);
    });
    open.setAttribute("aria-label", `Open verification and evidence for ${item.hrid}`);
    const row = node("li", node("h3", item.hrid), details);
    if (values.plasmid_record_id && item.refs?.[values.plasmid]) {
      row.append(
        node(
          "details",
          node("summary", "Pinned plasmid project record"),
          node("code", values.plasmid_record_id),
        ),
      );
    }
    row.append(open);
    return row;
  }

  draw() {
    const { items, loading, error, limited, more } = this.records;
    const out = [node("style", STYLE), node("h2", "Verification records")];
    if (items.length) {
      const list = node("ol", ...items.map((item) => this.row(item)));
      list.setAttribute("role", "list"); // Retain list semantics with markers hidden in WebKit.
      out.push(list);
    }
    const message =
      error ||
      (loading
        ? "Loading verifications…"
        : limited
          ? `Showing the first ${items.length} verification${items.length === 1 ? "" : "s"}.`
          : items.length
            ? ""
            : "No verification records are visible for this prep.");
    if (message) {
      const status = node("p", message);
      status.setAttribute("role", "status");
      out.push(status);
    }
    const actions = [];
    if (more && !loading && !limited && !error) {
      actions.push(
        button("Show more verifications", () => {
          iki.more(this);
        }),
      );
    }
    if (this.runs?.includes(RECORD)) {
      actions.push(
        button("Record verification", () => {
          iki.run(this, RECORD);
        }),
      );
    }
    if (actions.length) {
      const bar = node("div", ...actions);
      bar.className = "actions";
      out.push(bar);
    }
    this.shadowRoot.replaceChildren(...out);
  }
}

customElements.define("prep-verifications", PrepVerifications);
