// Checks every video without rendering it whole: each script against the schema and the
// rules, the product's words it quotes against the product, its committed captions and
// publishing files against the script, its render against what it was made from (a stale
// render fails), the JSON Schema against the Zod schema, and, unless --no-render, a still
// from every scene, rendered headless as a smoke test.
//   node tools/check.ts [--no-render] [--stills <dir>] [video…]
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  copyFileSync,
  rmSync,
  statSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chapterProblems } from "../src/system/script/publish.ts";
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
  textOutputs,
  videos,
} from "./lib.ts";
import { staleQuotes } from "./quotes.ts";

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

/**
 * A published video's committed outputs are current: its captions and publishing files
 * match its script, and its render was made from what's here now.
 */
function checkRender(video: string, plan: Plan): void {
  const regenerate = `run \`make video VIDEO=${video}\` to render it again`;
  for (const [name, text] of Object.entries(textOutputs(video))) {
    const file = join(outDir, name);
    if (!existsSync(file) || readFileSync(file, "utf8") !== text) {
      problems.push(`${video}: out/${name} doesn't match its script: ${regenerate}.`);
    }
  }
  const manifest = join(outDir, `${video}.json`);
  if (!existsSync(manifest)) {
    problems.push(`${video}: never rendered: ${regenerate}.`);
  } else if (
    (JSON.parse(readFileSync(manifest, "utf8")) as { inputs: string }).inputs !== inputsHash(video)
  ) {
    problems.push(
      `${video}: out/${video}.mp4 is stale: its script, the design system, the fonts or Motion Canvas changed since it was rendered. ${regenerate[0]?.toUpperCase() ?? ""}${regenerate.slice(1)}, review its frames, and commit the render with the change.`,
    );
  }
  problems.push(...chapterProblems(plan).map((problem) => `${video}: ${problem}`));
}

const checked: { video: string; times: number[] }[] = [];
for (const video of named.length > 0 ? named : videos()) {
  try {
    const script = readVideo(video);
    const plan = planFor(video);
    if (Math.abs(plan.duration - script.target) > script.target * TARGET_SLACK) {
      notes.push(
        `${video}: runs ${plan.duration.toFixed(1)} s, against a target of ${String(script.target)} s.`,
      );
    }
    problems.push(...staleQuotes(script));
    // A specimen is checked and its stills rendered, but never published: no render to check.
    if (!script.specimen) checkRender(video, plan);
    // A still as each scene's last line ends: every layout built, every step run, and the
    // caption still up.
    checked.push({
      video,
      times: plan.scenes.map((scene) => (scene.beats.at(-1)?.end ?? scene.end) - duration.quick),
    });
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
          copyFileSync(file, join(stillsAt, `${video}-${still}`));
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
