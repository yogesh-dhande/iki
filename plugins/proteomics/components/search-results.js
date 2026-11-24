const style = `
  :host {
    display: block; padding: .75rem; color-scheme: light dark; overflow-wrap: anywhere;
    font: 14px/1.45 var(--font, system-ui, sans-serif);
    color: var(--text, light-dark(#1d1f21, #e8e9eb));
    background: var(--surface, light-dark(#fff, #1a1b1e));
  }
  h2 { font-size: 1rem; margin: 1rem 0 .25rem; }
  dl { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: .25rem .75rem; }
  dl, dd { margin: 0; }
  ol { display: grid; gap: .5rem; padding-left: 1.5rem; }
  li { padding: .25rem 0; border-bottom: 1px solid var(--edge, light-dark(#e4e4df, #2d2f33)); }
  dd { font-variant-numeric: tabular-nums; }
`;
const labels = {
  protein_group: "Protein groups",
  peptide: "Peptides",
  spectrum_match: "Peptide-spectrum matches",
  precursor: "Precursors",
};
const number = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 8 });

function node(tag, ...children) {
  const element = document.createElement(tag);
  element.append(...children);
  return element;
}

function facts(entries) {
  return node(
    "dl",
    ...entries.flatMap(([label, value]) => [
      node("dt", label),
      node(
        "dd",
        value == null
          ? "Missing"
          : typeof value === "number"
            ? number.format(value)
            : String(value),
      ),
    ]),
  );
}

class ProteomicsSearchResults extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set entity(entity) {
    const { summary, version } = entity.values;
    const content = [node("style", style)];
    if (summary) {
      content.push(
        facts([
          ["Source version", version],
          ...Object.entries(summary.counts).map(([key, count]) => [labels[key], count]),
        ]),
      );
      if (summary.unmatched_accessions.length) {
        content.push(
          node("p", `Selected accessions without hits: ${summary.unmatched_accessions.join(", ")}`),
        );
      }
      content.push(node("h2", "Identification preview"));
      const rows = summary.preview.map((row) =>
        node(
          "li",
          facts([
            ["Identification", row.sequence || row.accessions.join(", ")],
            ...(row.sequence && row.accessions.length
              ? [["Proteins", row.accessions.join(", ")]]
              : []),
            ...Object.entries(row.quantities).slice(0, 6),
          ]),
        ),
      );
      content.push(node("ol", ...rows));
    } else {
      content.push(node("p", "No search report imported."));
    }
    this.shadowRoot.replaceChildren(...content);
  }
}

customElements.define("proteomics-search-results", ProteomicsSearchResults);
