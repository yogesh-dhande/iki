// Writes a video's text outputs from its script, without rendering: its captions
// (out/<video>.vtt and .srt) and what publishing it needs (out/<video>.publish.json and
// .md). `make video` writes them too; this is for a change that moves no pixels.
//   node tools/captions.ts entities
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { outDir, readVideo, textOutputs, videos } from "./lib.ts";

mkdirSync(outDir, { recursive: true });
const named = process.argv.slice(2);
for (const video of named.length > 0 ? named : videos()) {
  if (readVideo(video).specimen) continue;
  for (const [name, text] of Object.entries(textOutputs(video))) {
    writeFileSync(join(outDir, name), text);
    console.log(`Wrote out/${name}.`);
  }
}
