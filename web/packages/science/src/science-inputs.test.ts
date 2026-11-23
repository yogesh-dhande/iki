import { assert, expect, it } from "vitest";
import { OTHER_UNIT } from "./quantity-input.js";
import "./sequence-input.js";

it("retains the typed amount when switching between a listed unit and a UCUM code", () => {
  const field = document.createElement("iki-quantity-input");
  field.data = {
    label: "Volume",
    referenceUnit: "mL",
    units: [{ code: "mL", symbol: "mL" }],
    value: { number: "1e", unit: "mL", other: "" },
  };
  document.body.append(field);
  try {
    const changes: unknown[] = [];
    field.addEventListener("value-change", (event) =>
      changes.push((event as CustomEvent<unknown>).detail),
    );
    const select = field.querySelector("select");
    assert(select);
    select.value = OTHER_UNIT;
    select.dispatchEvent(new Event("change"));
    const other = field.querySelector<HTMLInputElement>(".field-sub input");
    assert(other);
    other.value = "L";
    other.dispatchEvent(new Event("input"));
    expect(changes).toEqual([
      { number: "1e", unit: OTHER_UNIT, other: "" },
      { number: "1e", unit: OTHER_UNIT, other: "L" },
    ]);
    select.value = "mL";
    select.dispatchEvent(new Event("change"));
    expect(field.contains(other)).toBe(false);
    expect(field.data.value).toEqual({ number: "1e", unit: "mL", other: "L" });
  } finally {
    field.remove();
  }
});

it("keeps the HELM editor focused while its reading settles and clears stale readings", () => {
  const field = document.createElement("iki-sequence-input");
  const data = { label: "Notation", format: "helm" as const, value: "", reading: "" };
  field.data = data;
  document.body.append(field);
  try {
    const control = field.querySelector("textarea");
    assert(control);
    control.focus();
    field.data = { ...data, reading: "Reading ready" };
    expect(document.activeElement).toBe(control);
    expect(field.querySelector('[role="status"]')?.textContent).toBe("Reading ready");
    control.value = "demo";
    control.dispatchEvent(new Event("input"));
    expect(field.data.value).toBe("demo");
    expect(field.querySelector('[role="status"]')?.textContent).toBe("");
  } finally {
    field.remove();
  }
});
