// The written pages' links, checked from their sources: each link to another page, to a key
// of the plugin spec, to a plugin's type or action, to a call of the API or to a file in the
// repository goes somewhere that's there, and use cases, concepts and guides link only to
// the site. The built site's check finds the same, and this
// says which page and which link, without a build.
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { api } from "./api.ts";
import { anchors, pluginHref, plugins } from "./plugins.ts";
import { ROOT } from "./repo.ts";
import { specPages, typeAnchor } from "./spec.ts";

const CONTENT = fileURLToPath(new URL("../content/", import.meta.url));

function mdx(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory()
      ? mdx(join(directory, entry.name))
      : entry.name.endsWith(".mdx")
        ? [join(directory, entry.name)]
        : [],
  );
}

/** Each link a page's Markdown and frontmatter write, outside code. */
export function linksOf(source: string): string[] {
  const text = source.replace(/^```[\s\S]*?^```/gm, "").replace(/`[^`\n]*`/g, "");
  const inline = [...text.matchAll(/\]\(([^)\s]+)\)/g)].map((match) => match[1] ?? "");
  const front = [...text.matchAll(/\bhref:\s*"?([^\s,}"]+)"?/g)].map((match) => match[1] ?? "");
  const html = [...text.matchAll(/\bhref="([^"]+)"/g)].map((match) => match[1] ?? "");
  return [...inline, ...front, ...html];
}

/** A heading's anchor, as Astro's Markdown makes it (GitHub's slugs). */
export function slug(heading: string): string {
  return heading
    .replace(/`/g, "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .replace(/\s/g, "-");
}

