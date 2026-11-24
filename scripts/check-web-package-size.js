// Build each published entry as a browser consumer would, with all its imports.
// Count each chunk or asset (including dynamic imports, fonts and Wasm) once.
import { log } from "node:console";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { gzipSync } from "node:zlib";
import { build } from "vite";

const {
  name,
  exports: entries,
  ikiSizeBudgets: budgets,
} = JSON.parse(await readFile("package.json", "utf8"));
for (const [entry, target] of Object.entries(entries)) {
  const limit = budgets[entry];
  if (!Number.isSafeInteger(limit) || limit <= 0) {
    throw new Error(`Missing byte budget for ${name}${entry.slice(1)}`);
  }
  await readFile(target.types);
  const { output } = await build({
    configFile: false,
    logLevel: "error",
    publicDir: false,
    build: {
      write: false,
      assetsInlineLimit: 0,
      target: "es2023",
      rolldownOptions: {
        input: resolve(target.default),
        preserveEntrySignatures: "strict",
      },
    },
  });
  const bytes = output.reduce(
    (total, file) => total + gzipSync(file.type === "chunk" ? file.code : file.source).byteLength,
    0,
  );
  const result = `${name}${entry.slice(1)}: ${bytes} gzip bytes (budget ${limit})`;
  if (bytes > limit) throw new Error(result);
  log(result);
}
