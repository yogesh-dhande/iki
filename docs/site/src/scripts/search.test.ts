// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

import { one } from "./testing.ts";

import { startSearch, statusOf, type Hit } from "./search.ts";

const HEADER = `
  <form data-search><input type="search" id="q"></form>
  <div data-search-panel hidden><p role="status"></p><ol></ol></div>
  <button id="elsewhere">Elsewhere</button>`;

const hit = (title: string): Hit => ({
  url: `/${title.toLowerCase()}/`,
  title: `${title} · Iki docs`,
  excerpt: `about <mark>${title}</mark>`,
});

describe("statusOf", () => {
  it("says what was found, and what to do when nothing was", () => {
    expect(statusOf("", [])).toBe("");
    expect(statusOf("plate", [hit("Plate")])).toBe("1 result for “plate”");
    expect(statusOf("x", [])).toBe("Nothing matches “x”. Try fewer or other words.");
    expect(
      statusOf(
        "a",
        Array.from({ length: 8 }, (_, i) => hit(String(i))),
      ),
    ).toBe("The first 8 results for “a”");
  });
});

describe("startSearch", () => {
  beforeEach(() => {
    document.body.innerHTML = HEADER;
    vi.useFakeTimers();
  });

  const input = () => one<HTMLInputElement>(document, "#q");
  const panel = () => one(document, "[data-search-panel]");
  const type = async (text: string) => {
    input().value = text;
    input().dispatchEvent(new Event("input"));
    await vi.runAllTimersAsync();
  };

  it("shows hits as links as the reader types, titled without the site's name", async () => {
    startSearch(document, (query) => Promise.resolve(query ? [hit("Plate"), hit("Prep")] : []));
    await type("p");
    expect(panel().hidden).toBe(false);
    const links = [...panel().querySelectorAll("a")];
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["/plate/", "/prep/"]);
    expect(links[0]?.querySelector(".hit-title")?.textContent).toBe("Plate");
    expect(panel().querySelector("[role=status]")?.textContent).toBe("2 results for “p”");
    await type("");
    expect(panel().hidden).toBe(true);
  });

  it("keeps the answer to the last query when an earlier one answers late", async () => {
    let release: (hits: Hit[]) => void = () => undefined;
    const slow = new Promise<Hit[]>((resolve) => (release = resolve));
    startSearch(document, (query) => (query === "pl" ? slow : Promise.resolve([hit("Plate")])));
    await type("pl");
    await type("plate");
    release([hit("Plasmid")]);
    await vi.runAllTimersAsync();
    expect(panel().querySelector(".hit-title")?.textContent).toBe("Plate");
  });

  it("says so when search isn't there, rather than failing silently", async () => {
    startSearch(document, () => Promise.reject(new Error("no index")));
    await type("plate");
    expect(panel().querySelector("[role=status]")?.textContent).toMatch(/needs the built site/);
  });

  it("closes with Escape, back to the box, and stays closed until the reader types", async () => {
    startSearch(document, () => Promise.resolve([hit("Plate")]));
    await type("plate");
    input().focus();
    input().dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(panel().hidden).toBe(true);
    expect(document.activeElement).toBe(input());
    input().dispatchEvent(new FocusEvent("focus"));
    expect(panel().hidden).toBe(true);
    input().dispatchEvent(new FocusEvent("blur"));
    input().dispatchEvent(new FocusEvent("focus"));
    expect(panel().hidden).toBe(false);
    one<HTMLButtonElement>(document, "#elsewhere").click();
    expect(panel().hidden).toBe(true);
  });

  it("submitting goes to the first hit", async () => {
    startSearch(document, () => Promise.resolve([hit("Plate")]));
    input().value = "plate";
    one(document, "form").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.runAllTimersAsync();
    expect(document.activeElement?.getAttribute("href")).toBe("/plate/");
  });
});
