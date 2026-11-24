/** Link the native control to a visible label, help and errors in the same document. */
export interface InputOptions {
  label: string;
  /** Set this and use a native <label for="…">, or use the accessible name in label. */
  inputId?: string;
  describedBy?: string;
  required?: boolean;
  invalid?: boolean;
  disabled?: boolean;
}

export function setControl(
  element: HTMLInputElement | HTMLTextAreaElement,
  data: InputOptions,
  value: string,
): void {
  if (data.inputId) {
    element.id = data.inputId;
    element.removeAttribute("aria-label");
  } else {
    element.removeAttribute("id");
    element.setAttribute("aria-label", data.label);
  }
  element.disabled = !!data.disabled;
  if (data.required) element.setAttribute("aria-required", "true");
  else element.removeAttribute("aria-required");
  if (data.invalid) element.setAttribute("aria-invalid", "true");
  else element.removeAttribute("aria-invalid");
  if (data.describedBy) element.setAttribute("aria-describedby", data.describedBy);
  else element.removeAttribute("aria-describedby");
  if (element.value !== value) element.value = value;
}

export function announce(host: HTMLElement, detail: unknown): void {
  host.dispatchEvent(new CustomEvent("value-change", { detail, bubbles: true, composed: true }));
}
