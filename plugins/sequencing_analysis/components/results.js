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

const number = new Intl.NumberFormat();

function node(tag, ...contents) {
  const element = document.createElement(tag);
  element.append(...contents);
  return element;
}

function facts(data) {
  return node(
    "dl",
    ...Object.entries(data).flatMap(([key, value]) => [
      node("dt", key),
      node("dd", typeof value === "number" ? number.format(value) : String(value || "—")),
    ]),
  );
}

class SequencingResults extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set entity(entity) {
    const { summary, pipeline_context: context } = entity.values;
    this.shadowRoot.replaceChildren(
      node("style", style),
      ...(summary && context
        ? [
            facts({
              Pipeline: context.pipeline,
              Version: context.version,
              ...(context.reference ? { Reference: context.reference } : {}),
              ...(summary.selector ? { Selection: summary.selector } : {}),
              ...summary.metrics,
            }),
            ...(summary.preview.length
              ? [
                  node("h2", "Selected rows"),
                  node("ol", ...summary.preview.map((row) => node("li", facts(row)))),
                ]
              : []),
          ]
        : [node("p", "No report imported.")]),
    );
  }
}

customElements.define("sequencing-results", SequencingResults);
