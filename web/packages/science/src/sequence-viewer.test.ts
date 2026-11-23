import { afterEach, assert, expect, it, vi } from "vitest";
import "./sequence-viewer.js";

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

it("reports the displayed feature and keyboard range independently of any host app", async () => {
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
  const view = document.createElement("iki-sequence-viewer");
  view.data = {
    title: "Example",
    alphabet: "protein",
    sequence: "MAGK",
    features: [{ type: "misc_feature", name: "Segment", start: 1, end: 3 }],
  };
  let selection: unknown;
  view.addEventListener("selection-change", (e) => {
    selection = (e as CustomEvent<unknown>).detail;
  });
  document.body.append(view);
  await vi.waitFor(() => {
    expect(view.querySelector('[role="application"]')).not.toBeNull();
  });
  const show = view.querySelector<HTMLButtonElement>('button[aria-label="Show Segment"]');
  assert(show);
  show.click();
  await vi.waitFor(() => {
    expect(view.shownFeature?.name).toBe("Segment");
  });
  const bases = view.querySelector('[role="application"]');
  assert(bases);
  bases.dispatchEvent(
    new KeyboardEvent("keydown", { key: "ArrowRight", shiftKey: true, bubbles: true }),
  );
  await vi.waitFor(() => {
    expect(selection).toEqual({ caret: 1, range: [1, 1] });
  });
});

it("shows supplied library results with their provenance and reports the selected feature", async () => {
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
  const view = document.createElement("iki-sequence-viewer");
  const data = { title: "Example", alphabet: "dna" as const, sequence: "ACGT" };
  view.data = { ...data, library: {} };
  document.body.append(view);
  await vi.waitFor(() => {
    expect(view.textContent).toContain("No library search has run.");
  });

  view.data = { ...data, library: { features: [] } };
  await vi.waitFor(() => {
    expect(view.textContent).toContain("No library features matched.");
  });

  const feature = { type: "misc_feature", name: "Segment", start: 2, end: 3 };
  view.data = {
    ...data,
    features: [{ ...feature, name: "Recorded", start: 1 }],
    library: {
      version: "Demo 1",
      features: [{ ...feature, qualifiers: { source: "Example catalogue" } }],
    },
  };
  await vi.waitFor(() => {
    expect(view.textContent).toContain("Example catalogue");
  });
  const section = [...view.querySelectorAll("section")].find(
    (item) => item.querySelector("h2")?.textContent === "Library features",
  );
  assert(section);
  expect(section.textContent).toContain("Demo 1");
  expect(section.textContent).not.toContain("Recorded");
  const show = section.querySelector<HTMLButtonElement>('button[aria-label="Show Segment"]');
  assert(show);
  show.click();
  await vi.waitFor(() => {
    expect(view.shownFeature).toEqual(feature);
  });
});
