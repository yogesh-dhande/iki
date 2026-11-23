import { URL } from "node:url";
import { cp, rm, writeFile } from "node:fs/promises";
import { build } from "vite";
import { chemicalBundle } from "./tools/chemical-bundle.mjs";

// Only these entries bundle a rendering engine. An ordinary script tag can load
// either built module without a bundler, an import map or separate asset requests.
await build({
  configFile: false,
  root: import.meta.dirname,
  define: { "process.env.NODE_ENV": '"production"' },
  build: {
    target: "es2023",
    emptyOutDir: false,
    license: { fileName: "structure-viewer.LICENSE.md" },
    lib: {
      entry: "src/structure-viewer.ts",
      formats: ["es"],
      fileName: () => "structure-viewer.js",
    },
    rolldownOptions: { output: { codeSplitting: false } },
  },
});
await writeFile(new URL("./dist/chemical-depiction.js", import.meta.url), chemicalBundle());
await cp(new URL("./licenses/", import.meta.url), new URL("./dist/licenses/", import.meta.url), {
  recursive: true,
});

// tsc emits temporary JS and maps for the private Mol* helpers before bundling.
for (const path of ["structure", "structure-viewer.js.map", "chemical-depiction.js.map"]) {
  await rm(new URL("./dist/" + path, import.meta.url), { recursive: true, force: true });
}
