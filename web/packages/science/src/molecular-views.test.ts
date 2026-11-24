import { assert, expect, it, vi } from "vitest";
import "./complex-composition.js";
import "./oligo-chemistry.js";
import "./oligo-duplex.js";

it("shows named monomers and reports when the user opens the positions table", async () => {
  const chemistry = document.createElement("iki-oligo-chemistry");
  document.body.append(chemistry);
  chemistry.data = {
    nucleotides: [
      {
        position: 1,
        base: "C",
        nucleobase: { name: "5-Methylcytosine", symbol: "5meC", modified: true },
      },
      { position: 2 },
    ],
    ends: { "5′": [{ name: "<label>", symbol: "label" }] },
  };
  const root = chemistry.shadowRoot;
  assert(root);
  expect(root.querySelector(".end")?.getAttribute("aria-label")).toBe("5′ end: <label>");
  expect(root.querySelector("label")).toBeNull();
  expect(root.querySelector(".nt")?.getAttribute("aria-label")).toContain(
    "5-Methylcytosine, modified",
  );
  expect(root.querySelectorAll(".nt")[1]?.getAttribute("aria-label")).toContain("abasic site");
  let detail: unknown = null;
  chemistry.addEventListener("details-change", (e) => {
    detail = (e as CustomEvent<unknown>).detail;
  });
  root.querySelector("summary")?.click();
  await vi.waitFor(() => {
    expect(detail).toEqual({ section: "positions", open: true });
  });
  chemistry.remove();
});

it("draws an existing antiparallel alignment, including overhangs and mismatches", () => {
  const duplex = document.createElement("iki-oligo-duplex");
  duplex.data = {
    sense: "AACT",
    antisense: "GAT",
    pairs: [
      { sense: 2, antisense: 3, paired: true },
      { sense: 3, antisense: 2, paired: false },
    ],
  };
  const root = duplex.shadowRoot;
  assert(root);
  expect(root.querySelector(".bad")?.textContent).toBe("·");
  expect(root.querySelector("p")?.textContent).toContain("1 mismatch or G·U wobble");
  expect(root.querySelector("dl")?.textContent).toContain("sense 2–3, antisense 2–3");
  duplex.data = { sense: "A", antisense: "T", pairs: [{ sense: 9, antisense: 1, paired: true }] };
  expect(root.querySelector('[role="status"]')?.textContent).toMatch(/coordinates/);
  duplex.data = { sense: "A", antisense: "A", pairs: [] };
  expect(root.querySelector('[role="status"]')?.textContent).toMatch(/don't pair/);
  duplex.data = null;
  expect(root.querySelector('[role="status"]')?.textContent).toBe("No pairing supplied.");
});

it("requests computation only in the enabled empty state and keeps missing masses unknown", () => {
  const complex = document.createElement("iki-complex-composition");
  const request = vi.fn();
  complex.addEventListener("compute-request", request);
  complex.data = { parts: [{ name: "Chain A" }], canCompute: true };
  const root = complex.shadowRoot;
  assert(root);
  root.querySelector("button")?.click();
  expect(request).toHaveBeenCalledOnce();
  complex.data = {
    ...complex.data,
    reduced: {
      formula: "C2H6S2",
      average: null,
      monoisotopic: null,
      extinctionCoefficient: null,
    },
  };
  expect(root.querySelector("button")).toBeNull();
  expect(root.querySelectorAll("table")[1]?.textContent).toContain("Average mass—");
});
