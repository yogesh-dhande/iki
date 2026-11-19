import { makeScene2D } from "@motion-canvas/2d";
import { loadScript } from "../system/script/load.ts";
import { playVideo } from "../system/runtime/video.tsx";
import { voiceFor } from "../system/runtime/voice.ts";
import { custom } from "./custom.ts";
import source from "./script.yaml?raw";

const video = loadScript(source, "src/specimen/script.yaml");

export default makeScene2D(function* (view) {
  yield* playVideo(view, video, custom, voiceFor(video.id));
});
