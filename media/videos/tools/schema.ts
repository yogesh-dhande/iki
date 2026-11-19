// Writes script.schema.json, the JSON Schema of a video's script, for editors.
//   pnpm --filter @ikisci/videos schema
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { scriptJsonSchema } from "../src/system/script/json-schema.ts";
import { root } from "./lib.ts";

writeFileSync(join(root, "script.schema.json"), scriptJsonSchema());
