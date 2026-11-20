// Pulls review frames from a rendered video: one near the end of each beat, when its
// steps have finished and its caption is still up, plus each transition's middle. They
// come from the captioned render, so each caption can be checked against its scene.
//   node tools/frames.ts entities <dir>
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { outputFiles } from "../src/system/script/publish.ts";
import { duration } from "../src/system/theme/motion.ts";
import { ffmpeg, outDir, planFor } from "./lib.ts";

const [video, dir] = process.argv.slice(2);
if (!video || !dir) throw new Error("node tools/frames.ts <video> <dir>");
const movie = join(outDir, outputFiles(video).captioned);
if (!existsSync(movie)) throw new Error(`Render it first: make video VIDEO=${video}`);
mkdirSync(dir, { recursive: true });

const shots: { name: string; at: number }[] = [];
const plan = planFor(video);
plan.scenes.forEach((scene, index) => {
  const number = String(index + 1).padStart(2, "0");
  if (scene.transitionLength > 0) {
    shots.push({
      name: `${number}-${scene.scene.id}-0-transition`,
      at: scene.start + scene.transitionLength / 2,
    });
  }
  scene.beats.forEach((beat, b) => {
    shots.push({
      name: `${number}-${scene.scene.id}-${String(b + 1)}-${beat.beat.id}`,
      at: beat.end - duration.quick,
    });
  });
});
for (const shot of shots) {
  const file = join(dir, `${shot.name}.png`);
  const result = spawnSync(ffmpeg(), [
    "-y",
    "-loglevel",
    "error",
    "-ss",
    shot.at.toFixed(3),
    "-i",
    movie,
    "-frames:v",
    "1",
    file,
  ]);
  if (result.status !== 0) throw new Error(`FFmpeg couldn't take ${shot.name}.`);
  console.log(`${shot.at.toFixed(2).padStart(6)} s  ${file}`);
}
