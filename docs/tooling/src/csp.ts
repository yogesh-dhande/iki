// A Content Security Policy for every page of a built static site, in a <meta> right after
// its charset. It allows the page's own scripts and styles and nothing else: files from the
// site, and the inline scripts and style elements the build wrote, each by its SHA-256. A
// host that can't set headers (GitHub Pages can't) still serves the policy, since it's in
// the page. Like the app's (server/src/iki_server/settings.py), nothing comes from another
// origin. Search (Pagefind) compiles WebAssembly, which needs 'wasm-unsafe-eval': that
// permits compiling WebAssembly, not eval().
//
// The interactive viewers from @ikisci/science (the plasmid map, Mol*) add style elements
// of their own as they draw, which no hash computed at build time can name. As in the app
// (server/src/iki_server/web/viewers.py), they run in frames of their own whose policy
// allows inline styles; the pages that hold the frames keep the strict policy.
//
// Shared by Iki's docs sites: `addPolicies(dist)` after a build, or the Astro integration.
import { createHash } from "node:crypto";
import { readdir, readFile, writeFile } from "node:fs/promises";
import { join, relative, sep } from "node:path";

import { type DefaultTreeAdapterMap, parse } from "parse5";

type Node = DefaultTreeAdapterMap["node"];
type Element = DefaultTreeAdapterMap["element"];

/** Script types a browser runs; anything else (JSON data) is inert. */
const RUNS = new Set(["", "module", "text/javascript", "application/javascript"]);

export interface Inline {
  scripts: Set<string>;
  styles: Set<string>;
  styleAttributes: Set<string>;
}

export interface PolicyOptions {
  /** A viewer's frame: its viewer adds style elements as it draws. */
  frame?: boolean;
}

function sha256(text: string): string {
  return `'sha256-${createHash("sha256").update(text, "utf-8").digest("base64")}'`;
}

function isElement(node: Node): node is Element {
  return "tagName" in node;
}

function text(element: Element): string {
  return element.childNodes.map((child) => ("value" in child ? child.value : "")).join("");
}

/** The inline scripts, style elements and style attributes in a page. */
export function inline(html: string): { found: Inline; charsetEnd: number } {
  const found: Inline = { scripts: new Set(), styles: new Set(), styleAttributes: new Set() };
  let charsetEnd = -1;
  const visit = (node: Node): void => {
    if (isElement(node)) {
      const attributes = new Map(node.attrs.map(({ name, value }) => [name, value]));
      const style = attributes.get("style");
      if (style !== undefined) {
        found.styleAttributes.add(sha256(style));
      }
      if (node.tagName === "script" && !attributes.has("src")) {
        const type = (attributes.get("type") ?? "").trim().toLowerCase();
        if (RUNS.has(type)) {
          found.scripts.add(sha256(text(node)));
        }
      } else if (node.tagName === "style") {
        found.styles.add(sha256(text(node)));
      } else if (node.tagName === "meta" && attributes.has("charset") && charsetEnd < 0) {
        charsetEnd = node.sourceCodeLocation?.endOffset ?? -1;
      }
    }
    if ("childNodes" in node) {
      node.childNodes.forEach(visit);
    }
    if ("content" in node) {
      visit(node.content);
    }
  };
  visit(parse(html, { sourceCodeLocationInfo: true }));
  return { found, charsetEnd };
}

const sorted = (values: Set<string>): string[] => [...values].sort();

/** The policy for a page with these inline scripts and styles. */
export function policy(
  { scripts, styles, styleAttributes }: Inline,
  options: PolicyOptions = {},
): string {
  let styleSources = ["'self'", ...sorted(styles)];
  if (options.frame) {
    // Hashes and 'unsafe-inline' together would void 'unsafe-inline': a frame lists neither.
    styleSources = ["'self'", "'unsafe-inline'"];
  } else if (styleAttributes.size > 0) {
    styleSources.push("'unsafe-hashes'", ...sorted(styleAttributes));
  }
  return [
    "default-src 'none'",
    ["script-src 'self' 'wasm-unsafe-eval'", ...sorted(scripts)].join(" "),
    `style-src ${styleSources.join(" ")}`,
    "img-src 'self' data: blob:",
    "font-src 'self'",
    // Search fetches its index; a viewer may fetch its own data from the site.
    "connect-src 'self'",
    "worker-src 'self'",
    // Only the site's own viewer frames, and only the site frames them.
    `frame-src ${options.frame ? "'none'" : "'self'"}`,
    "manifest-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
  ].join("; ");
}

/** The page with its policy, right after its charset. */
export function withPolicy(html: string, options: PolicyOptions = {}): string {
  if (html.includes('http-equiv="Content-Security-Policy"')) {
    throw new Error("A page already has a Content Security Policy");
  }
  const { found, charsetEnd } = inline(html);
  if (charsetEnd < 0) {
    throw new Error("A page has no <meta charset> to put its Content Security Policy after");
  }
  const meta = `<meta http-equiv="Content-Security-Policy" content="${policy(found, options)}">`;
  return html.slice(0, charsetEnd) + meta + html.slice(charsetEnd);
}

async function* pages(directory: string): AsyncGenerator<string> {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      yield* pages(path);
    } else if (entry.name.endsWith(".html")) {
      yield path;
    }
  }
}

/** Adds each built page's policy; pages under `frames` (a directory of `dist`) are frames. */
export async function addPolicies(dist: string, frames = "viewers"): Promise<number> {
  let count = 0;
  for await (const path of pages(dist)) {
    const frame = relative(dist, path).split(sep)[0] === frames;
    await writeFile(path, withPolicy(await readFile(path, "utf-8"), { frame }));
    count += 1;
  }
  return count;
}
