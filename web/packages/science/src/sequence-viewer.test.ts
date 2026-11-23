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
