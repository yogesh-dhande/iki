import { afterEach, describe, expect, it, vi } from "vitest";

import "./button.js";

afterEach(() => {
  document.body.replaceChildren();
});

describe("iki-button", () => {
  it("preserves focus while data updates the button and its accessible description", () => {
    const element = document.createElement("iki-button");
    element.data = {
      text: "Save",
      variant: "primary",
      label: "Save changes",
      describedBy: "reason",
    };
    document.body.append(element);
    const button = element.querySelector("button");
    expect(button?.className).toBe("button button-primary");
    expect(button?.getAttribute("aria-label")).toBe("Save changes");
    expect(button?.getAttribute("aria-describedby")).toBe("reason");
    button?.focus();

    element.data = { ...element.data, busy: true, busyText: "Saving" };
    expect(button?.textContent).toBe("Saving");
    expect(button?.hasAttribute("aria-label")).toBe(false);
    expect(button?.getAttribute("aria-busy")).toBe("true");
    expect(button?.getAttribute("aria-disabled")).toBe("true");
    expect(document.activeElement).toBe(button);
    element.data = { text: "Save" };
    expect(button?.hasAttribute("aria-disabled")).toBe(false);
    expect(button?.hasAttribute("aria-describedby")).toBe(false);
  });

  it("submits its form after a press unless a listener cancels it or the button is unavailable", () => {
    const form = document.createElement("form");
    const element = document.createElement("iki-button");
    document.body.append(form);
    form.append(element);
    const press = vi.fn();
    const submit = vi.fn((event: Event) => {
      event.preventDefault();
    });
    form.addEventListener("iki-press", press);
    form.addEventListener("submit", submit);
    const button = element.querySelector("button");
    element.data = { text: "Save", type: "submit" };
    button?.click();
    expect(press).toHaveBeenCalledTimes(1);
    expect(submit).toHaveBeenCalledTimes(1);

    form.addEventListener(
      "iki-press",
      (event) => {
        event.preventDefault();
      },
      { once: true },
    );
    button?.click();
    expect(press).toHaveBeenCalledTimes(2);
    expect(submit).toHaveBeenCalledTimes(1);
    for (const state of [{ disabled: true }, { busy: true }]) {
      element.data = { text: "Save", type: "submit", ...state };
      button?.click();
    }
    expect(press).toHaveBeenCalledTimes(2);
    expect(submit).toHaveBeenCalledTimes(1);

    form.removeChild(element);
    form.append(element);
    element.data = { text: "Preview" };
    element.querySelector("button")?.click();
    expect(press).toHaveBeenCalledTimes(3);
    expect(submit).toHaveBeenCalledTimes(1);
  });
});
