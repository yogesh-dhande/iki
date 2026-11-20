// A video's scene, from its script: every video's scene.tsx is this one call.
import { makeScene2D } from "@motion-canvas/2d";
import { loadScript } from "../script/load.ts";
import { playVideo, type CustomSteps } from "./video.tsx";
import { voiceFor } from "./voice.ts";

/** Whether this render burns captions in: render.html?captions=off for the clean render. */
function burnInCaptions(): boolean {
  return new URLSearchParams(location.search).get("captions") !== "off";
}

export function videoScene(source: string, file: string, custom: CustomSteps = {}) {
  const video = loadScript(source, file);
  return makeScene2D(function* (view) {
    yield* playVideo(view, video, {
      custom,
      voice: voiceFor(video.id),
      captions: burnInCaptions(),
    });
  });
}
