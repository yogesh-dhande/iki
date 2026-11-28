// ADRs 0084, 0191 and 0286. Bundles every first-party and example plugin's components with
// the bundler @ikisci/component publishes, from its workspace sources, so Iki's plugins
// are built as a plugin author's are. A plugin whose web/ has a build of its own builds
// itself (small_molecule's RDKit depiction).
import { existsSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import process from "node:process";

import { bundle } from "@ikisci/component/bundle";

const root = resolve(import.meta.dirname, "..");
const plugins = ["plugins", "examples/plugins"].flatMap((parent) =>
  readdirSync(resolve(root, parent), { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => resolve(root, parent, entry.name))
    .filter((plugin) => existsSync(resolve(plugin, "web/src")))
    .filter((plugin) => !existsSync(resolve(plugin, "web/build.js"))),
);

await bundle(plugins, {
  check: process.argv.includes("--check"),
  conditions: ["@ikisci/source"],
  root,
});
