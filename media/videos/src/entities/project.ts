import { makeProject } from "@motion-canvas/core";
// First, so the bundled fonts are loaded before anything is drawn.
import "../system/render/fonts.ts";
import scene from "./scene?scene";

export default makeProject({ scenes: [scene] });
