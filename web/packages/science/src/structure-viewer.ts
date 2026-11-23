/// <reference types="vite/client" />
/** Interactive, independent Mol* scenes from in-memory PDB or mmCIF coordinates. */
import type { Camera as MolCamera } from "molstar/lib/mol-canvas3d/camera.js";
import { OrderedSet } from "molstar/lib/mol-data/int.js";
import { Mat4, Vec3 } from "molstar/lib/mol-math/linear-algebra.js";
import {
  QueryContext,
  StructureElement,
  StructureSelection,
  type Structure,
} from "molstar/lib/mol-model/structure.js";
import type { StructureRef } from "molstar/lib/mol-plugin-state/manager/structure/hierarchy-state.js";
import { StructureSelectionQueries } from "molstar/lib/mol-plugin-state/helpers/structure-selection-query.js";
import { StateTransforms } from "molstar/lib/mol-plugin-state/transforms.js";
import { getTransformFromParams } from "molstar/lib/mol-plugin-state/transforms/helpers.js";
import type { PluginContext } from "molstar/lib/mol-plugin/context.js";
import { PluginConfig } from "molstar/lib/mol-plugin/config.js";
import { PluginSpec } from "molstar/lib/mol-plugin/spec.js";
import { createPluginUI } from "molstar/lib/mol-plugin-ui/index.js";
import { createRoot } from "react-dom/client";
import type { ReactNode } from "react";
import { DefaultPluginUISpec } from "molstar/lib/mol-plugin-ui/spec.js";
import { MolScriptBuilder as MS } from "molstar/lib/mol-script/language/builder.js";
import { Script } from "molstar/lib/mol-script/script.js";
import { Color } from "molstar/lib/mol-util/color/index.js";
import dark from "molstar/build/viewer/theme/dark.css?inline";
import light from "molstar/build/viewer/theme/light.css?inline";
import { fit as fitted, MAX_SPANS, spansOf, spanTest } from "./structure/residues.js";

/** Residues of a chain, by its authors' numbering, from `start` to `end`, each with its
 * insertion code if it has one. */
export interface Span {
  chain: string;
  start: number;
  end: number;
  start_insertion?: string;
  end_insertion?: string;
}

export interface StructureCamera {
  position: number[];
  target: number[];
  up: number[];
  fov?: number;
  radius?: number;
  mode?: "perspective" | "orthographic";
}

export interface StructureInput {
  structure: string | ArrayBuffer;
  format: "mmcif" | "pdb";
  label: string;
  colour?: string;
  hidden?: boolean;
  transform?: number[];
  selection?: Span[];
}

export interface StructureScene {
  structures: StructureInput[];
  camera?: StructureCamera;
}

function round(values: ArrayLike<number>, places = 3): number[] {
  const scale = 10 ** places;
  return Array.from(values, (v) => Math.round(v * scale) / scale);
}

