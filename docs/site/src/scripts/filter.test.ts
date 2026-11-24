// @vitest-environment jsdom
import { one } from "./testing.ts";
import { describe, expect, it } from "vitest";

import { matches, startFilter } from "./filter.ts";

describe("the decisions' filter", () => {
  it("needs every word, and the status when one is chosen", () => {
    expect(matches("0208 whole-plasmid sequencing", "accepted", "plasmid 0208", "")).toBe(true);
    expect(matches("0208 whole-plasmid sequencing", "accepted", "plasmid rna", "")).toBe(false);
    expect(matches("0208 whole-plasmid sequencing", "accepted", "", "proposed")).toBe(false);
  });

  it("hides the rows that don't match and says how many do", () => {
    document.body.innerHTML = `<section>
      <form data-filter><input name="q"><select name="status">
        <option value=""></option><option value="proposed"></option></select></form>
      <p data-count></p>
      <ol><li data-standing="accepted" data-text="0208 whole-plasmid"></li>
          <li data-standing="proposed" data-text="0206 annotation"></li></ol></section>`;
    const section = one(document, "section");
    startFilter(section);
    const count = one(section, "[data-count]");
    const rows = [...section.querySelectorAll("li")];
    expect(count.textContent).toBe("");
    one<HTMLInputElement>(section, "input").value = "plasmid";
    one(section, "form").dispatchEvent(new Event("input"));
    expect(rows.map((row) => row.hidden)).toEqual([false, true]);
    expect(count.textContent).toBe("1 of 2 decisions");
    one<HTMLSelectElement>(section, "select").value = "proposed";
    one(section, "form").dispatchEvent(new Event("input"));
    expect(count.textContent).toMatch(/No decision matches/);
  });
});
