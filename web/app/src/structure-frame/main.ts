/**
 * Mol*, in a frame of Iki's own: what the structure viewer runs (ADR 0085).
 *
 * The page hands it one port, then the structure's bytes and their format. The frame
 * has no network and no origin that's anyone's, so a file that fooled Mol*'s parsers
 * could only draw something wrong, never read Iki's pages or act as the person.
 */

import { PluginConfig } from "molstar/lib/mol-plugin/config.js";
import { PluginSpec } from "molstar/lib/mol-plugin/spec.js";
import { createPluginUI } from "molstar/lib/mol-plugin-ui/index.js";
import { renderReact18 } from "molstar/lib/mol-plugin-ui/react18.js";
import { DefaultPluginUISpec } from "molstar/lib/mol-plugin-ui/spec.js";
import { Color } from "molstar/lib/mol-util/color/index.js";
import dark from "molstar/build/viewer/theme/dark.css?inline";
import light from "molstar/build/viewer/theme/light.css?inline";

type Format = "mmcif" | "pdb";

interface Show {
  structure: ArrayBuffer;
  format: Format;
  label: string;
}

const scheme = window.matchMedia("(prefers-color-scheme: dark)");

// Iki's pages deny WebXR (Permissions-Policy), and Mol* asks whether it's there as it
// starts, which browsers report as a violation. There's nothing to ask: say it isn't.
Object.defineProperty(navigator, "xr", { value: undefined, configurable: true });

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
}

themed();
scheme.addEventListener("change", themed);

function accept(event: MessageEvent): void {
  // Only Iki may frame this page (frame-ancestors), so the parent's port is Iki's. Mol*
  // posts messages to its own window too (to schedule work), so wait for that one.
  const port = event.ports[0];
  if (event.source !== window.parent || !port) return;
  window.removeEventListener("message", accept);
  port.onmessage = (message: MessageEvent<Show>) => {
    const target = document.getElementById("structure");
    if (!target) return;
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
