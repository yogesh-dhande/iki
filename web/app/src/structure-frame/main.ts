/**
 * Mol*, in a frame of Iki's own: what the structure viewer runs (ADR 0085).
 *
 * The page hands it one port, then the structures it shows: each one's bytes, format and
 * label, and, for a saved view (ADR 0086, ADR 0110), its colour, whether it's hidden, how it
 * was moved and what was selected, and where the camera was. Each structure is its own
 * component in one scene. The frame answers `{ get: "state" }` with all of that as it is now,
 * so the page can save it, superposes structures on one of them when asked, and colours or
 * hides one. The frame has no network and no origin that's anyone's, so a file that fooled
 * Mol*'s parsers could only draw something wrong, never read Iki's pages or act as the person.
 */

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
import { renderReact18 } from "molstar/lib/mol-plugin-ui/react18.js";
import { DefaultPluginUISpec } from "molstar/lib/mol-plugin-ui/spec.js";
import { MolScriptBuilder as MS } from "molstar/lib/mol-script/language/builder.js";
import { Script } from "molstar/lib/mol-script/script.js";
import { Color } from "molstar/lib/mol-util/color/index.js";
import dark from "molstar/build/viewer/theme/dark.css?inline";
import light from "molstar/build/viewer/theme/light.css?inline";
import { fit as fitted, MAX_SPANS, spansOf, spanTest, type Span } from "./residues";

type Format = "mmcif" | "pdb";

/** Where the camera is, as a view saves it (iki/core.View's state). */
interface Camera {
  position: number[];
  target: number[];
  up: number[];
  fov?: number;
  radius?: number;
  mode?: "perspective" | "orthographic";
}

/** One structure of the scene, as the page gives it. */
interface Given {
  structure: ArrayBuffer;
  format: Format;
  label: string;
  colour?: string;
  hidden?: boolean;
  transform?: number[];
  selection?: Span[];
}

interface Show {
  structures: Given[];
  camera?: Camera;
}

/** What the page can ask once the scene is shown. */
type Ask =
  | { get: "state" }
  | { superpose: { onto: number } }
  | { colour: { index: number; colour: string | null } }
  | { hidden: { index: number; hidden: boolean } };

let shown: PluginContext | null = null;
/** Each structure's root in Mol*'s state, in the order the page gave them. */
let roots: string[] = [];

