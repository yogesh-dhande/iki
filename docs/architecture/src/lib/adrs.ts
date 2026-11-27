// The ADR index, read from docs/adr's own headers at build time so it can't drift:
// each ADR's number, title, status and date, and the Refines, Amends and Supersedes
// lines that link it to others.
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { REPO } from "./repo.ts";

export type Relation = "refines" | "extends" | "amends" | "supersedes" | "related";

export interface Adr {
  number: string;
  slug: string;
  file: string;
  title: string;
  status: string;
  state: "Accepted" | "Proposed" | "Superseded";
  date: string;
  /** What this ADR's header says it does to others. */
  out: Record<Relation, string[]>;
  /** The ADRs whose headers point here, the inverse of `out`. */
  in: Record<Relation, string[]>;
  /** ADRs this one's status says amended or superseded it. */
  amendedBy: string[];
  supersededBy: string[];
  /** The Context section's first paragraph, for summaries. */
  context: string;
  body: string;
}

const ADR_DIR = path.join(REPO, "docs/adr");
const NUMBER = /\[(\d{4})\]/g;

function numbersIn(text: string): string[] {
  return [...text.matchAll(NUMBER)].map((m) => m[1]);
}

function empty(): Record<Relation, string[]> {
  return { refines: [], extends: [], amends: [], supersedes: [], related: [] };
}

/** The header's list items, with their indented continuation lines joined on. */
function headerItems(lines: string[]): string[] {
  const items: string[] = [];
  for (const line of lines) {
    if (line.startsWith("## ")) break;
    if (line.startsWith("- ")) items.push(line.slice(2));
    else if (/^\s+\S/.test(line) && items.length) items[items.length - 1] += " " + line.trim();
  }
  return items;
}

function firstParagraph(body: string, heading: string): string {
  const at = body.indexOf(`\n## ${heading}`);
  if (at < 0) return "";
  const rest = body.slice(at).split("\n").slice(1);
  const para: string[] = [];
  for (const line of rest) {
    if (!line.trim()) {
      if (para.length) break;
      continue;
    }
    if (line.startsWith("#")) break;
    para.push(line.trim());
  }
  return para.join(" ");
}

function parse(file: string): Adr {
  const text = readFileSync(path.join(ADR_DIR, file), "utf8");
  const lines = text.split("\n");
  const heading = (lines[0] ?? "").replace(/^#\s*/, "");
  const match = /^(\d{4})\.\s+(.*)$/.exec(heading);
  if (!match) throw new Error(`docs/adr/${file} doesn't start with "# NNNN. Title".`);
  const number = match[1];
  const adr: Adr = {
    number,
    slug: file.replace(/\.md$/, ""),
    file: `docs/adr/${file}`,
    title: match[2].replace(/`/g, ""),
    status: "",
    state: "Accepted",
    date: "",
    out: empty(),
    in: empty(),
    amendedBy: [],
    supersededBy: [],
    context: firstParagraph(text, "Context"),
    body: text,
  };
  for (const item of headerItems(lines.slice(1))) {
    // "Refines: [0120](…)", and a few written "Refines [0120](…)" without the colon.
    const m = /^([A-Za-z][A-Za-z ]*?)\s*(?::|(?=\[))\s*(.*)$/.exec(item);
    const key = (m?.[1] ?? item).trim().toLowerCase();
    const value = (m?.[2] ?? "").trim();
    if (key === "status") {
      adr.status = value.replace(/\[(\d{4})\]\([^)]*\)/g, "$1");
      adr.state = value.startsWith("Superseded")
        ? "Superseded"
        : value.startsWith("Proposed")
          ? "Proposed"
          : "Accepted";
      if (adr.state === "Superseded") adr.supersededBy.push(...numbersIn(value));
      else adr.amendedBy.push(...numbersIn(value));
    } else if (key === "date") adr.date = value;
    else if (key === "refines") adr.out.refines.push(...numbersIn(value));
    else if (key === "extends") adr.out.extends.push(...numbersIn(value));
    else if (key === "amends") adr.out.amends.push(...numbersIn(value));
    else if (key === "supersedes") adr.out.supersedes.push(...numbersIn(value));
    else if (key === "related") adr.out.related.push(...numbersIn(value));
    else if (key.startsWith("amended by")) adr.amendedBy.push(...numbersIn(item));
    // "Refined by" and dated "Amended:" notes restate links the other ADR's header makes,
    // or describe a change in place; the inverse links below come from the headers.
  }
  return adr;
}

let cache: Map<string, Adr> | undefined;

export function adrs(): Map<string, Adr> {
  if (cache) return cache;
  const files = readdirSync(ADR_DIR)
    .filter((f) => /^\d{4}-.*\.md$/.test(f) && !f.startsWith("0000-"))
    .sort();
  const all = new Map<string, Adr>();
  for (const file of files) {
    const adr = parse(file);
    if (all.has(adr.number)) throw new Error(`Two ADRs are numbered ${adr.number}.`);
    all.set(adr.number, adr);
  }
  for (const adr of all.values()) {
    for (const relation of Object.keys(adr.out) as Relation[]) {
      for (const target of adr.out[relation]) {
        const other = all.get(target);
        if (!other) throw new Error(`${adr.file} names ADR ${target}, which doesn't exist.`);
        if (!other.in[relation].includes(adr.number)) other.in[relation].push(adr.number);
      }
    }
  }
  cache = all;
  return all;
}

/** One ADR by number ("120", "0120"), or a build error naming the page that cited it. */
export function adr(n: string | number): Adr {
  const number = String(n).padStart(4, "0");
  const found = adrs().get(number);
  if (!found) throw new Error(`The architecture site cites ADR ${number}, which doesn't exist.`);
  return found;
}

export function adrHref(n: string | number): string {
  return `/adrs/${adr(n).number}/`;
}
