#!/usr/bin/env node
// Bundles plugins' components (./bundle.mjs):
//
//   iki-component-bundle [--check] [--condition <name>]... <plugin directory>...
import { relative, resolve } from "node:path";
import process from "node:process";

import { bundle } from "./bundle.mjs";

function usage() {
  return "usage: iki-component-bundle [--check] [--condition <name>]... <plugin directory>...";
}

async function main(argv) {
  const plugins = [];
  const conditions = [];
  let check = false;
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--check") check = true;
    else if (arg === "--condition" && argv[i + 1]) conditions.push(argv[(i += 1)]);
    else if (arg === "--help" || arg === "-h") {
      process.stdout.write(usage() + "\n");
      return;
    } else if (arg.startsWith("-")) throw new Error(`unknown option ${arg}\n${usage()}`);
    else plugins.push(resolve(arg));
  }
  if (!plugins.length) throw new Error(usage());
  const files = await bundle(plugins, { check, conditions });
  for (const file of files) {
    process.stdout.write(`${check ? "checked" : "wrote"} ${relative(process.cwd(), file)}\n`);
  }
}

main(process.argv.slice(2)).catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
