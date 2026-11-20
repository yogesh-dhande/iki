// The page tools/render.ts opens in headless Chromium: it loads a video's Motion Canvas
// project and renders it, all of it into PNG frames or a few stills, through Motion
// Canvas's own renderer and image-sequence exporter. No editor, no clicks.
//   /render.html?video=entities                 every frame
//   /render.html?video=entities&stills=1.5,30   one frame at each of these seconds
import { LogLevel, Renderer, RendererResult, Vector2, type Project } from "@motion-canvas/core";
import type { RendererSettings } from "@motion-canvas/core";
import { color, frame } from "../theme/theme.ts";

const projects = import.meta.glob<Project>("../../*/project.ts", {
  query: "?project",
  import: "default",
});

export interface RenderStatus {
  state: "rendering" | "done" | "failed";
  frame: number;
  errors: string[];
}

declare global {
  interface Window {
    ikiRender?: RenderStatus;
  }
}

const status: RenderStatus = { state: "rendering", frame: 0, errors: [] };
window.ikiRender = status;

async function main(): Promise<void> {
  const params = new URLSearchParams(location.search);
  const video = params.get("video") ?? "";
  const load = projects[`../../${video}/project.ts`];
  if (!load) throw new Error(`No video "${video}": expected src/${video}/project.ts.`);
  const project = await load();
  project.logger.onLogged.subscribe((entry) => {
    if (entry.level === LogLevel.Error) status.errors.push(entry.message);
  });
  const renderer = new Renderer(project);
  renderer.onFrameChanged.subscribe((current) => (status.frame = current));
  const settings: RendererSettings = {
    name: video,
    range: [0, Infinity],
    fps: frame.fps,
    size: new Vector2(frame.width, frame.height),
    resolutionScale: 1,
    colorSpace: "srgb",
    background: color.canvas,
    exporter: {
      name: "@motion-canvas/core/image-sequence",
      options: { fileType: "image/png", quality: 100, groupByScene: false },
    },
  };
  const stills = params.get("stills");
  if (stills !== null) {
    for (const time of stills.split(",").map(Number)) await renderer.renderFrame(settings, time);
    status.state = status.errors.length > 0 ? "failed" : "done";
    return;
  }
  const result = await new Promise<RendererResult>((resolve) => {
    renderer.onFinished.subscribe(resolve);
    void renderer.render(settings);
  });
  status.state =
    result === RendererResult.Success && status.errors.length === 0 ? "done" : "failed";
}

main().catch((error: unknown) => {
  status.errors.push(error instanceof Error ? (error.stack ?? error.message) : String(error));
  status.state = "failed";
});
