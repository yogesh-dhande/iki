import { describe, expect, it } from "vitest";

import { linkDecisions, render, splitCode } from "./markdown.ts";

const decisions = new Map([["0126", "0126-an-electronic-signature"]]);

describe("linkDecisions", () => {
  it("links each mention of a decision, and each link to its file, outside code", () => {
    const text =
      "See ADR 0126, [the file](0126-an-electronic-signature.md#decision) and `ADR 0126`.\n\n" +
      "```bash\necho ADR 0126\n```\n\nNot ADR 9999, nor [ADR 0126](elsewhere).";
    expect(linkDecisions(text, decisions)).toBe(
      "See [ADR 0126](/decisions/0126-an-electronic-signature/), " +
        "[the file](/decisions/0126-an-electronic-signature/#decision) and `ADR 0126`.\n\n" +
        "```bash\necho ADR 0126\n```\n\nNot ADR 9999, nor [ADR 0126](elsewhere).",
    );
  });

  it("leaves a link to a file that isn't a record, such as the template", () => {
    expect(linkDecisions("Copy [it](0000-template.md).", decisions)).toBe(
      "Copy [it](0000-template.md).",
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
    expect(render("Signs with `verify=true` (ADR 0126).", decisions, { inline: true })).toBe(
      'Signs with <code>verify=true</code> (<a href="/decisions/0126-an-electronic-signature/">ADR 0126</a>).',
    );
    expect(render("One.\n\nTwo.", decisions, { inline: true })).toBe("<p>One.</p>\n<p>Two.</p>");
    expect(render("```\nwide\n```", decisions)).toContain('<pre tabindex="0">');
  });
});
