// Writes a video's captions, out/<video>.vtt, from its script, without rendering.
//   node tools/captions.ts entities
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { toVtt } from "../src/system/script/captions.ts";
import { outDir, planFor, videos } from "./lib.ts";

mkdirSync(outDir, { recursive: true });
for (const video of process.argv.slice(2).length > 0 ? process.argv.slice(2) : videos()) {
  writeFileSync(join(outDir, `${video}.vtt`), toVtt(planFor(video)));
  console.log(`Wrote out/${video}.vtt.`);
}
