import { describe, expect, it } from "vitest";

import { render, renderText, splitCode, withoutCitations } from "./markdown.ts";

describe("withoutCitations", () => {
  it("leaves out each design-record citation, outside code", () => {
    const text =
      "Reuses the run (ADR 0057). Its lot (its material, ADR 0180) and `(ADR 0126)`.\n\n" +
      "```bash\necho (ADR 0126)\n```";
    expect(withoutCitations(text)).toBe(
      "Reuses the run. Its lot (its material) and `(ADR 0126)`.\n\n```bash\necho (ADR 0126)\n```",
    );
  });

  it("leaves out citations of several records, and one that ends a parenthesis", () => {
    expect(
      withoutCitations(
        "Shown in more places (ADR 0084, ADR 0117). Named people (ADR 0122, 0125). " +
          "Registered once (by the record that registered it: ADR 0115), as it was.",
      ),
    ).toBe(
      "Shown in more places. Named people. Registered once (by the record that registered it), as it was.",
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

describe("renderText", () => {
  it("keeps a plugin's brackets as written, not as links", () => {
    const html = renderText("Such as RNA1{[MOE](G)[sP].[dR](A)}, or `[x](y)`.", { inline: true });
    expect(html).not.toContain("<a");
    expect(html).toContain("RNA1{[MOE](G)[sP].[dR](A)}");
    expect(html).toContain("<code>[x](y)</code>");
  });
});
