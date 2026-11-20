import motionCanvasModule from "@motion-canvas/vite-plugin";
import { defineConfig } from "vite";

// The plugin is CommonJS: its default export arrives wrapped under Node's ESM loader.
const motionCanvas =
  (motionCanvasModule as unknown as { default?: typeof motionCanvasModule }).default ??
  motionCanvasModule;

export default defineConfig({
  plugins: [
    motionCanvas({
      // Every video: src/<video>/project.ts.
      project: ["./src/*/project.ts"],
      // Frames land here while a video renders; tools/render.py encodes and removes them.
      output: process.env["IKI_VIDEO_FRAMES"] ?? "./frames",
    }),
  ],
  server: { host: "127.0.0.1", strictPort: true },
});
