/**
 * Inter (ADR 0176), served from Iki's own static files: the files are the npm package's,
 * byte for byte, and iki.css asks for each over the ranges the package does. After a new
 * version of @fontsource-variable/inter, `pnpm --filter @ikisci/app fonts` copies them.
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/** A file of the repository's, from web/app (as pnpm runs the tests) or from its root. */
function repositoryPath(path: string): string {
  const here = join(process.cwd(), "..", "..", path);
  return existsSync(here) ? here : path;
}

const PACKAGE = "web/app/node_modules/@fontsource-variable/inter";
const SERVED = "server/src/iki_server/web/static/iki/fonts";
const SUBSETS = ["latin", "latin-ext", "greek"];

/** Each @font-face's file and unicode-range, by file name. */
function ranges(css: string): Map<string, string> {
  const found = new Map<string, string>();
  for (const face of css.matchAll(/@font-face\s*{([^}]*)}/g)) {
    const body = face[1] ?? "";
    const file = /url\(["']?(?:\.\/files\/|fonts\/)?([^"')]+)["']?\)/.exec(body)?.[1];
    const range = /unicode-range:\s*([^;]+);/.exec(body)?.[1];
    if (file && range) found.set(file, range.replace(/\s+/g, ""));
  }
  return found;
}

describe("Inter", () => {
  it("is served as the package publishes it", () => {
    for (const subset of SUBSETS) {
      const name = `inter-${subset}-wght-normal.woff2`;
      const published = readFileSync(repositoryPath(`${PACKAGE}/files/${name}`));
      const served = readFileSync(repositoryPath(`${SERVED}/${name}`));
      expect(served.equals(published), name).toBe(true);
    }
    const licence = readFileSync(repositoryPath(`${PACKAGE}/LICENSE`), "utf8");
    expect(readFileSync(repositoryPath(`${SERVED}/inter-LICENSE.txt`), "utf8")).toBe(licence);
  });

  it("covers the ranges the package's stylesheet does", () => {
    const published = ranges(readFileSync(repositoryPath(`${PACKAGE}/wght.css`), "utf8"));
    const ours = ranges(
      readFileSync(repositoryPath("server/src/iki_server/web/static/iki/iki.css"), "utf8"),
    );
    expect([...ours.keys()]).toEqual(SUBSETS.map((s) => `inter-${s}-wght-normal.woff2`));
    for (const [file, range] of ours) expect(range, file).toBe(published.get(file));
  });
});
