import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { plugins } from "./plugins.ts";
import { problems, satisfies, type Prerequisites } from "./prerequisites.ts";

const given = (over: Partial<Prerequisites>): Prerequisites => ({
  plugins: [{ name: "iki/plate_assays", version: ">=0.2" }],
  roles: [{ role: "Analyst", needs: "Project Editor, lab role Assay analyst" }],
  compute: "backend",
  data: [],
  services: [],
  ...over,
});
const known = [{ name: "iki/plate_assays", version: "0.2.1", starters: ["dose-response"] }];

describe("a use case's prerequisites", () => {
  it("read version comparisons as the plugin manifests write them", () => {
    expect(satisfies("0.2.1", ">=0.2")).toBe(true);
    expect(satisfies("0.2.1", ">=0.2,<0.3")).toBe(true);
    expect(satisfies("0.2.1", ">=0.4")).toBe(false);
    expect(satisfies("0.2.1", "0.2.1")).toBe(true);
    expect(() => satisfies("0.2.1", "latest")).toThrow("isn't a version comparison");
  });

  it("name a plugin that isn't there, a version it doesn't have, and a starter it doesn't ship", () => {
    expect(problems(given({}), known)).toEqual([]);
    expect(problems(given({ plugins: [{ name: "iki/nope", version: ">=1" }] }), known)).toEqual([
      "iki/nope isn't one of Iki's plugins",
    ]);
    expect(
      problems(given({ plugins: [{ name: "iki/plate_assays", version: ">=0.4" }] }), known),
    ).toEqual(["iki/plate_assays is at 0.2.1, not >=0.4"]);
    expect(problems(given({ starter: "iki/plate_assays.dose-response" }), known)).toEqual([]);
    expect(problems(given({ starter: "iki/plate_assays.elisa" }), known)).toEqual([
      "no plugin ships the starter iki/plate_assays.elisa",
    ]);
  });

  it("hold for every use case, against Iki's own plugins", () => {
    const directory = fileURLToPath(new URL("../content/use-cases/", import.meta.url));
    const pages = readdirSync(directory).filter((name) => name.endsWith(".mdx"));
    expect(pages.length).toBeGreaterThan(2);
    for (const name of pages) {
      const source = readFileSync(`${directory}${name}`, "utf-8");
      const body = source.split(/^---$/m).slice(2).join("---");
      // "Before you start" follows "In short" and comes before the steps, and the page says
      // where Iki stops.
      const short = body.indexOf("## In short");
      const before = body.indexOf("<BeforeYouStart prerequisites={frontmatter.prerequisites} />");
      const next = body.indexOf("\n## ", short + 1);
      const step = body.indexOf("<Step ");
      expect(short, name).toBeGreaterThanOrEqual(0);
      expect(before, name).toBeGreaterThan(short);
      expect(before < step || step < 0, name).toBe(true);
      expect(next < 0 || before < next, name).toBe(true);
      expect(body, name).toMatch(/^## What Iki does not decide$/m);
    }
    expect(plugins().length).toBeGreaterThan(10);
  });
});
