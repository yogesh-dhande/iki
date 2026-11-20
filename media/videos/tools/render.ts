// Renders a video, headless and repeatably, into out/:
//   <video>.mp4             without captions: the website's <video> and YouTube's upload
//   <video>.captioned.mp4   with the captions burned in, for social clips
//   <video>.vtt, .srt       the captions, for the website's <track> and YouTube's upload
//   <video>.thumbnail.png   1280×720, from the script's chosen beat
//   <video>.publish.json    title, description with chapters, tags; .md to read and paste
//   <video>.json            what the render was made from, so a stale render shows
//   node tools/render.ts entities        (or: make video VIDEO=entities)
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { outputFiles } from "../src/system/script/publish.ts";
import { duration } from "../src/system/theme/motion.ts";
import { frame } from "../src/system/theme/theme.ts";
import {
  ffmpeg,
  inputsHash,
  launch,
  outDir,
  planFor,
  readVideo,
  renderPage,
  serve,
  textOutputs,
} from "./lib.ts";

/** YouTube's thumbnail size. */
const THUMBNAIL = { width: 1280, height: 720 };

const video = process.argv[2];
if (!video) throw new Error("Which video? node tools/render.ts <video>");
const script = readVideo(video);
if (script.specimen) throw new Error(`${video} is a specimen: it's never rendered whole.`);
const plan = planFor(video);
const files = outputFiles(video);
const work = mkdtempSync(join(tmpdir(), `iki-video-${video}-`));

function run(command: string, args: string[]): void {
  const result = spawnSync(command, args, { stdio: "inherit" });
  if (result.status !== 0) throw new Error(`${command} failed (${String(result.status)}).`);
}

/** Seconds between keyframes: the usual for streaming on the web (README: Encoding). */
const KEYFRAME_SECONDS = 2;

/**
 * Encodes PNG frames as an MP4 that suits both YouTube's upload and streaming on the web:
 * H.264 High profile, 4:2:0, BT.709, closed GOPs with two B-frames, and the index at the
 * front (+faststart) so it plays while it loads. `audio`, when there's a voice-over, is
 * muxed as AAC-LC, 48 kHz stereo, as YouTube recommends.
 */
function encode(pngs: string, output: string, audio?: string): void {
  run(
    ffmpeg(),
    [
      ["-y", "-loglevel", "error"],
      ["-framerate", String(frame.fps), "-i", join(pngs, "%06d.png")],
      audio ? ["-i", audio] : [],
      ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-tune", "animation"],
      ["-profile:v", "high", "-g", String(frame.fps * KEYFRAME_SECONDS), "-bf", "2"],
      ["-flags", "+cgop"],
      ["-vf", "scale=out_color_matrix=bt709:out_range=tv", "-pix_fmt", "yuv420p"],
      ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"],
      audio ? ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-shortest"] : ["-an"],
      // No timestamps or encoder strings, so the same frames make the same file.
      ["-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-threads", "1"],
      ["-movflags", "+faststart", output],
    ].flat(),
  );
}

/** Renders every frame into work/<name>/, with captions burned in or not. */
async function renderFrames(name: string, captions: boolean): Promise<string> {
  const frames = join(work, name);
  const { server, url } = await serve(frames);
  const browser = await launch();
  try {
    const query = `video=${encodeURIComponent(video ?? "")}${captions ? "" : "&captions=off"}`;
    await renderPage(browser, url, query);
  } finally {
    await browser.close();
    await server.close();
  }
  return join(frames, video ?? "");
}

console.log(
  `Rendering ${video}: ${plan.duration.toFixed(1)} s, ${String(Math.ceil(plan.duration * frame.fps))} frames, twice.`,
);
try {
  mkdirSync(outDir, { recursive: true });

  console.log("Without captions:");
  const clean = await renderFrames("clean", false);
  console.log(`Encoding ${String(readdirSync(clean).length)} frames.`);
  encode(clean, join(outDir, files.video));

  // The thumbnail: the chosen beat once its steps are done, from the clean frames.
  const beats = plan.scenes.flatMap((scene) => scene.beats);
  const chosen = beats.find((beat) => beat.beat.id === script.publish?.thumbnail) ?? beats[0];
  if (!chosen) throw new Error(`${video} has no beats.`);
  const still = Math.round((chosen.end - duration.quick) * frame.fps);
  run(ffmpeg(), [
    ...["-y", "-loglevel", "error", "-i", join(clean, `${String(still).padStart(6, "0")}.png`)],
    ...["-vf", `scale=${String(THUMBNAIL.width)}:${String(THUMBNAIL.height)}:flags=lanczos`],
    ...["-map_metadata", "-1", "-fflags", "+bitexact", join(outDir, files.thumbnail)],
  ]);

  console.log("With captions:");
  const captioned = await renderFrames("captioned", true);
  console.log(`Encoding ${String(readdirSync(captioned).length)} frames.`);
  encode(captioned, join(outDir, files.captioned));

  for (const [name, text] of Object.entries(textOutputs(video))) {
    writeFileSync(join(outDir, name), text);
  }
  const manifest = {
    video,
    duration: Number(plan.duration.toFixed(3)),
    fps: frame.fps,
    size: [frame.width, frame.height],
    inputs: inputsHash(video),
  };
  writeFileSync(join(outDir, `${video}.json`), `${JSON.stringify(manifest, null, 2)}\n`);
  console.log(
    `Wrote out/${video}.* (${Object.values(files).join(", ")}, the publishing files and the manifest).`,
  );
} finally {
  rmSync(work, { recursive: true, force: true });
}