function cameraNow(plugin: PluginContext): StructureCamera | null {
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

function toMolCamera(camera: StructureCamera): Partial<MolCamera.Snapshot> {
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

/** Whether this browser draws 3D: not where WebGL is off, as on many virtual desktops. */
function webgl(): boolean {
  const canvas = document.createElement("canvas");
  return Boolean(canvas.getContext("webgl2") ?? canvas.getContext("webgl"));
}

/** The structure at `index`, as Mol*'s hierarchy has it now. */
function structureAt(
  plugin: PluginContext,
  roots: string[],
  index: number,
): StructureRef | undefined {
  const ref = roots[index];
  return plugin.managers.structure.hierarchy.current.structures.find(
    (s) => s.cell.transform.ref === ref,
  );
}

/** The transform on a structure, if it has one: a matrix, whichever way it was given. */
function transformOf(plugin: PluginContext, structure: StructureRef): Mat4 | null {
  const [cell] = plugin.state.data.selectQ((q) =>
    q
      .byRef(structure.cell.transform.ref)
      .subtree()
      .withTransformer(StateTransforms.Model.TransformStructureConformation),
  );
  const data = structure.cell.obj?.data;
  if (!cell || !data) return null;
  const params = cell.transform.params as {
    transform: Parameters<typeof getTransformFromParams>[0];
  };
  return getTransformFromParams(params.transform, data.boundary.sphere.center);
}

/** Moves a structure by `matrix`, as Mol*'s own superposition does. */
async function move(plugin: PluginContext, structure: StructureRef, matrix: Mat4): Promise<void> {
  const ref = structure.cell.transform.ref;
  const [cell] = plugin.state.data.selectQ((q) =>
    q.byRef(ref).subtree().withTransformer(StateTransforms.Model.TransformStructureConformation),
  );
  const params = {
    transform: { name: "matrix" as const, params: { data: matrix, transpose: false } },
  };
  const build = cell
    ? plugin.state.data.build().to(cell).update(params)
    : plugin.state.data
        .build()
        .to(ref)
        .insert(StateTransforms.Model.TransformStructureConformation, params);
  await plugin.runTask(plugin.state.data.updateTree(build));
}

async function colour(plugin: PluginContext, structure: StructureRef, value: string | null) {
  const components = structure.components;
  await (value
    ? plugin.managers.structure.component.updateRepresentationsTheme(components, {
        color: "uniform",
        colorParams: { value: Color(parseInt(value.slice(1), 16)) },
      })
    : plugin.managers.structure.component.updateRepresentationsTheme(components, {
        color: "default",
      }));
}

/** Residues selected in a structure, as ranges of each chain's authors' numbering, with
 * their insertion codes (see ./residues). */
function selectionOf(plugin: PluginContext, data: Structure): Span[] {
  const loci = plugin.managers.structure.selection.getLoci(data);
  if (!StructureElement.Loci.is(loci) || StructureElement.Loci.isEmpty(loci)) return [];
  return spansOf(loci);
}

function select(plugin: PluginContext, data: Structure, spans: Span[]): void {
  if (!spans.length) return;
  const groups = spans.slice(0, MAX_SPANS).map(spanTest);
  const selection = Script.getStructureSelection(MS.struct.combinator.merge(groups), data);
  plugin.managers.structure.selection.fromLoci(
    "add",
    StructureSelection.toLociWithSourceUnits(selection),
  );
}

/** Each structure's first polymer chain, by its residues' α carbons (or phosphates). */
function chainOf(data: Structure): StructureElement.Loci | null {
  const loci = StructureSelection.toLociWithSourceUnits(
    StructureSelectionQueries.trace.query(new QueryContext(data)),
  );
  const first = loci.elements[0];
  return first && OrderedSet.size(first.indices) >= 3
    ? StructureElement.Loci(loci.structure, [first])
    : null;
}

export type SuperpositionResult =
  { index: number; rmsd: number; residues: number } | { index: number; failed: string };

/** Superposes each other shown structure on the one at `onto`, by their first chains,
 * paired by aligning their sequences, as Mol*'s own superposition does, over as many
 * residues as were paired. */
async function superpose(
  plugin: PluginContext,
  roots: string[],
  onto: number,
): Promise<SuperpositionResult[]> {
  const pivot = structureAt(plugin, roots, onto);
  const pivotData = pivot?.cell.obj?.data;
  const reference = pivotData ? chainOf(pivotData) : null;
  const results: SuperpositionResult[] = [];
  if (!pivot || !reference) {
    return roots.flatMap((_, index) =>
      index === onto ? [] : [{ index, failed: "the structure it's superposed on has no chain" }],
    );
  }
  // Where the pivot was moved to itself, so the others land where it's drawn.
  const placed = transformOf(plugin, pivot) ?? Mat4.identity();
  for (let index = 0; index < roots.length; index++) {
    if (index === onto) continue;
    const structure = structureAt(plugin, roots, index);
    const data = structure?.cell.obj?.data;
    const chain = data ? chainOf(data) : null;
    if (!structure || !chain) {
      results.push({ index, failed: "it has no chain to superpose by" });
      continue;
    }
    const fit = fitted(reference, chain);
    if (!fit || !Number.isFinite(fit.rmsd)) {
      results.push({ index, failed: "its sequence doesn't align with the other's" });
      continue;
    }
    await move(plugin, structure, Mat4.mul(Mat4(), placed, fit.bTransform));
    results.push({ index, rmsd: Math.round(fit.rmsd * 100) / 100, residues: fit.residues });
  }
  plugin.managers.camera.reset();
  return results;
}

export interface StructureState {
  transform?: number[];
  hidden?: boolean;
  selection?: Span[];
}

export interface SceneState {
  camera: StructureCamera | null;
  structures: StructureState[];
}

function stateNow(plugin: PluginContext, roots: string[]): SceneState {
  return {
    camera: cameraNow(plugin),
    structures: roots.map((_, index) => {
      const structure = structureAt(plugin, roots, index);
      if (!structure) return {};
      const matrix = transformOf(plugin, structure);
      const data = structure.cell.obj?.data;
      const selection = data ? selectionOf(plugin, data) : [];
      return {
        ...(matrix && !Mat4.isIdentity(matrix) ? { transform: round(matrix, 6) } : {}),
        ...(structure.cell.state.isHidden ? { hidden: true } : {}),
        ...(selection.length ? { selection } : {}),
      };
    }),
  };
}

async function createPlugin(target: HTMLElement, render: (component: ReactNode) => void) {
  const base = DefaultPluginUISpec();
  return createPluginUI({
    target,
    render,
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
}

async function show(plugin: PluginContext, message: StructureScene, roots: string[]) {
  for (const given of message.structures) {
    const data = await plugin.builders.data.rawData({
      data:
        typeof given.structure === "string"
          ? given.structure
          : new TextDecoder().decode(given.structure),
      label: given.label,
    });
    const trajectory = await plugin.builders.structure.parseTrajectory(data, given.format);
    const made = await plugin.builders.structure.hierarchy.applyPreset(trajectory, "default");
    roots.push(made?.structure.ref ?? "");
  }
  for (const [index, given] of message.structures.entries()) {
    const structure = structureAt(plugin, roots, index);
    if (!structure) continue;
    if (given.transform) await move(plugin, structure, Mat4.fromArray(Mat4(), given.transform, 0));
    if (given.colour) await colour(plugin, structure, given.colour);
    const data = structureAt(plugin, roots, index)?.cell.obj?.data;
    if (data && given.selection) select(plugin, data, given.selection);
    // Hidden through the hierarchy, as Mol*'s UI does: importing its state behaviour here
    // would load Mol*'s modules out of order.
    if (given.hidden) plugin.managers.structure.hierarchy.toggleVisibility([structure], "hide");
  }
  if (message.camera) {
    plugin.canvas3d?.requestCameraReset({ snapshot: toMolCamera(message.camera), durationMs: 0 });
  } else if (message.structures.some((s) => s.transform)) {
    plugin.managers.camera.reset();
  }
}

/** A new scene replaces the previous scene, including its camera and residue selections. */
export class StructureViewer extends HTMLElement {
  #data: StructureScene | null = null;
  #scene: { plugin: PluginContext; roots: string[] } | null = null;
  #dispose: (() => void) | null = null;
  #generation = 0;
  readonly #root = this.attachShadow({ mode: "open" });

  get data(): StructureScene | null {
    return this.#data;
  }

  set data(scene: StructureScene | null) {
    this.#data = scene;
    if (this.isConnected) void this.#render();
  }

  connectedCallback(): void {
    void this.#render();
  }

  disconnectedCallback(): void {
    ++this.#generation;
    this.#dispose?.();
    this.#dispose = null;
    this.#scene = null;
  }

  getState(): SceneState | null {
    return this.#scene && stateNow(this.#scene.plugin, this.#scene.roots);
  }

  async setColour(index: number, value: string | null): Promise<void> {
    const scene = this.#scene;
    if (!scene) return;
    if (value !== null && !/^#[0-9a-f]{6}$/i.test(value))
      throw new TypeError("Expected #RRGGBB or null");
    const target = structureAt(scene.plugin, scene.roots, index);
    if (target) await colour(scene.plugin, target, value);
  }

  setHidden(index: number, hidden: boolean): void {
    const scene = this.#scene;
    if (!scene) return;
    const target = structureAt(scene.plugin, scene.roots, index);
    if (target)
      scene.plugin.managers.structure.hierarchy.toggleVisibility(
        [target],
        hidden ? "hide" : "show",
      );
  }

  async superpose(onto: number): Promise<SuperpositionResult[]> {
    const scene = this.#scene;
    return scene ? superpose(scene.plugin, scene.roots, onto) : [];
  }

  async #render(): Promise<void> {
    const generation = ++this.#generation;
    this.#dispose?.();
    this.#dispose = null;
    this.#scene = null;
    this.#root.replaceChildren();
    const data = this.#data;
    if (!data?.structures.length) return;
    const status = document.createElement("p");
    status.setAttribute("role", "status");
    this.#root.append(status);
    if (!webgl()) {
      status.textContent = "This browser cannot draw 3D structures. WebGL is unavailable.";
      this.dispatchEvent(
        new CustomEvent("structure-unsupported", {
          detail: "webgl",
          bubbles: true,
          composed: true,
        }),
      );
      return;
    }
    status.textContent = "Drawing structures…";
    const media = matchMedia("(prefers-color-scheme: dark)");
    const style = document.createElement("style");
    const target = document.createElement("div");
    const theme = () => {
      style.textContent =
        (media.matches ? dark : light) +
        "\n:host{display:block;position:relative;height:28rem} .stage{position:absolute;inset:0}";
    };
    theme();
    target.className = "stage";
    this.#root.append(style, target);
    const root = createRoot(target);
    let plugin: PluginContext | undefined;
    const abort = new AbortController();
    // Mol* mounts a fresh background-image link whenever the viewport becomes empty.
    const logos = new MutationObserver(() => {
      target
        .querySelector("a.msp-logo:not([aria-label])")
        ?.setAttribute("aria-label", "Mol* project website");
    });
    logos.observe(target, { childList: true, subtree: true });
    abort.signal.addEventListener(
      "abort",
      () => {
        logos.disconnect();
      },
      { once: true },
    );
    try {
      plugin = await createPlugin(target, (component) => {
        root.render(component);
      });
      if (generation !== this.#generation) return;
      const active = plugin;
      const refresh = () => {
        theme();
        active.canvas3d?.setProps({
          renderer: { backgroundColor: Color(media.matches ? 0x14161a : 0xffffff) },
        });
      };
      refresh();
      media.addEventListener("change", refresh, { signal: abort.signal });
      const roots: string[] = [];
      await show(plugin, data, roots);
      if (generation !== this.#generation) return;
      // Mol*'s side panels overflow on narrow screens. Give each scroll region a
      // keyboard stop and a name even when its current contents have no focus target.
      for (const panel of target.querySelectorAll<HTMLElement>(".msp-scrollable-container")) {
        panel.tabIndex = 0;
        panel.setAttribute("role", "region");
        panel.setAttribute(
          "aria-label",
          panel.closest(".msp-layout-left") ? "Scene navigation" : "Structure controls",
        );
      }
      this.#scene = { plugin, roots };
      const changes = plugin.managers.structure.selection.events.changed.subscribe(() => {
        const selections = roots.map((_, i) => {
          const structure = structureAt(active, roots, i)?.cell.obj?.data;
          return structure ? selectionOf(active, structure) : [];
        });
        this.dispatchEvent(
          new CustomEvent("structure-selection", {
            detail: selections,
            bubbles: true,
            composed: true,
          }),
        );
      });
      this.#dispose = () => {
        changes.unsubscribe();
        abort.abort();
        root.unmount();
        active.dispose();
      };
      status.remove();
    } catch (error) {
      if (generation !== this.#generation) return;
      const reason = error instanceof Error ? error.message : String(error);
      status.setAttribute("role", "alert");
      status.textContent = reason;
      this.dispatchEvent(
        new CustomEvent("structure-error", { detail: reason, bubbles: true, composed: true }),
      );
      return;
    } finally {
      if (!this.#scene || generation !== this.#generation) {
        abort.abort();
        root.unmount();
        plugin?.dispose();
        target.remove();
      }
    }
    this.dispatchEvent(new CustomEvent("structure-ready", { bubbles: true, composed: true }));
  }
}

if (!customElements.get("iki-structure-viewer"))
  customElements.define("iki-structure-viewer", StructureViewer);

declare global {
  interface HTMLElementEventMap {
    "structure-ready": Event;
    "structure-error": CustomEvent<string>;
    "structure-unsupported": CustomEvent<"webgl">;
    "structure-selection": CustomEvent<Span[][]>;
  }
  interface HTMLElementTagNameMap {
    "iki-structure-viewer": StructureViewer;
  }
}
