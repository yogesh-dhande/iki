// Where things are: the repository, its files on GitHub, and which of them are pages.
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

/** A file in the repository, on GitHub. */
export function github(path: string, kind: "blob" | "tree" = "blob"): string {
  return `${REPOSITORY}/${kind}/${REF}/${path}`;
}

/** Each repository file that is a page of the site, and its path there: none yet. A link
 * to any other repository file goes to it on GitHub. */
export function sources(): Record<string, string> {
  return {};
}
