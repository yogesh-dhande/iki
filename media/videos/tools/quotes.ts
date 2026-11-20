// Whether the product still says what a script quotes from it (the script's `quotes`). A
// quote that's gone means the product changed under the video: a renamed label, a
// reworded message. Update the script and render the video again.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { extname, join, relative } from "node:path";
import type { Video } from "../src/system/script/schema.ts";
import { root } from "./lib.ts";

export const repoRoot = join(root, "..", "..");

const TEXT = new Set([".ts", ".tsx", ".js", ".py", ".yaml", ".yml", ".html", ".json", ".md"]);
const SKIP = new Set(["node_modules", "dist", ".venv", "__pycache__"]);

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    if (SKIP.has(name)) return [];
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return TEXT.has(extname(name)) ? [path] : [];
  });
}

/** The script's quotes that the product no longer says, each as a problem to report. */
export function staleQuotes(video: Video): string[] {
  const read = new Map<string, string[]>();
  return video.quotes.flatMap(({ text, in: where }) => {
    const dir = join(repoRoot, where);
    let texts = read.get(dir);
    if (!texts) {
      try {
        texts = sources(dir).map((file) => readFileSync(file, "utf8"));
      } catch {
        return [`${video.id}: quotes "${text}" from ${where}, which doesn't exist.`];
      }
      read.set(dir, texts);
    }
    return texts.some((source) => source.includes(text))
      ? []
      : [
          `${video.id}: the product no longer says "${text}" (in ${relative(repoRoot, dir)}). Update src/${video.id}/script.yaml to what it says now, then make video VIDEO=${video.id}.`,
        ];
  });
}
