import process from "node:process";
import { URL } from "node:url";
import { readFileSync, writeFileSync } from "node:fs";
import {
  chemicalBundle,
  isCurrentBundle,
} from "../../../web/packages/science/tools/chemical-bundle.mjs";

const adapter = readFileSync(new URL("./src/depiction.js", import.meta.url), "utf8");
const output = new URL("../components/depiction.js", import.meta.url);
if (process.argv.includes("--check")) {
  if (!isCurrentBundle(readFileSync(output, "utf8"), adapter)) {
    throw new Error("Rebuild the depiction with pnpm --filter @ikisci/small-molecule-web bundle");
  }
} else {
  writeFileSync(output, chemicalBundle(adapter));
}
