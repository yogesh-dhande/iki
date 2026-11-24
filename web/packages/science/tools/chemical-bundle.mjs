import { Buffer } from "node:buffer";
import { URL } from "node:url";
// The public entry and opaque plugin frame both receive the same RDKit loader,
// unchanged WASM bytes, and custom element. No runtime fetch or JS evaluation.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { gzipSync, gunzipSync } from "node:zlib";
import ts from "typescript";
import { withoutEval } from "./rdkit-no-eval.mjs";

const sha256 = (data) => createHash("sha256").update(data).digest("hex");
const pkg = dirname(dirname(createRequire(import.meta.url).resolve("@rdkit/rdkit")));
const { version, license } = JSON.parse(readFileSync(join(pkg, "package.json"), "utf8"));
const loader = readFileSync(join(pkg, "dist", "RDKit_minimal.js"), "utf8");
const wasm = readFileSync(join(pkg, "dist", "RDKit_minimal.wasm"));
const source = readFileSync(new URL("../src/chemical-depiction.ts", import.meta.url), "utf8");
const component = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2023, module: ts.ModuleKind.ESNext },
}).outputText;

export function chemicalBundle(
  adapter = "",
  packed = gzipSync(wasm, { level: 9 }).toString("base64"),
) {
  return [
    "// Generated from @ikisci/science/chemical-depiction.",
    `// RDKit.js ${version} (@rdkit/rdkit), RDKit's MinimalLib built with Emscripten:`,
    `// ${license}, Copyright (c) 2006-2015, Rational Discovery LLC, Greg Landrum, and Julie`,
    "// Penzotti and others (https://github.com/rdkit/rdkit/blob/master/license.txt); its",
    "// Emscripten runtime MIT (https://github.com/emscripten-core/emscripten). Its",
    "// WebAssembly holds the InChI library 1.07.3 (MIT), CoordGen and RingDecomposerLib",
    "// (BSD-3-Clause), Boost (BSL-1.0), FreeType 2.13.3 (the FreeType License: portions of",
    "// this software are copyright The FreeType Project, https://freetype.org, all rights",
    "// reserved), zlib, and the Telex font (SIL OFL 1.1).",
    `// RDKit_minimal.js sha256 ${sha256(loader)}, with embind's and emval's invokers as`,
    `// closures (tools/rdkit-no-eval.mjs); RDKit_minimal.wasm sha256 ${sha256(wasm)}, unchanged.`,
    withoutEval(loader).trimEnd(),
    `const RDKIT_WASM_GZIP_BASE64 = "${packed}";`,
    component,
    adapter,
  ].join("\n");
}

/** gzip bytes can differ between Node versions. Compare the inflated WASM and everything else. */
export function isCurrentBundle(built, adapter) {
  const packed = /^const RDKIT_WASM_GZIP_BASE64 = "([A-Za-z0-9+/=]+)";$/m.exec(built)?.[1];
  if (!packed) return false;
  try {
    return (
      gunzipSync(Buffer.from(packed, "base64")).equals(wasm) &&
      built === chemicalBundle(adapter, packed)
    );
  } catch {
    return false;
  }
}
