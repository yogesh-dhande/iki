/** A native button's appearance and accessible name, shared with framework adapters. */
export interface ButtonOptions {
  variant?: "primary" | "quiet" | "danger";
  type?: "button" | "submit";
  /** A fuller accessible name beginning with the visible text. */
  label?: string;
  /** ID of an element in the same document that describes the action. */
  describedBy?: string | undefined;
  disabled?: boolean;
  busy?: boolean;
  busyText?: string;
}

export interface ButtonData extends ButtonOptions {
  /** Text on the button. */
  text: string;
}

export function buttonClass(variant?: ButtonOptions["variant"]): string {
  if (variant === "primary") return "button button-primary";
  if (variant === "danger") return "button button-quiet button-danger";
  if (variant === "quiet") return "button button-quiet";
  return "button";
}

/** Use the same control on a framework-owned native button (including rich labels). */
export function buttonState<T>(options: ButtonOptions, content: T) {
  const busyText = options.busy && options.busyText ? options.busyText : undefined;
  const unavailable = options.disabled === true || options.busy === true;
  return {
    attributes: {
      className: buttonClass(options.variant),
      type: options.type ?? "button",
      "aria-busy": options.busy ? ("true" as const) : undefined,
      "aria-disabled": unavailable ? ("true" as const) : undefined,
      "aria-label": options.label && !busyText ? options.label : undefined,
      "aria-describedby": options.describedBy,
    },
    content: busyText ?? content,
    press: (event: Pick<Event, "preventDefault">, activate?: () => void) => {
      if (unavailable) event.preventDefault();
      else activate?.();
    },
  };
}

/** The light-DOM button participates in its surrounding form and can reference external help. */
export class IkiButton extends HTMLElement {
  #data: ButtonData = { text: "" };
  readonly #control = document.createElement("button");
  readonly #label = this.#control.appendChild(new Text());

  get data(): ButtonData {
    return this.#data;
  }

  set data(value: ButtonData) {
    this.#data = value;
    this.#render();
  }

  connectedCallback(): void {
    this.#render();
    this.replaceChildren(this.#control);
  }

  #render(): void {
    const { attributes, content, press } = buttonState(this.#data, this.#data.text);
    const { className, type, ...aria } = attributes;
    const button = this.#control;
    button.className = className;
    button.type = type;
    for (const [name, value] of Object.entries(aria)) {
      if (value === undefined) button.removeAttribute(name);
      else button.setAttribute(name, value);
    }
    if (this.#label.data !== content) this.#label.data = content;
    button.onclick = (event) => {
      press(event, () => {
        const activated = this.dispatchEvent(
          new CustomEvent("iki-press", { bubbles: true, composed: true, cancelable: true }),
        );
        if (!activated) event.preventDefault();
      });
    };
  }
}

if (!customElements.get("iki-button")) customElements.define("iki-button", IkiButton);

declare global {
  interface HTMLElementTagNameMap {
    "iki-button": IkiButton;
  }
}
