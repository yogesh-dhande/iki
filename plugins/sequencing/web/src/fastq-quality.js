const style = `
  :host {
    display: grid; gap: .75rem; padding: .75rem; color-scheme: light dark;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 17rem), 1fr));
    font: 14px/1.45 var(--font, system-ui, sans-serif);
    color: var(--text, light-dark(#1d1f21, #e8e9eb));
    background: var(--surface, light-dark(#fff, #1a1b1e));
  }
  section { min-width: 0; overflow-wrap: anywhere; }
  h2, p, dl, dd { margin: 0; }
  h2 { font-size: 1rem; }
  p { margin-top: .125rem; }
  dl { display: grid; grid-template-columns: 1fr auto; gap: .25rem 1rem; margin-top: .5rem; }
  dd { text-align: end; font-variant-numeric: tabular-nums; }
`;

const integers = new Intl.NumberFormat();
const decimals = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

function node(tag, ...contents) {
  const element = document.createElement(tag);
  element.append(...contents);
  return element;
}

function mateSummary(mate) {
  const labels = [
    ["Reads", integers.format(mate.reads)],
    ["Bases", integers.format(mate.bases)],
    ["Shortest read", integers.format(mate.min_length) + " bases"],
    ["Longest read", integers.format(mate.max_length) + " bases"],
    ["Mean Phred score", decimals.format(mate.mean_phred)],
    ["Bases at Q30 or above", decimals.format(mate.q30_percent) + "%"],
  ];
  return node(
    "section",
    node("h2", `Read ${mate.mate}`),
    node("p", mate.file),
    node("dl", ...labels.flatMap(([label, value]) => [node("dt", label), node("dd", value)])),
  );
}

class FastqQuality extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set entity(entity) {
    const mates = entity.values.statistics ?? [];
    this.shadowRoot.replaceChildren(
      node("style", style),
      ...(mates.length ? mates.map(mateSummary) : [node("p", "No FASTQ statistics recorded.")]),
    );
  }
}

customElements.define("fastq-quality", FastqQuality);
