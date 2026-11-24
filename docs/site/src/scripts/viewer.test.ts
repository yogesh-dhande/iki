// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { followTheme } from "./viewer.ts";

describe("a viewer's frame", () => {
  it("wears the theme of the page that holds it, and follows its changes", async () => {
    const outer = document.implementation.createHTMLDocument("page");
    outer.documentElement.dataset["theme"] = "dark";
    const frame = { parent: { document: outer }, document } as unknown as Window;
    followTheme(frame);
    expect(document.documentElement.dataset["theme"]).toBe("dark");
    delete outer.documentElement.dataset["theme"];
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.documentElement.dataset["theme"]).toBeUndefined();
  });

  it("follows the system's theme when another site frames it", () => {
    const frame = {
      get parent(): never {
        return {
          get document(): never {
            throw new DOMException("cross-origin", "SecurityError");
          },
        } as never;
      },
      document,
    } as unknown as Window;
    delete document.documentElement.dataset["theme"];
    expect(() => {
      followTheme(frame);
    }).not.toThrow();
    expect(document.documentElement.dataset["theme"]).toBeUndefined();
  });
});
