// Regenerates src/system/theme/tokens.generated.ts from the web app's design tokens.
//   pnpm --filter @ikisci/videos tokens
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { TOKENS_CSS, lightTokens, tokensModule } from "../src/system/theme/css-tokens.ts";

const root = join(import.meta.dirname, "..");
const css = readFileSync(join(root, TOKENS_CSS), "utf8");
writeFileSync(join(root, "src/system/theme/tokens.generated.ts"), tokensModule(lightTokens(css)));
