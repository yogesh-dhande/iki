// Display the report assembled by the release action. The record's signature and Export
// controls remain in the host, where Iki verifies the issued report and audits its download.
const STYLE = `
  :host {
    display: block; min-width: 0; padding: .75rem; overflow-wrap: anywhere;
    color-scheme: light dark;
    --text: light-dark(#1d1f21, #e8e9eb); --surface: light-dark(#fff, #1a1b1e);
    --muted: light-dark(#5d6064, #a3a6ab); --border: light-dark(#e4e4df, #2d2f33);
    color: var(--text); background: var(--surface); font: 14px/1.45 system-ui, sans-serif;
  }
  h2, h3, p, dl, dd { margin: 0; }
  h2 { font-size: 1.125rem; }
  h3 { font-size: 1rem; margin-block: 1rem .25rem; }
  [role=status] { color: var(--muted); margin-block: .25rem .75rem; }
  dl { display: grid; grid-template-columns: minmax(6rem, 28%) minmax(0, 1fr); gap: .2rem .75rem; }
  dt { color: var(--muted); }
  ul { margin: 0; padding: 0; list-style: none; }
  li { padding: .55rem 0; border-bottom: 1px solid var(--border); }
  .row-title { font-weight: 650; }
  dd { font-variant-numeric: tabular-nums; }
`;

function node(tag, ...contents) {
  const element = document.createElement(tag);
  element.append(...contents);
  return element;
}

function facts(entries) {
  return node(
    "dl",
    ...entries.flatMap(([label, value]) => [node("dt", label), node("dd", String(value ?? "—"))]),
  );
}

function label(record) {
  return `${record.hrid ?? record.id} · revision ${record.revision}`;
}

function limit(test) {
  if (test.kind === "conforms") return test.acceptance;
  const { lower, upper, unit } = test;
  if (lower == null) return `≤ ${upper} ${unit}`;
  if (upper == null) return `≥ ${lower} ${unit}`;
  return `${lower}–${upper} ${unit}`;
}

function listOf(items, draw) {
  const list = node("ul", ...items.map(draw));
  list.setAttribute("role", "list");
  return list;
}

function result(test, tests, investigations) {
  const exit = investigations.find((investigation) => investigation.test === test.id);
  const original = tests.find((attempt) => attempt.id === test.repeat_of);
  const title = node("p", test.attribute);
  title.className = "row-title";
  const details = facts([
    ["Test", label(test)],
    ["Method", test.method],
    [
      "Observed",
      test.kind === "conforms"
        ? test.result === "conforms"
          ? "Conforms"
          : "Does not conform"
        : `${test.value} ${test.unit}`,
    ],
    ...(test.observation ? [["Observation", test.observation]] : []),
    ["Acceptance", limit(test)],
    ["Outcome", test.outcome === "pass" ? "Pass" : "Out of specification"],
    ...(original ? [["Repeats", label(original)]] : []),
    ...(test.evidence
      ? [["Assay evidence", `${test.evidence} · revision ${test.evidence_revision}`]]
      : []),
    ...(exit
      ? [
          ["Investigation", label(exit)],
          ["Decision", exit.resolution],
          ["Findings", exit.findings],
        ]
      : []),
  ]);
  return node("li", title, details);
}

class ReleaseCertificate extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set entity(entity) {
    const { report, review } = entity.values;
    const issued = review === "signed_off" || review === "released";
    const status = node("p", issued ? "Issued certificate" : "Draft awaiting signed issue");
    status.setAttribute("role", "status");
    const body = [node("style", STYLE), node("h2", "Certificate of analysis"), status];
    if (report) {
      const lot = report.lot;
      const spec = report.specification;
      body.push(
        facts([
          ["Lot", lot.lot_number ? `${lot.lot_number} · ${label(lot)}` : label(lot)],
          ["Testing date", report.testing_on],
          ["Specification", `${spec.version} · ${label(spec)}`],
          ["Effective from", spec.effective_from],
          ["Source", spec.source],
        ]),
        node("h3", "Release tests"),
        listOf(report.tests, (test) => result(test, report.tests, report.investigations)),
      );
    }
    this.shadowRoot.replaceChildren(...body);
  }
}

customElements.define("release-certificate", ReleaseCertificate);
