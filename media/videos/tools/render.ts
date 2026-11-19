// Renders a video, headless and repeatably: out/<video>.mp4, its captions out/<video>.vtt,
// and out/<video>.json, which says what it was rendered from.
//   node tools/render.ts entities        (or: make video VIDEO=entities)
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { toVtt } from "../src/system/script/captions.ts";
import { frame } from "../src/system/theme/theme.ts";
import { ffmpeg, inputsHash, launch, outDir, planFor, renderPage, serve } from "./lib.ts";

const video = process.argv[2];
if (!video) throw new Error("Which video? node tools/render.ts <video>");
const plan = planFor(video);
const frames = mkdtempSync(join(tmpdir(), `iki-video-${video}-`));
console.log(
  `Rendering ${video}: ${plan.duration.toFixed(1)} s, ${String(Math.ceil(plan.duration * frame.fps))} frames.`,
);

try {
  const { server, url } = await serve(frames);
  const browser = await launch();
  try {
    await renderPage(browser, url, `video=${encodeURIComponent(video)}`);
  } finally {
    await browser.close();
    await server.close();
  }
  const pngs = join(frames, video);
  console.log(`Encoding ${String(readdirSync(pngs).length)} frames.`);
  mkdirSync(outDir, { recursive: true });
  const encoded = spawnSync(
    ffmpeg(),
    [
      ["-y", "-loglevel", "error"],
      ["-framerate", String(frame.fps), "-i", join(pngs, "%06d.png")],
      // H.264 for every player, tuned for flat graphics, in BT.709 as HD video expects.
      ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-tune", "animation"],
      ["-vf", "scale=out_color_matrix=bt709:out_range=tv", "-pix_fmt", "yuv420p"],
      ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"],
      // No timestamps or encoder strings, so the same frames make the same file.
      ["-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-threads", "1"],
      ["-movflags", "+faststart", join(outDir, `${video}.mp4`)],
    ].flat(),
    { stdio: "inherit" },
  );
  if (encoded.status !== 0) throw new Error(`FFmpeg failed (${String(encoded.status)}).`);
  writeFileSync(join(outDir, `${video}.vtt`), toVtt(plan));
  writeFileSync(
    join(outDir, `${video}.json`),
    `${JSON.stringify({ video, duration: Number(plan.duration.toFixed(3)), fps: frame.fps, size: [frame.width, frame.height], inputs: inputsHash(video) }, null, 2)}\n`,
  );
  console.log(`Wrote out/${video}.mp4, out/${video}.vtt and out/${video}.json.`);
} finally {
  rmSync(frames, { recursive: true, force: true });
}
