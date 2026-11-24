import { afterEach, assert, describe, expect, it } from "vitest";
import "./plasmid-map.js";
import "./plasmid-thumbnail.js";

afterEach(() => {
  document.body.replaceChildren();
});

describe("plasmid map", () => {
  it("uses one-based closed spans, keeps the selected view and reports layer changes", () => {
    const map = document.createElement("iki-plasmid-map");
    map.data = {
      length: 400,
      name: "Synthetic circle",
      topology: "circular",
      parts: { items: [{ start: 350, end: 30, name: "Wrap", type: "misc_feature" }] },
      orfs: { items: [] },
    };
    document.body.append(map);
    const root = map.shadowRoot;
    assert(root);
    let view: unknown, layer: unknown;
    map.addEventListener("view-change", (event) => {
      view = (event as CustomEvent<unknown>).detail;
    });
    map.addEventListener("layer-change", (event) => {
      layer = (event as CustomEvent<unknown>).detail;
    });
    const button = (name: string) =>
      Array.from(root.querySelectorAll("button")).find((b) => b.textContent === name);
    expect(root.querySelector("table")?.textContent).toContain("350–400, 1–30");
    button("Linear")?.click();
    expect(view).toEqual({ view: "line" });
    map.data = { ...map.data, linkedPrimerIds: [] };
    expect(button("Linear")?.getAttribute("aria-pressed")).toBe("true");
    button("Library parts (1)")?.click();
    expect(layer).toEqual({ key: "parts", shown: false });
    expect(root.querySelector("table")).toBeNull();
  });

  it("offers only linked primer IDs to the host", () => {
    const map = document.createElement("iki-plasmid-map");
    const sites = ["first", "second"].map((primer) => ({
      primer,
      name: primer,
      start: 1,
      end: 2,
      three_prime: 2,
      annealed: 2,
      tail: 0,
    }));
    map.data = {
      name: "Synthetic segment",
      topology: "linear",
      length: 4,
      sites: { items: sites },
      linkedPrimerIds: ["first"],
    };
    let selected: unknown;
    document.body.addEventListener(
      "primer-open",
      (e) => {
        selected = (e as CustomEvent<unknown>).detail;
      },
      { once: true },
    );
    document.body.append(map);
    const buttons = map.shadowRoot?.querySelectorAll<HTMLButtonElement>("table button");
    expect(buttons).toHaveLength(1);
    buttons?.[0]?.click();
    expect(selected).toEqual({ id: "first" });
  });
});

describe("map thumbnail", () => {
  it("draws a whole circle and both sides of a wrapping feature, then opens by DOM event", () => {
    const map = document.createElement("iki-plasmid-thumbnail");
    map.data = {
      title: "Synthetic circle",
      length: 40,
      alphabet: "dna",
      circular: true,
      features: [
        { type: "gene", name: "Whole", start: 1, end: 40 },
        { type: "misc_feature", name: "Wrap", start: 38, end: 3 },
      ],
      openLabel: "Open map",
    };
    document.body.append(map);
    expect(map.querySelector('[role="img"]')?.getAttribute("aria-label")).toBe(
      "Circular map of Synthetic circle, 40 bp: Whole, Wrap",
    );
    expect(map.querySelectorAll("path")).toHaveLength(3);
    let opened = false;
    map.addEventListener("thumbnail-open", (event) => {
      opened = event.bubbles && event.composed;
    });
    map.querySelector("button")?.click();
    expect(opened).toBe(true);
  });
});
