const style = `
  :host {
    display: block; padding: .75rem; color-scheme: light dark; overflow-wrap: anywhere;
    font: 14px/1.45 var(--font, system-ui, sans-serif);
    color: var(--text, light-dark(#1d1f21, #e8e9eb));
    background: var(--surface, light-dark(#fff, #1a1b1e));
  }
  h2 { margin: 0 0 .25rem; font-size: 1rem; }
  p, dl, dd { margin: 0; }
  dl { display: grid; grid-template-columns: 1fr 1fr; gap: .25rem .75rem; }
  section, details { margin: .75rem 0; padding: .5rem 0; border-bottom: 1px solid var(--edge, light-dark(#e4e4df, #2d2f33)); }
  dd { font-variant-numeric: tabular-nums; }
  meter { width: min(100%, 20rem); display: block; }
`;
const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });

function node(tag, ...children) {
  const element = document.createElement(tag);
  element.append(...children);
  return element;
}

function facts(rows) {
  return node("dl", ...rows.flatMap(([key, value]) => [node("dt", key), node("dd", value)]));
}

function peak(row) {
  const glycan = Object.entries(row.glycans)
    .filter(([, count]) => count)
    .map(([residue, count]) => `${residue} × ${count}`)
    .join(", ");
  return node(
    "section",
    node("h2", row.name),
    facts([
      ["Species", row.start ? `${row.analyte} ${row.start}–${row.end}` : row.analyte],
      ["Observed mass", `${number.format(row.observed_da)} Da`],
      ["Expected mass", `${number.format(row.expected_da)} Da`],
      ["Mass error", `${number.format(row.error_ppm)} ppm`],
      ["Tolerance", row.within_tolerance ? "Within tolerance" : "Outside tolerance"],
      ...(glycan ? [["Glycan composition", glycan]] : []),
      ...(row.other_delta_da
        ? [["Other mass delta", `${number.format(row.other_delta_da)} Da · ${row.delta_evidence}`]]
        : []),
    ]),
  );
}

function coverage(row) {
  const meter = node("meter");
  meter.max = 100;
  meter.value = row.percent;
  meter.setAttribute("aria-label", `${row.chain} chain coverage`);
  return node(
    "section",
    node("h2", `${row.chain} chain`),
    node("p", `${row.covered} of ${row.length} residues (${number.format(row.percent)}%)`),
    meter,
  );
}

class BiologicsMSComparison extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set entity(entity) {
    const report = entity.values.comparison;
    const content = [node("style", style)];
    if (report?.peaks) {
      content.push(node("p", `${report.basis} mass · ±${number.format(report.tolerance_ppm)} ppm`));
      content.push(...report.peaks.map(peak));
    } else if (report?.coverage) {
      content.push(...report.coverage.map(coverage));
      const rows = report.peptides.map((peptide) => {
        const mods = peptide.modifications
          .map((mod) => `${mod.name} at residue ${mod.chain_position}`)
          .join(", ");
        return node(
          "li",
          `${peptide.chain} ${peptide.start}–${peptide.end} · ${peptide.sequence}${mods ? ` · ${mods}` : ""}`,
        );
      });
      content.push(node("details", node("summary", "Mapped peptides"), node("ul", ...rows)));
    } else {
      content.push(node("p", "No MS comparison recorded."));
    }
    this.shadowRoot.replaceChildren(...content);
  }
}

customElements.define("biologics-ms-comparison", BiologicsMSComparison);