/** The anchors a page's source makes: its headings', and any element's own `id`. */
function anchorsOf(source: string): Set<string> {
  const text = source.replace(/^```[\s\S]*?^```/gm, "");
  const found = new Set<string>();
  for (const match of text.matchAll(/^#{2,6} +(.+?) *$/gm)) {
    let id = slug(match[1] ?? "");
    for (let n = 1; found.has(id); n += 1) id = `${slug(match[1] ?? "")}-${String(n)}`;
    found.add(id);
  }
  for (const match of text.matchAll(/\bid="([^"]+)"/g)) found.add(match[1] ?? "");
  return found;
}

/** Every path the written pages are at, from their files, with their anchors. */
function written(): Map<string, Set<string>> {
  const paths = new Map<string, Set<string>>();
  for (const [collection, prefix] of [
    ["guides", "/guides/"],
    ["concepts", "/concepts/"],
    ["use-cases", "/use-cases/"],
    ["reference", "/reference/"],
  ] as const) {
    for (const file of mdx(join(CONTENT, collection))) {
      const path = `${prefix}${relative(join(CONTENT, collection), file).replace(/\.mdx$/, "")}/`;
      paths.set(path, anchorsOf(readFileSync(file, "utf-8")));
    }
    paths.set(prefix, new Set());
  }
  return paths;
}

const spec = specPages();
const specKeys = new Map(
  spec.map((page) => [
    page.href,
    new Set([
      ...page.sections.flatMap((section) => [section.id, ...section.keys.map((key) => key.id)]),
      ...(page.byType.length ? ["by-type", ...page.byType.map((row) => typeAnchor(row.type))] : []),
    ]),
  ]),
);
const pluginAnchors = new Map(
  plugins().map((plugin) => [pluginHref(plugin), new Set(anchors(plugin))]),
);
const calls = new Set(api().all.map((endpoint) => `/reference/api/${endpoint.slug}/`));
const pages = written();

/** Why a link goes nowhere, or null if it goes somewhere. */
export function problemWith(link: string): string | null {
  if (/^(https?:|mailto:|#)/.test(link)) {
    return /github\.com\/[^/]+\/iki\/blob\/[^/]+\/(docs\/adr|spec\/README\.md)/.test(link)
      ? "a design record or the old spec README"
      : null;
  }
  if (link.startsWith("repo:")) {
    const path = link.slice("repo:".length).split("#")[0] ?? "";
    if (path.startsWith("docs/adr/")) return "a design record";
    if (path === "spec/README.md") return "the old spec README";
    return existsSync(join(ROOT, path)) ? null : "a file that isn't in the repository";
  }
  const [path = "", fragment] = link.split("#");
  if (path.startsWith("/reference/plugin-spec/")) {
    const keys = specKeys.get(path);
    if (!keys && path !== "/reference/plugin-spec/") return "a spec page that isn't generated";
    if (fragment && !keys?.has(fragment)) return `no key ${fragment} on ${path}`;
    return null;
  }
  if (path.startsWith("/reference/plugins/")) {
    const ids = pluginAnchors.get(path);
    if (!ids && path !== "/reference/plugins/") return "a plugin that isn't generated";
    if (fragment && !ids?.has(fragment)) return `no type or action ${fragment} on ${path}`;
    return null;
  }
  if (path.startsWith("/reference/api/")) {
    return path === "/reference/api/" || calls.has(path) ? null : "an API call that isn't there";
  }
  if (path === "/reference/" || path === "/") return null;
  const anchors = pages.get(path);
  if (!anchors) return "a page that isn't written";
  return fragment && !anchors.has(fragment) ? `no section ${fragment} on ${path}` : null;
}

describe("the written pages' links", () => {
  const files = mdx(CONTENT);

  it("are found in Markdown, frontmatter and HTML, not in code", () => {
    expect(
      linksOf(
        '[a](/guides/x/) and `[b](/no/)`\n  - { label: C, href: /concepts/y/ }\n<a href="/z/">z</a>\n```\n[d](/no/)\n```',
      ),
    ).toEqual(["/guides/x/", "/concepts/y/", "/z/"]);
  });

  it("know a link that goes nowhere", () => {
    expect(problemWith("/reference/plugin-spec/field/#computed")).toBeNull();
    expect(problemWith("/reference/plugin-spec/field/#no_such_key")).toBe(
      "no key no_such_key on /reference/plugin-spec/field/",
    );
    expect(problemWith("/guides/write-a-plugin/")).toBeNull();
    expect(problemWith("/guides/no-such-guide/")).toBe("a page that isn't written");
    expect(problemWith("/concepts/corrections/#no-such-section")).toBe(
      "no section no-such-section on /concepts/corrections/",
    );
    expect(slug("What `on_edit` returns to, and why")).toBe("what-on_edit-returns-to-and-why");
    expect(problemWith("repo:docs/adr/0035-entity-revisions-provenance-and-edit-rules.md")).toBe(
      "a design record",
    );
    expect(problemWith("repo:spec/README.md")).toBe("the old spec README");
    expect(problemWith("/reference/plugins/lots/#Lot")).toBeNull();
    expect(problemWith("/reference/plugins/lots/#NoSuchType")).toBe(
      "no type or action NoSuchType on /reference/plugins/lots/",
    );
  });

  it("put each table they write by hand in a region a keyboard can scroll", () => {
    const unreachable = files.flatMap((file) =>
      [...readFileSync(file, "utf-8").matchAll(/<div class="table"[^>]*>/g)]
        .filter(([tag]) => !/tabindex="0" role="region" aria-label="[^"]+"/.test(tag))
        .map(([tag]) => `${relative(CONTENT, file)}: ${tag}`),
    );
    expect(unreachable).toEqual([]);
  });

  it("go to the site's own pages from use cases, concepts and guides, never to GitHub", () => {
    const written = ["use-cases", "concepts", "guides"].flatMap((collection) =>
      mdx(join(CONTENT, collection)),
    );
    const outward = written.flatMap((file) => {
      const source = readFileSync(file, "utf-8");
      return [
        ...linksOf(source).filter(
          (link) => link.startsWith("repo:") || link.includes("github.com"),
        ),
        ...(/\bgithub\(/.test(source) ? ["github(…)"] : []),
      ].map((link) => `${relative(CONTENT, file)}: ${link}`);
    });
    expect(outward).toEqual([]);
  });

  it("each go to a page, a key, a call or a file that's there", () => {
    expect(files.length).toBeGreaterThan(10);
    const broken = files.flatMap((file) =>
      linksOf(readFileSync(file, "utf-8")).flatMap((link) => {
        const problem = problemWith(link);
        return problem ? [`${relative(CONTENT, file)}: ${link} (${problem})`] : [];
      }),
    );
    expect(broken).toEqual([]);
  });
});
