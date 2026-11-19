// What the tools share: finding videos, reading their scripts, serving them and driving
// headless Chromium through them.
import { createHash } from "node:crypto";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { chromium, type Browser } from "playwright-core";
import { createServer, type ViteDevServer } from "vite";
import { loadScript } from "../src/system/script/load.ts";
import type { Plan } from "../src/system/script/plan.ts";
import { planVideo, type Voice } from "../src/system/script/plan.ts";
import type { Video } from "../src/system/script/schema.ts";

export const root = join(import.meta.dirname, "..");
export const outDir = join(root, "out");

/** Every video: each folder in src/ with a script.yaml. */
export function videos(): string[] {
  return readdirSync(join(root, "src"))
    .filter((name) => existsSync(join(root, "src", name, "script.yaml")))
    .sort();
}

export function scriptFile(video: string): string {
  return join(root, "src", video, "script.yaml");
}

export function readVideo(video: string): Video {
  const file = scriptFile(video);
  if (!existsSync(file)) throw new Error(`No video "${video}": expected ${relative(root, file)}.`);
  return loadScript(readFileSync(file, "utf8"), relative(root, file));
}

export function readVoice(video: string): Voice | undefined {
  const file = join(root, "src", video, "voice.json");
  return existsSync(file) ? (JSON.parse(readFileSync(file, "utf8")) as Voice) : undefined;
}

export function planFor(video: string): Plan {
  return planVideo(readVideo(video), readVoice(video));
}

function filesUnder(dir: string): string[] {
  return readdirSync(dir, { recursive: true, encoding: "utf8" })
    .map((name) => join(dir, name))
    .filter((path) => statSync(path).isFile() && !path.endsWith(".meta"))
    .sort();
}

/**
 * A hash of everything a video's render depends on: its folder, the design system, and
 * the pinned packages. The render's manifest keeps it, so a stale render shows.
 */
export function inputsHash(video: string): string {
  const hash = createHash("sha256");
  const files = [
    ...filesUnder(join(root, "src", video)),
    ...filesUnder(join(root, "src", "system")),
    join(root, "package.json"),
  ];
  for (const file of files) {
    hash.update(relative(root, file));
    hash.update(readFileSync(file));
  }
  return hash.digest("hex");
}

/** Serves the package with Vite, writing exported frames under `frames`. */
export async function serve(frames: string): Promise<{ server: ViteDevServer; url: string }> {
  process.env["IKI_VIDEO_FRAMES"] = frames;
  const server = await createServer({
    root,
    configFile: join(root, "vite.config.ts"),
    logLevel: "warn",
    server: { port: 0 },
  });
  await server.listen();
  const url = server.resolvedUrls?.local[0];
  if (!url) throw new Error("Vite didn't say where it's serving.");
  return { server, url };
}

export async function launch(): Promise<Browser> {
  try {
    return await chromium.launch();
  } catch (error) {
    throw new Error(
      `Couldn't start headless Chromium (${String(error)}). Install it with:\n` +
        "  pnpm --filter @ikisci/videos exec playwright-core install chromium-headless-shell",
      { cause: error },
    );
  }
}

const PROGRESS_EVERY = 300;

/** Opens render.html for a video, waits for it to finish, and fails on any error. */
export async function renderPage(browser: Browser, url: string, query: string): Promise<void> {
  const page = await browser.newPage();
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.goto(`${url}render.html?${query}`);
  let last = -1;
  for (;;) {
    const status = await page.evaluate(() => window.ikiRender);
    if (status && status.state !== "rendering") {
      errors.push(...status.errors);
      break;
    }
    if (status && status.frame - last >= PROGRESS_EVERY) {
      last = status.frame;
      process.stdout.write(`  frame ${String(status.frame)}\n`);
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  await page.close();
  if (errors.length > 0) throw new Error(`The render failed:\n${errors.join("\n")}`);
}

export function ffmpeg(): string {
  return process.env["FFMPEG"] ?? "ffmpeg";
}
