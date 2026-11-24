import { announce, setControl, type InputOptions } from "./input-control.js";

export const OTHER_UNIT = "__other";

/** Preserve the amount exactly as typed, including a partial decimal or exponent. */
export interface QuantityDraft {
  number: string;
  unit: string;
  other: string;
}

export interface QuantityInputData extends InputOptions {
  value: QuantityDraft;
  units: readonly { code: string; symbol: string }[];
  /** The reference unit shown in help for entering another UCUM code. */
  referenceUnit: string;
}

let nextId = 0;

export class QuantityInputElement extends HTMLElement {
  private readonly number = document.createElement("input");
  private readonly select = document.createElement("select");
  private readonly group = document.createElement("div");
  private readonly extra = document.createElement("div");
  private readonly other = document.createElement("input");
  private readonly referenceText = document.createTextNode("");
  private readonly defaultId = `quantity-${String(++nextId)}`;
  private model: QuantityInputData | undefined;

  constructor() {
    super();
    this.group.className = "input-with-unit";
    this.number.type = "text";
    this.number.className = "input-number";
    this.number.inputMode = "decimal";
    this.number.autocomplete = "off";
    this.number.spellcheck = false;
    this.number.oninput = () => {
      this.update({ number: this.number.value });
    };
    this.select.onchange = () => {
      this.update({ unit: this.select.value });
    };
    this.group.append(this.number, this.select);

    this.extra.className = "field-sub";
    this.other.type = "text";
    this.other.className = "input-code";
    this.other.autocomplete = "off";
    this.other.spellcheck = false;
    this.other.oninput = () => {
      this.update({ other: this.other.value });
    };
    const label = document.createElement("label");
    label.textContent = "UCUM unit code";
    this.other.id = `${this.defaultId}-other`;
    label.htmlFor = this.other.id;
    const help = document.createElement("p");
    help.id = `${this.defaultId}-help`;
    help.className = "field-help";
    this.other.setAttribute("aria-describedby", help.id);
    const link = document.createElement("a");
    link.textContent = "UCUM";
    link.href = "https://ucum.org/ucum";
    link.target = "_blank";
    link.rel = "noreferrer";
    const example = document.createElement("code");
    example.textContent = "ug/mL";
    help.append(this.referenceText, link, " codes are, such as ", example, " for μg/mL.");
    this.extra.append(label, this.other, help);
  }

  get data(): QuantityInputData | undefined {
    return this.model;
  }

  set data(value: QuantityInputData | undefined) {
    this.model = value;
    if (this.isConnected) this.render();
  }

  connectedCallback(): void {
    this.render();
  }

  private update(change: Partial<QuantityDraft>): void {
    if (!this.model || this.model.disabled) return;
    const value = { ...this.model.value, ...change };
    this.data = { ...this.model, value };
    announce(this, value);
  }

  private render(): void {
    const model = this.model;
    if (!model) {
      this.replaceChildren();
      return;
    }
    if (!this.contains(this.group)) this.replaceChildren(this.group);
    setControl(this.number, model, model.value.number);
    this.select.disabled = !!model.disabled;
    this.select.setAttribute("aria-label", `Unit for ${model.label}`);
    const options = [...model.units, { code: OTHER_UNIT, symbol: "Other unit…" }];
    if (
      this.select.options.length !== options.length ||
      options.some(
        (unit, i) =>
          unit.code !== this.select.options[i]?.value ||
          unit.symbol !== this.select.options[i].text,
      )
    ) {
      this.select.replaceChildren(...options.map((unit) => new Option(unit.symbol, unit.code)));
    }
    this.select.value = model.value.unit;
    if (model.value.unit === OTHER_UNIT) {
      if (!this.contains(this.extra)) this.append(this.extra);
      this.other.disabled = !!model.disabled;
      if (this.other.value !== model.value.other) this.other.value = model.value.other;
      this.referenceText.data = `Any unit that converts to ${model.referenceUnit}, written as `;
    } else this.extra.remove();
  }
}

if (!customElements.get("iki-quantity-input"))
  customElements.define("iki-quantity-input", QuantityInputElement);

declare global {
  interface HTMLElementTagNameMap {
    "iki-quantity-input": QuantityInputElement;
  }
}
