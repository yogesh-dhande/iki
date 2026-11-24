// Markdown that isn't a page of its own: an API description, an ADR's status line. Its
// mentions of decisions ("ADR 0126") link to their pages, outside code.
import { markdownToHtml } from "satteri";

import { decisionPath } from "./repo.ts";
import { focusableScrollers } from "./scrollers.ts";

/** Each decision's number, and its page's slug: "0126" → "0126-an-electronic-…". */
export type Decisions = ReadonlyMap<string, string>;

const MENTION = /\bADR (\d{4})\b(?!\])/g;
const FILE_LINK = /\]\((\d{4}-[^)#]+)\.md(#[^)]*)?\)/g;

/** Code, which is left as written: fenced blocks, then spans. */
const CODE = /(^ {0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?^ {0,3}\2[`~]*[ \t]*$|(`+)[\s\S]*?[^`]\3(?!`))/m;

/** Links each "ADR 0126" outside code, and each link to an ADR's file, to its page. */
export function linkDecisions(markdown: string, decisions: Decisions): string {
  return splitCode(markdown)
    .map(({ code, part }) =>
      code
        ? part
        : part
            .replace(FILE_LINK, (link, slug: string, hash?: string) =>
              // A record's file goes to its page; any other file (the template) as written.
              decisions.get(slug.slice(0, 4)) === slug
                ? `](${decisionPath(slug)}${hash ?? ""})`
                : link,
            )
            .replace(MENTION, (text, number: string) => {
              const slug = decisions.get(number);
              return slug ? `[${text}](${decisionPath(slug)})` : text;
            }),
    )
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

/** The Markdown as HTML; `inline` drops a single paragraph's <p>. */
export function render(markdown: string, decisions: Decisions, { inline = false } = {}): string {
  const result = markdownToHtml(linkDecisions(markdown, decisions), {
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
