// @vitest-environment jsdom
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "./alignment-viewer.js";
import type { AlignmentData, AlignmentPosition } from "./alignment-viewer.js";

const example: AlignmentData = {
  title: "Synthetic reads",
  referenceRow: 2,
  alignment: {
    kind: "alignment",
    rows: ["AC-", "ACT"].map((aligned, i) => ({
      id: `row-${String(i + 1)}`,
      description: "",
      aligned,
      residues: aligned.replaceAll("-", "").length,
      start: 1,
      spanned: false,
      reverse: false,
    })),
    length: 3,
    alphabet: "nucleotide",
    alphabet_guessed: false,
    identical: 2,
    gapped: 1,
    consensus: "ACT",
    agreement: [100, 100, 50],
    conservation: "** ",
  },
  labels: [
    { text: "Sample A", selectionKey: "sample-a", href: "/samples/a" },
    { text: "Reference" },
  ],
};

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
});

afterEach(async () => {
  await act(async () => {
    document.body.replaceChildren();
    await Promise.resolve();
  });
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("iki-alignment-viewer", () => {
  it("reports file coordinates when a reference is pinned and labels its scrollable grid", () => {
    const viewer = document.createElement("iki-alignment-viewer");
    const positions: AlignmentPosition[] = [];
    viewer.addEventListener("position-change", (event) => {
      positions.push((event as CustomEvent<AlignmentPosition>).detail);
    });
    viewer.data = example;
    act(() => {
      document.body.append(viewer);
    });
    const grid = viewer.querySelector('[role="application"]');
    expect(grid?.getAttribute("aria-label")).toBe("Alignment of Synthetic reads");
    expect(positions.at(-1)).toEqual({ row: 2, column: 1 });
    act(() => {
      grid?.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    });
    act(() => {
      grid?.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true }));
    });
    expect(viewer.position).toEqual({ row: 1, column: 3 });
    expect(viewer.querySelector(".alignment-at")?.textContent).toContain(
      "Sample A, column 3: a gap.",
    );
  });

  it("supports native row links, cancelable navigation and selection with caller-owned keys", () => {
    const viewer = document.createElement("iki-alignment-viewer");
    const keys: string[][] = [];
    viewer.addEventListener("selection-change", (event) => {
      keys.push((event as CustomEvent<string[]>).detail);
    });
    viewer.data = example;
    act(() => {
      document.body.append(viewer);
    });
    const checkbox = viewer.querySelector<HTMLInputElement>('[aria-label="Select Sample A"]');
    act(() => {
      checkbox?.click();
    });
    expect(keys).toEqual([["sample-a"]]);
    expect(checkbox?.checked).toBe(true);
    act(() => {
      viewer.selection = [];
    });
    expect(checkbox?.checked).toBe(false);
    expect(keys).toHaveLength(1);
    const link = viewer.querySelector("a");
    expect(link?.getAttribute("href")).toBe("/samples/a");
    const open = vi.fn((event: Event) => {
      event.preventDefault();
    });
    viewer.addEventListener("row-open", open);
    expect(link?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }))).toBe(
      false,
    );
    expect(open).toHaveBeenCalledTimes(1);
  });
});
