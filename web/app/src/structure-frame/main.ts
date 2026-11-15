/**
 * Mol*, in a frame of Iki's own: what the structure viewer runs (ADR 0085).
 *
 * The page hands it one port, then the structure's bytes, their format and, for a saved
 * view (ADR 0086), where the camera was. It answers `{ get: "camera" }` with where the
 * camera is now, so the page can save it. The frame has no network and no origin that's
 * anyone's, so a file that fooled Mol*'s parsers could only draw something wrong, never
 * read Iki's pages or act as the person.
 */

import type { Camera as MolCamera } from "molstar/lib/mol-canvas3d/camera.js";
import { Vec3 } from "molstar/lib/mol-math/linear-algebra.js";
import type { PluginContext } from "molstar/lib/mol-plugin/context.js";
import { PluginConfig } from "molstar/lib/mol-plugin/config.js";
import { PluginSpec } from "molstar/lib/mol-plugin/spec.js";
import { createPluginUI } from "molstar/lib/mol-plugin-ui/index.js";
import { renderReact18 } from "molstar/lib/mol-plugin-ui/react18.js";
import { DefaultPluginUISpec } from "molstar/lib/mol-plugin-ui/spec.js";
import { Color } from "molstar/lib/mol-util/color/index.js";
import dark from "molstar/build/viewer/theme/dark.css?inline";
import light from "molstar/build/viewer/theme/light.css?inline";

type Format = "mmcif" | "pdb";

/** Where the camera is, as a view saves it (core.View's state). */
interface Camera {
  position: number[];
  target: number[];
  up: number[];
  fov?: number;
  radius?: number;
  mode?: "perspective" | "orthographic";
}

interface Show {
  structure: ArrayBuffer;
  format: Format;
  label: string;
  camera?: Camera;
}

let shown: PluginContext | null = null;

function round(values: ArrayLike<number>): number[] {
  return Array.from(values, (v) => Math.round(v * 1000) / 1000);
}

function cameraNow(plugin: PluginContext): Camera | null {
  const camera = plugin.canvas3d?.camera.getSnapshot();
  if (!camera) return null;
  return {
    position: round(camera.position),
    target: round(camera.target),
    up: round(camera.up),
    fov: Math.round(camera.fov * 10000) / 10000,
    radius: Math.round(camera.radius * 1000) / 1000,
    mode: camera.mode,
  };
}

function toMolCamera(camera: Camera): Partial<MolCamera.Snapshot> {
  const vec = (v: number[]) => Vec3.create(v[0] ?? 0, v[1] ?? 0, v[2] ?? 0);
  return {
    position: vec(camera.position),
    target: vec(camera.target),
    up: vec(camera.up),
    ...(camera.fov !== undefined ? { fov: camera.fov } : {}),
    ...(camera.radius !== undefined ? { radius: camera.radius } : {}),
    ...(camera.mode !== undefined ? { mode: camera.mode } : {}),
  };
}

const scheme = window.matchMedia("(prefers-color-scheme: dark)");

// Iki's pages deny WebXR (Permissions-Policy), and Mol* asks whether it's there as it
// starts, which browsers report as a violation. There's nothing to ask: say it isn't.
Object.defineProperty(navigator, "xr", { value: undefined, configurable: true });

/** Whether this browser draws 3D: not where WebGL is off, as on many virtual desktops. */
function webgl(): boolean {
  const canvas = document.createElement("canvas");
  return Boolean(canvas.getContext("webgl2") ?? canvas.getContext("webgl"));
}

function themed(): void {
  const style = document.getElementById("molstar-theme") ?? document.createElement("style");
  style.id = "molstar-theme";
  style.textContent = scheme.matches ? dark : light;
  document.head.append(style);
}

async function show(target: HTMLElement, message: Show) {
  const base = DefaultPluginUISpec();
  const plugin = await createPluginUI({
    target,
    render: renderReact18,
    spec: {
      ...base,
      // Nothing to fetch from anywhere: the frame has no network.
      actions: [],
      layout: {
        initial: {
          isExpanded: false,
          showControls: true,
          controlsDisplay: "reactive",
          regionState: { left: "collapsed", top: "full", right: "full", bottom: "hidden" },
        },
      },
      components: { ...base.components, remoteState: "none" },
      config: [
        [PluginConfig.Viewport.ShowExpand, false],
        [PluginConfig.Viewport.ShowToggleFullscreen, false],
        [PluginConfig.Viewport.ShowXR, "never"],
        [PluginConfig.VolumeStreaming.Enabled, false],
      ],
      behaviors: base.behaviors.filter(
        (b: PluginSpec.Behavior) => !b.transformer.id.includes("volume-streaming"),
      ),
    },
  });
  const background = () =>
    plugin.canvas3d?.setProps({
      renderer: { backgroundColor: Color(scheme.matches ? 0x14161a : 0xffffff) },
    });
  background();
  scheme.addEventListener("change", background);
  const data = await plugin.builders.data.rawData({
    data: new TextDecoder().decode(message.structure),
    label: message.label,
  });
  const trajectory = await plugin.builders.structure.parseTrajectory(data, message.format);
  await plugin.builders.structure.hierarchy.applyPreset(trajectory, "default");
  if (message.camera) {
    plugin.canvas3d?.requestCameraReset({ snapshot: toMolCamera(message.camera), durationMs: 0 });
  }
  shown = plugin;
}

themed();
scheme.addEventListener("change", themed);

function accept(event: MessageEvent): void {
  // Only Iki may frame this page (frame-ancestors), so the parent's port is Iki's. Mol*
  // posts messages to its own window too (to schedule work), so wait for that one.
  const port = event.ports[0];
  if (event.source !== window.parent || !port) return;
  window.removeEventListener("message", accept);
  let started = false;
  port.onmessage = (message: MessageEvent<Show | { get: "camera" }>) => {
    if ("get" in message.data) {
      port.postMessage({ camera: shown ? cameraNow(shown) : null });
      return;
    }
    const target = document.getElementById("structure");
    if (!target || started) return;
    started = true;
    if (!webgl()) {
      port.postMessage({ unsupported: "webgl" });
      return;
    }
    show(target, message.data).then(
      () => {
        port.postMessage({ shown: true });
      },
      (error: unknown) => {
        port.postMessage({ failed: error instanceof Error ? error.message : String(error) });
      },
    );
  };
}

window.addEventListener("message", accept);
