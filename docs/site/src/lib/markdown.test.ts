import { describe, expect, it } from "vitest";

import { render, splitCode, withoutCitations } from "./markdown.ts";

describe("withoutCitations", () => {
  it("leaves out each design-record citation, outside code", () => {
    const text =
      "Reuses the run (ADR 0057). Its lot (its material, ADR 0180) and `(ADR 0126)`.\n\n" +
      "```bash\necho (ADR 0126)\n```";
    expect(withoutCitations(text)).toBe(
      "Reuses the run. Its lot (its material) and `(ADR 0126)`.\n\n```bash\necho (ADR 0126)\n```",
    );
  });

  it("knows a code span by its own run of backticks", () => {
    expect(splitCode("a ``x ` y`` b")).toEqual([
      { code: false, part: "a " },
      { code: true, part: "``x ` y``" },
      { code: false, part: " b" },
    ]);
  });
});

describe("render", () => {
  it("renders one paragraph inline, and puts code in the tab order", () => {
    expect(render("Signs with `verify=true` (ADR 0126).", { inline: true })).toBe(
      "Signs with <code>verify=true</code>.",
    );
    expect(render("One.\n\nTwo.", { inline: true })).toBe("<p>One.</p>\n<p>Two.</p>");
    expect(render("```\nwide\n```")).toContain('<pre tabindex="0">');
  });
});
