// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";

import { one } from "./testing.ts";

import { apply, KEY, startThemeSwitch, stored } from "./theme.ts";

describe("the theme switch", () => {
  beforeEach(() => {
    localStorage.clear();
    delete document.documentElement.dataset["theme"];
    document.body.innerHTML = `<div data-theme-switch>
      <button data-choice="system">System</button>
      <button data-choice="light">Light</button>
      <button data-choice="dark">Dark</button></div>`;
  });
  const button = (choice: string) => one<HTMLButtonElement>(document, `[data-choice=${choice}]`);

  it("follows the system until the reader chooses, and keeps the choice", () => {
    startThemeSwitch(document);
    expect(button("system").getAttribute("aria-pressed")).toBe("true");
    button("dark").click();
    expect(document.documentElement.dataset["theme"]).toBe("dark");
    expect(localStorage.getItem(KEY)).toBe("dark");
    expect(button("dark").getAttribute("aria-pressed")).toBe("true");
    expect(button("system").getAttribute("aria-pressed")).toBe("false");
    button("system").click();
    expect(document.documentElement.dataset["theme"]).toBeUndefined();
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it("reads only a choice it knows, and survives storage that refuses", () => {
    expect(stored({ getItem: () => "dark" })).toBe("dark");
    expect(stored({ getItem: () => "sepia" })).toBe("system");
    expect(
      stored({
        getItem: () => {
          throw new Error("denied");
        },
      }),
    ).toBe("system");
    apply(document.documentElement, "light");
    expect(document.documentElement.dataset["theme"]).toBe("light");
  });
});
