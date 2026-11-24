// The decision records (docs/adr), read as the site shows them: each one's number, title,
// status and date from its first lines, the decisions it names in them (Refines, Amends,
// Supersedes…), and its text from its first section on. The index and each decision's
// page are generated from these; nothing about an ADR is written twice.

/** What a decision's status says it is, from the status line's first word. */
export type Standing = "accepted" | "proposed" | "superseded";

export interface Field {
  /** As the record writes it: Refines, Refined by, Amends, Builds on, Supersedes… */
  label: string;
  /** Its value, as Markdown: links name other records' files. */
  markdown: string;
  /** The four-digit numbers of the decisions it links to, in order. */
  decisions: string[];
}

export interface Decision {
  /** Its file's name without `.md`, which is its page's slug. */
  slug: string;
  number: string;
  title: string;
  standing: Standing;
  /** The status line, as Markdown, such as "Accepted (amended by [0125](…))". */
  status: string;
  /** ISO date, as the record gives it. */
  date: string;
  /** Every header field but Status and Date, in the record's order. */
  fields: Field[];
  /** The record from its first section (`## Context`) on. */
  body: string;
}

const TITLE = /^# (\d{4})\. +(.+?)\s*$/;
const ITEM = /^- ([A-Z][A-Za-z -]*?)(?:: *| (?=\[))(.*)$/;
const LINKED = /\]\((\d{4})-[^)]*\.md(?:#[^)]*)?\)/g;

export function linkedDecisions(markdown: string): string[] {
  return [...markdown.matchAll(LINKED)].map((match) => match[1] ?? "");
}

function standingOf(status: string): Standing {
  const word = status.trim().split(/\s+/)[0]?.toLowerCase();
  if (word === "accepted" || word === "proposed" || word === "superseded") {
    return word;
  }
  throw new Error(`Unknown status "${status}"`);
}

/** Reads one record; `slug` is its file name without `.md`. */
export function parseDecision(slug: string, text: string): Decision {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const title = TITLE.exec(lines[0] ?? "");
  if (!title?.[1] || !title[2]) {
    throw new Error(`${slug}.md doesn't start with "# NNNN. Title"`);
  }
  if (!slug.startsWith(`${title[1]}-`)) {
    throw new Error(`${slug}.md is titled ${title[1]}, not its file's number`);
  }
  const items: { label: string; markdown: string }[] = [];
  let index = 1;
  for (; index < lines.length; index += 1) {
    const line = lines[index] ?? "";
    if (line.startsWith("## ")) {
      break;
    }
    const item = ITEM.exec(line);
    const last = items.at(-1);
    if (item?.[1] !== undefined) {
      items.push({ label: item[1], markdown: item[2] ?? "" });
    } else if (/^ {2,}\S/.test(line) && last) {
      last.markdown = `${last.markdown} ${line.trim()}`.trim();
    } else if (line.trim() !== "") {
      throw new Error(`${slug}.md: "${line}" is neither a header field nor a section`);
    }
  }
  const take = (label: string): string => {
    const found = items.find((item) => item.label === label);
    if (!found) {
      throw new Error(`${slug}.md has no ${label}`);
    }
    return found.markdown;
  };
  const status = take("Status");
  return {
    slug,
    number: title[1],
    title: title[2],
    standing: standingOf(status),
    status,
    date: take("Date"),
    fields: items
      .filter((item) => item.label !== "Status" && item.label !== "Date")
      .map((item) => ({ ...item, decisions: linkedDecisions(item.markdown) })),
    body: lines.slice(index).join("\n").trim() + "\n",
  };
}

/** What later records say of this one: each that refines, amends, builds on or
 * supersedes it, by the field that says so. A record's own "Refined by" is kept as
 * written; these are found from the other side, so none is missed. */
export function laterDecisions(
  number: string,
  all: readonly Decision[],
): { label: string; decisions: Decision[] }[] {
  const by: Record<string, string> = {
    Refines: "Refined by",
    Amends: "Amended by",
    "Builds on": "Built on by",
    Extends: "Extended by",
    Supersedes: "Superseded by",
  };
  const groups = new Map<string, Decision[]>();
  for (const other of all) {
    for (const field of other.fields) {
      const label = by[field.label];
      if (label && field.decisions.includes(number)) {
        groups.set(label, [...(groups.get(label) ?? []), other]);
      }
    }
  }
  return [...groups].map(([label, decisions]) => ({ label, decisions }));
}

/** The paragraph of the index README before its own index table: what an ADR is, how to
 * add one and what the statuses mean. The site's index replaces the table. */
export function guideOfIndex(readme: string): string {
  const text = readme.replace(/\r\n/g, "\n");
  const start = text.indexOf("\n") + 1;
  const end = text.indexOf("\n## Index");
  if (end < 0) {
    throw new Error("docs/adr/README.md has no ## Index section to end its guide at");
  }
  return text.slice(start, end).trim() + "\n";
}
