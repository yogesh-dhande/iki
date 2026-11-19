// Checks every video without rendering it whole: each script against the schema and the
// rules, its captions against out/<video>.vtt, the JSON Schema against the Zod schema,
// and, unless --no-render, a still from every scene, rendered headless as a smoke test.
//   node tools/check.ts [--no-render] [--stills <dir>] [video…]
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { toVtt } from "../src/system/script/captions.ts";
import { scriptJsonSchema } from "../src/system/script/json-schema.ts";
import { ScriptError } from "../src/system/script/load.ts";
import type { Plan } from "../src/system/script/plan.ts";
import { duration } from "../src/system/theme/motion.ts";
import { frame } from "../src/system/theme/theme.ts";
import {
  inputsHash,
  launch,
  outDir,
  planFor,
  readVideo,
  renderPage,
  root,
  serve,
  videos,
} from "./lib.ts";

/** A still smaller than this is blank, or nearly: a scene that drew nothing. */
const BLANK_BYTES = 20_000;
/** A video may run this far from its target length before the check says so. */
const TARGET_SLACK = 0.15;

const args = process.argv.slice(2);
const render = !args.includes("--no-render");
const stillsAt = args.includes("--stills") ? args[args.indexOf("--stills") + 1] : undefined;
const named = args.filter((arg, i) => !arg.startsWith("--") && args[i - 1] !== "--stills");
const problems: string[] = [];
const notes: string[] = [];

if (readFileSync(join(root, "script.schema.json"), "utf8") !== scriptJsonSchema()) {
  problems.push("script.schema.json is out of date: run `pnpm --filter @ikisci/videos schema`.");
}

/** A published video's committed captions match its script, and its render is current. */
function checkRender(video: string, plan: Plan): void {
  const vtt = join(outDir, `${video}.vtt`);
  if (!existsSync(vtt) || readFileSync(vtt, "utf8") !== toVtt(plan)) {
    problems.push(
      `${video}: out/${video}.vtt doesn't match its script: run \`make video VIDEO=${video}\` (or node tools/captions.ts ${video}).`,
    );
  }
  const manifest = join(outDir, `${video}.json`);
  if (!existsSync(manifest)) {
    notes.push(`${video}: not rendered yet: make video VIDEO=${video}`);
  } else if (
    (JSON.parse(readFileSync(manifest, "utf8")) as { inputs: string }).inputs !== inputsHash(video)
  ) {
    notes.push(
      `${video}: its script or the design system changed since out/${video}.mp4 was rendered: make video VIDEO=${video}`,
    );
  }
}

const checked: { video: string; times: number[] }[] = [];
for (const video of named.length > 0 ? named : videos()) {
  try {
    const { target, specimen } = readVideo(video);
    const plan = planFor(video);
    if (Math.abs(plan.duration - target) > target * TARGET_SLACK) {
      notes.push(
        `${video}: runs ${plan.duration.toFixed(1)} s, against a target of ${String(target)} s.`,
      );
    }
    // A specimen is checked and its stills rendered, but never published: no render to check.
    if (!specimen) checkRender(video, plan);
    // A still near the end of every scene: every layout built, every step run up to it.
    checked.push({ video, times: plan.scenes.map((scene) => scene.end - duration.quick) });
    console.log(`${video}: ${String(plan.scenes.length)} scenes, ${plan.duration.toFixed(1)} s.`);
  } catch (error) {
    problems.push(error instanceof ScriptError ? error.message : `${video}: ${String(error)}`);
  }
}

if (render && checked.length > 0 && problems.length === 0) {
  const frames = mkdtempSync(join(tmpdir(), "iki-video-check-"));
  const { server, url } = await serve(frames);
  const browser = await launch();
  try {
    for (const { video, times } of checked) {
      await renderPage(
        browser,
        url,
        `video=${encodeURIComponent(video)}&stills=${times.map((t) => t.toFixed(3)).join(",")}`,
      );
      const dir = join(frames, "still", "project");
      const stills = existsSync(dir) ? readdirSync(dir).sort() : [];
      if (stills.length !== times.length) {
        problems.push(
          `${video}: rendered ${String(stills.length)} of ${String(times.length)} stills.`,
        );
      }
      for (const still of stills) {
        const file = join(dir, still);
        if (statSync(file).size < BLANK_BYTES)
          problems.push(`${video}: the still at frame ${still} is blank.`);
        if (stillsAt !== undefined) {
          mkdirSync(stillsAt, { recursive: true });
          renameSync(file, join(stillsAt, `${video}-${still}`));
        }
      }
      rmSync(dir, { recursive: true, force: true });
      console.log(
        `${video}: ${String(stills.length)} stills rendered at ${String(frame.width)}×${String(frame.height)}.`,
      );
    }
  } finally {
    await browser.close();
    await server.close();
    rmSync(frames, { recursive: true, force: true });
  }
}

for (const note of notes) console.log(`note: ${note}`);
if (problems.length > 0) {
  console.error(problems.map((problem) => `error: ${problem}`).join("\n"));
  process.exit(1);
}
console.log("Videos check out.");
