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

it("titles a count's choice about an unregistered label by what the person said", () => {
  const scanned = { position: "A3", code: "BUFFER A", identity_basis: null };
  expect(
    eventTitle({
      kind: "scan",
      before: null,
      after: { ...scanned, unregistered_action: "additional" },
    }),
  ).toBe("Recorded as another tube");
  const moved = {
    kind: "scan" as const,
    before: { ...scanned, position: "A1" },
    after: { ...scanned, unregistered_action: "move" },
  };
  expect(eventTitle(moved)).toBe("Moved from A1");
  // Said in the title, not again beside the label.
  expect(factText("scan", moved.after)).toBe("BUFFER A at A3");
  // A later reread of the same tube is a scan like any other.
  expect(eventTitle({ kind: "scan", before: moved.after, after: scanned })).toBe("Scan changed");
});
