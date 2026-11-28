// The host resolves readable references and opens the requested historical revision.
// The frame displays evidence from the experiment revision it received.

import * as iki from "@ikisci/component";

const CAPTURE = "iki/experiments.capture_results";

const STYLE = `
  :host { display:block; padding:.75rem; font:14px/1.45 var(--font,system-ui,sans-serif);
    color:var(--text,light-dark(#1d1f21,#e8e9eb)); background:var(--surface,light-dark(#fff,#1a1b1e));
    --edge:var(--border-strong,light-dark(#d6d6d0,#3a3c41)); --link:var(--accent,light-dark(#0b6e5f,#3fb8ad)); }
  h2 { font-size:1rem; margin:0 0 .5rem; }
  h3 { font-size:.95rem; margin:1rem 0 .3rem; }
  p, ul { margin:.4rem 0; } ul { padding-left:1.3rem; }
  p, li, dd, td, th { overflow-wrap:anywhere; }
  dl { margin:0; display:grid; grid-template-columns:minmax(6rem,max-content) minmax(0,1fr); column-gap:1rem; }
  dt { font-weight:600; } dd { margin:0; } dt, dd { padding:.2rem 0; }
  .scroll { overflow-x:auto; max-width:100%; }
  table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
  th, td { padding:.3rem .5rem; text-align:left; vertical-align:top; border-bottom:1px solid var(--edge); }
  th { font-size:.85rem; min-width:5rem; }
  .note { white-space:pre-wrap; }
  button { font:inherit; color:var(--link); border:1px solid var(--link); border-radius:var(--radius-sm,6px);
    background:transparent; padding:.25rem .5rem; cursor:pointer; }
  :is(button,.scroll):focus-visible { outline:2px solid var(--link); outline-offset:-2px; }
`;

function node(tag, attributes, ...children) {
  const out = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) out.setAttribute(name, value);
  out.append(...children);
  return out;
}

function facts(entries) {
  const list = node("dl", {});
  for (const [name, value] of entries) {
    if (value != null && value !== "") list.append(node("dt", {}, name), node("dd", {}, value));
  }
  return list;
}

function table(name, headings, rows) {
  const header = node("tr", {}, ...headings.map((h) => node("th", { scope: "col" }, h)));
  const body = node("tbody", {});
  for (const cells of rows) {
    body.append(node("tr", {}, ...cells.map((cell) => node("td", {}, cell))));
  }
  return node(
    "div",
    { class: "scroll", role: "region", "aria-label": name, tabindex: "0" },
    node("table", { "aria-label": name }, node("thead", {}, header), body),
  );
}

function refName(ref) {
  return ref.title && ref.title !== ref.hrid ? `${ref.hrid} · ${ref.title}` : ref.hrid;
}

function version(id, revision) {
  return revision == null ? id : `${id} · revision ${revision}`;
}

class Evidence extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set entity(experiment) {
    this.experiment = experiment;
    this.draw();
  }
  set place(place) {
    this.at = place;
    this.draw();
  }

  revisionLink(id, revision, name, readable) {
    if (!id) return "—";
    const text = version(name, revision);
    if (!readable || readable.purged || !Number.isSafeInteger(revision) || revision < 1)
      return text;
    const button = node("button", { type: "button" }, `${text} (JSON)`);
    button.addEventListener("click", () => {
      iki.open(this, id, revision);
    });
    return button;
  }

  resultRows(pins) {
    return pins.map((pin) => {
      const ref = this.experiment.refs?.[pin.result];
      return [
        this.revisionLink(
          pin.record_id,
          pin.record_revision,
          ref ? refName(ref) : "Restricted result",
          ref,
        ),
        pin.record_id,
        this.revisionLink(pin.entry_id, pin.entry_revision, pin.entry_id, ref),
      ];
    });
  }

  draw() {
    if (!this.experiment) return;
    const entity = this.experiment;
    const v = entity.values ?? {};
    const out = [
      node("style", {}, STYLE),
      node("h2", {}, "Experiment evidence"),
      facts([
        ["ELN entry", v.eln_id],
        ["ELN URL", v.eln_url],
        ["ELN revision", v.eln_revision],
        ["Protocol", v.protocol_id],
        ["Protocol revision", v.protocol_revision],
      ]),
    ];

    out.push(node("h3", {}, "Captured results"));
    if (v.result_pins?.length) {
      out.push(
        table(
          "Captured results",
          ["Result revision", "Project record ID", "Registry entry"],
          this.resultRows(v.result_pins),
        ),
      );
    } else {
      out.push(node("p", {}, "No result revisions captured."));
    }
    if (v.review === "recorded" && this.at?.runs?.includes(CAPTURE)) {
      const button = node("button", { type: "button" }, "Capture results for review");
      button.addEventListener("click", () => {
        iki.run(this, CAPTURE);
      });
      out.push(button);
    }

    if (v.samples?.length) {
      out.push(
        node("h3", {}, "Samples"),
        node(
          "ul",
          {},
          ...v.samples.map((id) => {
            const sample = entity.refs?.[id];
            return node("li", {}, sample ? refName(sample) : "Restricted sample");
          }),
        ),
      );
    }
    if (v.material_draws?.length) {
      out.push(
        node("h3", {}, "Material used"),
        table(
          "Ledger withdrawals",
          ["Amount used", "Stock record", "Source", "Withdrawn at", "Operator", "Movement"],
          v.material_draws.map((d) => [
            `${d.amount} ${d.unit}`,
            version(d.record, d.record_revision),
            d.source ? version(d.source, d.source_revision) : "—",
            d.at,
            entity.people?.[d.by]?.name ?? d.by ?? "—",
            d.movement,
          ]),
        ),
      );
    }
    if (v.observations?.length) {
      out.push(
        node("h3", {}, "Dated observations"),
        table(
          "Dated observations",
          ["Observed at", "Observation"],
          v.observations.map((o) => [
            node("time", { datetime: o.observed_at }, o.observed_at),
            node("span", { class: "note" }, o.note),
          ]),
        ),
      );
    }
    if (v.attachments?.length) {
      out.push(
        node("h3", {}, "Attachments"),
        node(
          "ul",
          {},
          ...v.attachments.map((id) => {
            const file = entity.files?.[id];
            return node(
              "li",
              {},
              file ? `${file.name} · ${file.media_type}` : "Restricted attachment",
            );
          }),
        ),
      );
    }
    this.shadowRoot.replaceChildren(...out);
  }
}
customElements.define("experiment-evidence", Evidence);
