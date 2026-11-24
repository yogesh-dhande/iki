import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { guideOfIndex, laterDecisions, linkedDecisions, parseDecision } from "./adr.ts";
import { ADRS, isDecision } from "./repo.ts";

const RECORD = `# 0208. Whole-plasmid sequencing is checked against the plasmid's own sequence

- Status: Accepted (amended by [0210](0210-delegated.md))
- Date: 2026-11-23
- Refines: [0184](0184-a-plasmid-prep.md), [0163](0163-x.md),
  [0204](0204-y.md#decision)
- Amended by [0210](0210-delegated.md) for delegated actions

## Context

Labs now sequence whole plasmids.
`;

describe("parseDecision", () => {
  it("reads the title, status, date and every field, continued lines included", () => {
    const d = parseDecision("0208-whole-plasmid", RECORD);
    expect(d.number).toBe("0208");
    expect(d.title).toBe("Whole-plasmid sequencing is checked against the plasmid's own sequence");
    expect(d.standing).toBe("accepted");
    expect(d.date).toBe("2026-11-23");
    expect(d.fields.map((f) => [f.label, f.decisions])).toEqual([
      ["Refines", ["0184", "0163", "0204"]],
      ["Amended by", ["0210"]],
    ]);
    expect(d.body).toBe("## Context\n\nLabs now sequence whole plasmids.\n");
  });

  it("refuses a record whose header it can't read", () => {
    expect(() => parseDecision("0209-x", RECORD)).toThrow(/titled 0208/);
    expect(() => parseDecision("0208-x", RECORD.replace("- Date: 2026-11-23\n", ""))).toThrow(
      /no Date/,
    );
    expect(() => parseDecision("0208-x", RECORD.replace("- Date:", "Some prose\n- Date:"))).toThrow(
      /neither a header field nor a section/,
    );
    expect(() => parseDecision("0208-x", RECORD.replace("Accepted", "Pondered"))).toThrow(
      /Unknown status/,
    );
  });

  it("finds the decisions a field links to", () => {
    expect(linkedDecisions("[0126](0126-a.md#x) and [docs](../README.md)")).toEqual(["0126"]);
  });
});

describe("laterDecisions", () => {
  it("finds, from the other side, the records that refine, amend or supersede one", () => {
    const all = [
      parseDecision("0208-a", RECORD),
      parseDecision(
        "0210-b",
        "# 0210. B\n\n- Status: Accepted\n- Date: 2026-11-24\n- Amends: [0208](0208-a.md)\n- Supersedes: [0184](0184-a.md)\n\n## Context\n",
      ),
    ];
    expect(
      laterDecisions("0184", all).map((g) => [g.label, g.decisions.map((d) => d.number)]),
    ).toEqual([
      ["Refined by", ["0208"]],
      ["Superseded by", ["0210"]],
    ]);
    expect(laterDecisions("0208", all).map((g) => g.label)).toEqual(["Amended by"]);
  });
});

describe("the repository's records", () => {
  const names = readdirSync(ADRS).filter(isDecision).sort();
  const all = names.map((name) =>
    parseDecision(name.replace(/\.md$/, ""), readFileSync(join(ADRS, name), "utf-8")),
  );

  it("all read, each linking only to records that are there", () => {
    expect(all.length).toBeGreaterThan(200);
    const numbers = new Set(all.map((d) => d.number));
    const dangling = all.flatMap((d) =>
      d.fields.flatMap((f) =>
        f.decisions.filter((n) => !numbers.has(n)).map((n) => `${d.number} → ${n}`),
      ),
    );
    expect(dangling).toEqual([]);
  });

  it("the index's guide stops before its own table, which the site replaces", () => {
    const guide = guideOfIndex(readFileSync(join(ADRS, "README.md"), "utf-8"));
    expect(guide).toContain("## Adding an ADR");
    expect(guide).not.toContain("## Index");
    expect(() => guideOfIndex("# ADRs\n\nNo index.\n")).toThrow(/no ## Index/);
  });
});
