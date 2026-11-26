// After the build (and Pagefind's index): every page gets its Content Security Policy.
import { fileURLToPath } from "node:url";

import { addPolicies } from "@ikisci/docs-tooling/csp";

const dist = fileURLToPath(new URL("../dist/", import.meta.url));
const count = await addPolicies(dist);
console.log(`Content Security Policy added to ${String(count)} pages.`);
