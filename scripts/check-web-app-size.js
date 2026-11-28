// Weigh what the built web app loads before it draws its first page: each entry's chunks and
// every chunk they import, but not the pages it fetches when they're first opened. A page
// imported from a module the app starts with would load with it, and so with every page.
// Run from web/app after `vite build`; the budgets are in its package.json, in gzip bytes.
import { log } from "node:console";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

const OUT_DIR = "../../server/src/iki_server/web/static/app";

const { ikiSizeBudgets: budgets } = JSON.parse(await readFile("package.json", "utf8"));
const manifest = JSON.parse(await readFile(join(OUT_DIR, "manifest.json"), "utf8"));
const entries = Object.keys(manifest).filter((key) => manifest[key].isEntry);
for (const entry of Object.keys(budgets)) {
  if (!entries.includes(entry)) throw new Error(`${entry} has a budget but isn't an entry`);
}
const failed = [];
for (const entry of entries) {
  const limit = budgets[entry];
  if (!Number.isSafeInteger(limit) || limit <= 0) {
    throw new Error(`Missing byte budget for the web app's ${entry}`);
  }
  const loaded = new Set();
  const next = [entry];
  while (next.length) {
    const key = next.pop();
    if (loaded.has(key)) continue;
    loaded.add(key);
    next.push(...(manifest[key].imports ?? []));
  }
  let bytes = 0;
  for (const key of loaded) {
    bytes += gzipSync(await readFile(join(OUT_DIR, manifest[key].file))).byteLength;
  }
  const result = `@ikisci/app ${entry}: ${String(loaded.size)} chunks, ${String(bytes)} gzip bytes (budget ${String(limit)})`;
  if (bytes > limit) failed.push(result);
  else log(result);
}
if (failed.length) {
  throw new Error(
    `${failed.join("\n")}\nWhat the app starts with grew: a page imported statically loads with it.`,
  );
}
