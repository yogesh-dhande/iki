import { announce, setControl, type InputOptions } from "./input-control.js";

export interface SequenceInputData extends InputOptions {
  value: string;
  /** A single-line sequence by default. HELM uses a multiline editor and live reading. */
  format?: "sequence" | "helm";
  /** A caller-supplied description of the current HELM value. */
  reading?: string;
}

let nextId = 0;

export class SequenceInputElement extends HTMLElement {
  private readonly single = document.createElement("input");
  private readonly multi = document.createElement("textarea");
  private readonly status = document.createElement("p");
  private model: SequenceInputData | undefined;

  constructor() {
    super();
    this.single.type = "text";
    this.multi.rows = 3;
    this.status.className = "field-help";
    this.status.id = `sequence-reading-${String(++nextId)}`;
    this.status.setAttribute("role", "status");
    for (const control of [this.single, this.multi]) {
      control.autocomplete = "off";
      control.spellcheck = false;
      control.className = "input-code";
      control.oninput = () => {
        if (!this.model || this.model.disabled) return;
        this.data = { ...this.model, value: control.value, reading: "" };
        announce(this, control.value);
      };
    }
  }

  get data(): SequenceInputData | undefined {
    return this.model;
  }

  set data(value: SequenceInputData | undefined) {
    this.model = value;
    if (this.isConnected) this.render();
  }

  connectedCallback(): void {
    this.render();
  }

  private render(): void {
    const model = this.model;
    if (!model) {
      this.replaceChildren();
      return;
    }
    const helm = model.format === "helm";
    const control = helm ? this.multi : this.single;
    if (control.parentNode !== this)
      this.replaceChildren(...(helm ? [control, this.status] : [control]));
    setControl(control, model, model.value);
    if (helm) {
      control.setAttribute(
        "aria-describedby",
        [model.describedBy, this.status.id].filter(Boolean).join(" "),
      );
      this.status.textContent = model.reading ?? "";
    }
  }
}

if (!customElements.get("iki-sequence-input"))
  customElements.define("iki-sequence-input", SequenceInputElement);

declare global {
  interface HTMLElementTagNameMap {
    "iki-sequence-input": SequenceInputElement;
  }
}
