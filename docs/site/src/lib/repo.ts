// Where things are: the repository, its files on GitHub, and which of them are pages.
import { readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

declare const __IKI_ROOT__: string | undefined;

/** The repository's root, as a path. Astro's config passes it to the build (`define`),
 * whose bundled pages don't sit where this file does; read in place, it's found from here. */
export const ROOT =
  typeof __IKI_ROOT__ === "string"
    ? __IKI_ROOT__
    : fileURLToPath(new URL("../../../../", import.meta.url));
/** The GitHub account that hosts the repository (docs/tests/test_release_owner.py). */
export const REPOSITORY = "https://github.com/yogesh-dhande/iki";
/** What links to the repository's files point at. */
export const REF = "main";
export const ADRS = join(ROOT, "docs", "adr");

/** A file in the repository, on GitHub. */
export function github(path: string, kind: "blob" | "tree" = "blob"): string {
  return `${REPOSITORY}/${kind}/${REF}/${path}`;
}

/** An ADR's file name is its page's slug: `0184-a-plasmid-prep-…`. */
export function isDecision(name: string): boolean {
  return /^\d{4}-.+\.md$/.test(name) && !name.startsWith("0000-");
}

export function decisionPath(slug: string): string {
  return `/decisions/${slug}/`;
}

/** Each repository file that is a page of the site, and its path there. */
export function sources(): Record<string, string> {
  const found: Record<string, string> = { "docs/adr/README.md": "/decisions/" };
  for (const name of readdirSync(ADRS).sort()) {
    if (isDecision(name)) {
      found[`docs/adr/${name}`] = decisionPath(name.replace(/\.md$/, ""));
    }
  }
  return found;
}
