// Markdown that isn't a page of its own: the API's descriptions. They cite the design
// records the server's code follows ("(ADR 0126)"), which are for Iki's developers; the
// site leaves those citations out, outside code.
import { markdownToHtml } from "satteri";

import { focusableScrollers } from "./scrollers.ts";

/** Code, which is left as written: fenced blocks, then spans. */
const CODE = /(^ {0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?^ {0,3}\2[`~]*[ \t]*$|(`+)[\s\S]*?[^`]\3(?!`))/m;

/** One or more design records: "ADR 0126", "ADR 0122, 0125", "ADRs 0084 and 0117". */
const RECORDS = String.raw`ADRs? \d{4}(?:(?:,\s*|\s+and\s+)(?:ADRs? )?\d{4})*`;
/** A parenthesis that only cites them, and a citation that ends one: ", ADR 0126)". */
const WHOLE = new RegExp(String.raw`\s*\(${RECORDS}\)`, "g");
const ENDING = new RegExp(String.raw`[,;:]\s*${RECORDS}\)`, "g");

/** The text without its design-record citations: "(ADR 0126)", "(ADR 0084, ADR 0117)" and
 * ", ADR 0126)". */
export function withoutCitations(markdown: string): string {
  return splitCode(markdown)
    .map(({ code, part }) => (code ? part : part.replace(WHOLE, "").replace(ENDING, ")")))
    .join("");
}

/** The text in order, each part code or not. */
export function splitCode(markdown: string): { code: boolean; part: string }[] {
  const parts: { code: boolean; part: string }[] = [];
  let rest = markdown;
  for (let found = CODE.exec(rest); found; found = CODE.exec(rest)) {
    parts.push({ code: false, part: rest.slice(0, found.index) }, { code: true, part: found[0] });
    rest = rest.slice(found.index + found[0].length);
  }
  parts.push({ code: false, part: rest });
  return parts;
}

/** A plugin's own text as HTML: its descriptions mark code with backticks and nothing else,
 * and write brackets as they are, as HELM's "[dR](A)" does, so none of them is a link. */
export function renderText(text: string, options: { inline?: boolean } = {}): string {
  const literal = splitCode(text)
    .map(({ code, part }) => (code ? part : part.replace(/[[\]]/g, "\\$&")))
    .join("");
  return render(literal, options);
}

/** The Markdown as HTML; `inline` drops a single paragraph's <p>. */
export function render(markdown: string, { inline = false } = {}): string {
  const result = markdownToHtml(withoutCitations(markdown), {
    hastPlugins: [focusableScrollers()],
  });
  if (result instanceof Promise) {
    // Only an asynchronous plugin makes it so, and these are all synchronous.
    throw new Error("Rendering Markdown here must be synchronous");
  }
  const html = result.html.trim();
  if (inline) {
    const single = /^<p>([\s\S]*)<\/p>$/.exec(html);
    if (single?.[1] !== undefined && !single[1].includes("<p>")) {
      return single[1];
    }
  }
  return html;
}
