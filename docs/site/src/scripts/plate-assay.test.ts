// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";

import { one } from "./testing.ts";

import { startPlateAssay } from "./plate-assay.ts";

const FIGURE = `<figure data-plate-assay>
  <button data-toggle aria-pressed="false">Exclude A4</button>
  <button data-well="A1" data-role="Sample" data-detail="1 nM" data-reading="0.93 AU" aria-pressed="false"></button>
  <button data-well="A4" data-role="Sample" data-detail="8 nM" data-reading="0.64 AU" data-excludable aria-pressed="true"></button>
  <p><span data-show="well"></span> <span data-show="role"></span></p>
  <p data-show="detail"></p><p data-show="reading"></p></figure>`;

describe("the worked plate", () => {
  let figure: HTMLElement;
  beforeEach(() => {
    document.body.innerHTML = FIGURE;
    figure = one(document, "figure");
    startPlateAssay(figure);
  });
  const shown = (name: string) => figure.querySelector(`[data-show=${name}]`)?.textContent;
  const well = (name: string) => one<HTMLButtonElement>(figure, `[data-well=${name}]`);

  it("shows the chosen well's reading, one well pressed at a time", () => {
    expect(shown("well")).toBe("A4");
    well("A1").click();
    expect([shown("well"), shown("detail"), shown("reading")]).toEqual(["A1", "1 nM", "0.93 AU"]);
    expect(well("A1").getAttribute("aria-pressed")).toBe("true");
    expect(well("A4").getAttribute("aria-pressed")).toBe("false");
  });

  it("switches between the two fits, and shows the excluded well", () => {
    const toggle = one<HTMLButtonElement>(figure, "[data-toggle]");
    well("A1").click();
    toggle.click();
    expect(figure.hasAttribute("data-excluded")).toBe(true);
    expect(toggle.getAttribute("aria-pressed")).toBe("true");
    expect([shown("well"), shown("role")]).toEqual(["A4", "Sample, excluded"]);
    expect(figure.dataset["selected"]).toBe("excludable");
    toggle.click();
    expect(figure.hasAttribute("data-excluded")).toBe(false);
    expect(shown("role")).toBe("Sample");
  });
});
