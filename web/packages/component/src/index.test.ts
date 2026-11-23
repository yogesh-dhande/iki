import { describe, expect, it } from "vitest";

import { EVENTS, more, open, refine, select } from "./index.js";

function heard(send: (element: EventTarget) => void) {
  const element = new EventTarget();
  const events: { type: string; detail: unknown; bubbles: boolean }[] = [];
  for (const name of Object.values(EVENTS)) {
    element.addEventListener(name, (event) => {
      const { detail, bubbles } = event as CustomEvent<unknown>;
      events.push({ type: name, detail, bubbles });
    });
  }
  send(element);
  return events;
}

describe("a component's element", () => {
  it("asks Iki for things with events that bubble to its bridge", () => {
    expect(
      heard((e) => {
        select(e, ["PRT-000001"]);
      }),
    ).toEqual([{ type: "iki-select", detail: { entities: ["PRT-000001"] }, bubbles: true }]);
    expect(
      heard((e) => {
        more(e);
      }),
    ).toEqual([{ type: "iki-more", detail: null, bubbles: true }]);
    const narrower = { where: { field: "name", op: "starts_with" as const, value: "AB" } };
    expect(
      heard((e) => {
        refine(e, narrower);
      }),
    ).toEqual([{ type: "iki-refine", detail: narrower, bubbles: true }]);
    expect(
      heard((e) => {
        refine(e, null);
      }),
    ).toEqual([{ type: "iki-refine", detail: null, bubbles: true }]);
    expect(
      heard((e) => {
        open(e, "PRT-000002");
      }),
    ).toEqual([{ type: "iki-open", detail: { entity: "PRT-000002" }, bubbles: true }]);
    const record = "00000000-0000-7000-8000-00000000e002";
    expect(
      heard((e) => {
        open(e, record, 4);
      }),
    ).toEqual([{ type: "iki-open", detail: { entity: record, revision: 4 }, bubbles: true }]);
  });
});
