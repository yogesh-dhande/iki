// Galleries run on a plain static file server. Bundle the entries which import React
// together so their stories share its runtime; normal package imports still use ESM.
import { resolve } from "node:path";
import process from "node:process";
import { build } from "vite";

const entries = process.argv.slice(2);
await build({
  configFile: false,
  base: "./",
  publicDir: false,
  build: {
    outDir: "dist/stories",
    emptyOutDir: true,
    assetsInlineLimit: 0,
    target: "es2023",
    rolldownOptions: {
      input: Object.fromEntries(entries.map((name) => [name, resolve("dist", name + ".js")])),
      output: { entryFileNames: "[name].js" },
    },
  },
});
