import { expect, it } from "vitest";

import { eventTitle, factText } from "./observations";

it("titles a first observation as recorded, whether or not its line existed before", () => {
  expect(eventTitle({ kind: "outcome", before: null, after: { outcome: "matched" } })).toBe(
    "Outcome recorded",
  );
  // An expected tube's line is there before it's checked or scanned.
  expect(
    eventTitle({ kind: "outcome", before: { outcome: null }, after: { outcome: "matched" } }),
  ).toBe("Outcome recorded");
  expect(
    eventTitle({
      kind: "scan",
      before: { observed: false, position: null },
      after: { observed: true, position: "A1", code: "LAB-1" },
    }),
  ).toBe("Scan recorded");
});

it("titles a correction by what it did", () => {
  expect(
    eventTitle({ kind: "outcome", before: { outcome: "matched" }, after: { outcome: "damaged" } }),
  ).toBe("Outcome changed");
  expect(
    eventTitle({ kind: "outcome", before: { outcome: "matched" }, after: { outcome: null } }),
  ).toBe("Outcome cleared");
  expect(
    eventTitle({
      kind: "outcome",
      before: { outcome: "extra" },
      after: { outcome: "extra", removed: true },
    }),
  ).toBe("Removed from the checklist");
  expect(
    eventTitle({
      kind: "outcome",
      before: { outcome: "extra", removed: true },
      after: { outcome: "extra", removed: false },
    }),
  ).toBe("Restored to the checklist");
  // A count's scan goes only when its position is scanned again.
  expect(
    eventTitle({
      kind: "scan",
      before: { observed: true, position: "A2", code: null },
      after: null,
    }),
  ).toBe("Scan replaced");
  expect(
    eventTitle({
      kind: "scan",
      before: { observed: true, position: "A2", code: "LAB-1" },
      after: { observed: false, position: null },
    }),
  ).toBe("Scan replaced");
});

it("says what was recorded before the note on it", () => {
  expect(factText("outcome", { note: "Cap cracked", outcome: "damaged", code: "RC000" })).toBe(
    "Damaged · note “Cap cracked”",
  );
  expect(factText("scan", { position: "A2", code: null, observed: true })).toBe("Empty at A2");
  expect(factText("scan", { position: null, observed: false })).toBe("Not scanned");
  expect(factText("scan", { position: "A2", code: "", observed: true })).toBe(
    "A record you can’t open at A2",
  );
});