function round(values: ArrayLike<number>, places = 3): number[] {
  const scale = 10 ** places;
  return Array.from(values, (v) => Math.round(v * scale) / scale);
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

/** The structure at `index`, as Mol*'s hierarchy has it now. */
function structureAt(plugin: PluginContext, index: number): StructureRef | undefined {
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

type Result = { index: number; rmsd: number; residues: number } | { index: number; failed: string };

/** Superposes each other shown structure on the one at `onto`, by their first chains,
 * paired by aligning their sequences, as Mol*'s own superposition does, over as many
 * residues as were paired. */
async function superpose(plugin: PluginContext, onto: number): Promise<Result[]> {
  const pivot = structureAt(plugin, onto);
  const pivotData = pivot?.cell.obj?.data;
  const reference = pivotData ? chainOf(pivotData) : null;
  const results: Result[] = [];
  if (!pivot || !reference) {
    return roots.flatMap((_, index) =>
      index === onto ? [] : [{ index, failed: "the structure it's superposed on has no chain" }],
    );
  }
  // Where the pivot was moved to itself, so the others land where it's drawn.
  const placed = transformOf(plugin, pivot) ?? Mat4.identity();
  for (let index = 0; index < roots.length; index++) {
    if (index === onto) continue;
    const structure = structureAt(plugin, index);
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

interface StructureNow {
  transform?: number[];
  hidden?: boolean;
  selection?: Span[];
}

function stateNow(plugin: PluginContext): { camera: Camera | null; structures: StructureNow[] } {
  return {
    camera: cameraNow(plugin),
    structures: roots.map((_, index) => {
      const structure = structureAt(plugin, index);
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
  roots = [];
  for (const given of message.structures) {
    const data = await plugin.builders.data.rawData({
      data: new TextDecoder().decode(given.structure),
      label: given.label,
    });
    const trajectory = await plugin.builders.structure.parseTrajectory(data, given.format);
    const made = await plugin.builders.structure.hierarchy.applyPreset(trajectory, "default");
    roots.push(made?.structure.ref ?? "");
  }
  for (const [index, given] of message.structures.entries()) {
    const structure = structureAt(plugin, index);
    if (!structure) continue;
    if (given.transform) await move(plugin, structure, Mat4.fromArray(Mat4(), given.transform, 0));
    if (given.colour) await colour(plugin, structure, given.colour);
    const data = structureAt(plugin, index)?.cell.obj?.data;
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
  shown = plugin;
}

themed();
scheme.addEventListener("change", themed);

const FORMATS = new Set(["mmcif", "pdb"]);

/** What the page gave, if it's a scene to show: anything else is ignored. */
function readShow(data: unknown): Show | null {
  if (typeof data !== "object" || data === null) return null;
  const { structures, camera } = data as Record<string, unknown>;
  if (!Array.isArray(structures) || !structures.length) return null;
  const given = structures.flatMap((s: unknown) => {
    if (typeof s !== "object" || s === null) return [];
    const v = s as Record<string, unknown>;
    if (!(v["structure"] instanceof ArrayBuffer) || !FORMATS.has(String(v["format"]))) return [];
    const transform = v["transform"];
    return [
      {
        structure: v["structure"],
        format: v["format"] as Format,
        label: typeof v["label"] === "string" ? v["label"].slice(0, 200) : "Structure",
        ...(typeof v["colour"] === "string" && /^#[0-9a-f]{6}$/.test(v["colour"])
          ? { colour: v["colour"] }
          : {}),
        ...(v["hidden"] === true ? { hidden: true } : {}),
        ...(Array.isArray(transform) &&
        transform.length === 16 &&
        transform.every((n) => typeof n === "number" && Number.isFinite(n))
          ? { transform: transform as number[] }
          : {}),
        ...(Array.isArray(v["selection"]) ? { selection: v["selection"] as Span[] } : {}),
      },
    ];
  });
  if (given.length !== structures.length) return null;
  return { structures: given, ...(camera ? { camera: camera as Camera } : {}) };
}

async function answer(plugin: PluginContext, ask: Ask, port: MessagePort): Promise<void> {
  if ("get" in ask) {
    port.postMessage({ state: stateNow(plugin) });
  } else if ("superpose" in ask) {
    port.postMessage({ superposed: await superpose(plugin, ask.superpose.onto) });
  } else if ("colour" in ask) {
    const structure = structureAt(plugin, ask.colour.index);
    if (structure) await colour(plugin, structure, ask.colour.colour);
  } else {
    const structure = structureAt(plugin, ask.hidden.index);
    if (structure)
      plugin.managers.structure.hierarchy.toggleVisibility(
        [structure],
        ask.hidden.hidden ? "hide" : "show",
      );
  }
}

function readAsk(data: unknown): Ask | null {
  if (typeof data !== "object" || data === null) return null;
  const v = data as Record<string, unknown>;
  const index = (o: unknown) => {
    const i = (o as Record<string, unknown> | null)?.["index"];
    return Number.isInteger(i) && (i as number) >= 0 && (i as number) < roots.length;
  };
  if (v["get"] === "state") return { get: "state" };
  const onto = (v["superpose"] as Record<string, unknown> | null | undefined)?.["onto"];
  if (Number.isInteger(onto) && (onto as number) >= 0 && (onto as number) < roots.length)
    return { superpose: { onto: onto as number } };
  if (index(v["colour"])) {
    const c = v["colour"] as { index: number; colour: unknown };
    const value = typeof c.colour === "string" && /^#[0-9a-f]{6}$/.test(c.colour) ? c.colour : null;
    return { colour: { index: c.index, colour: value } };
  }
  if (index(v["hidden"])) {
    const h = v["hidden"] as { index: number; hidden: unknown };
    return { hidden: { index: h.index, hidden: h.hidden === true } };
  }
  return null;
}

function accept(event: MessageEvent): void {
  // Only Iki may frame this page (frame-ancestors), so the parent's port is Iki's. Mol*
  // posts messages to its own window too (to schedule work), so wait for that one.
  const port = event.ports[0];
  if (event.source !== window.parent || !port) return;
  window.removeEventListener("message", accept);
  let started = false;
  port.onmessage = (message: MessageEvent<unknown>) => {
    if (started) {
      const ask = readAsk(message.data);
      if (!ask) return;
      if (!shown) {
        if ("get" in ask) port.postMessage({ state: null });
        return;
      }
      answer(shown, ask, port).catch((error: unknown) => {
        port.postMessage({ error: error instanceof Error ? error.message : String(error) });
      });
      return;
    }
    const scene = readShow(message.data);
    const target = document.getElementById("structure");
    if (!target || !scene) return;
    started = true;
    if (!webgl()) {
      port.postMessage({ unsupported: "webgl" });
      return;
    }
    show(target, scene).then(
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
