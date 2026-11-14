// A sample's tube label. Iki sets `entity` to the sample whose page it's on: its
// `hrid`, `title` and `values`, as the person viewing it may read them.
const STYLE = `
  :host { display: block; padding: 1rem; color: CanvasText; }
  .label {
    display: inline-grid;
    gap: 0.25rem;
    padding: 0.75rem 1rem;
    border: 2px solid currentColor;
    border-radius: 6px;
    font: 0.9rem ui-monospace, monospace;
  }
  .hrid { font-size: 1.25rem; font-weight: 700; }
`;

class SampleLabel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  set entity(entity) {
    const style = document.createElement("style");
    style.textContent = STYLE;
    const label = document.createElement("div");
    label.className = "label";
    const lines = [
      ["hrid", entity.hrid],
      ["name", entity.values.name],
      ["kind", entity.values.kind],
      ["storage", entity.values.storage ?? "No storage yet"],
    ];
    for (const [name, text] of lines) {
      const line = document.createElement("span");
      line.className = name;
      line.textContent = text ?? "";
      label.append(line);
    }
    this.shadowRoot.replaceChildren(style, label);
  }
}

customElements.define("example-sample-label", SampleLabel);
