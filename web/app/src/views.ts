/**
 * Views (iki/core.View, ADR 0086, ADR 0110): a saved look at entities in Iki's viewers, each at
 * the revision it showed. A view lists its entities in order, each with its revision and its
 * viewer; those in the structure viewer are drawn together, in one Mol* scene, and each other
 * one has a viewer of its own. Each viewer keeps its own part of the state: the sequence
 * viewer the feature it showed, the alignment viewer the residue its cursor was on, the
 * structure viewer where the camera was and, for each structure, its colour, whether it's
 * hidden, how it was moved to superpose it, and what was selected. `version` says which
 * shape the state is, so views saved before keep opening as the viewers grow.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions } from "@tanstack/react-query";

import { iki } from "./api";
import type { Entity } from "./entities";

export const VIEW_TYPE = "iki/core.View";
/** How many entities a view shows at most (`targets`' `max_items`). */
export const MAX_TARGETS = 20;

export type ViewerName = Schemas["EntityTypeOut"]["viewers"][number]["viewer"];

/** A feature as a view names it: what it is and where, not its place in a list. */
export interface FeatureRef {
  type: string;
  name?: string;
  start: number;
  end: number;
}

/** Where the structure viewer's camera was. */
export interface Camera {
  position: number[];
  target: number[];
  up: number[];
  fov?: number;
  radius?: number;
  mode?: "perspective" | "orthographic";
}

/** Where the alignment viewer's cursor was: a column and a row of the file, from 1. */
export interface Position {
  column: number;
  row: number;
}

/** Residues of a chain, by its authors' numbering, from `start` to `end`, each with its
 * insertion code if it has one (35A is `start: 35, start_insertion: "A"`). */
export interface Span {
  chain: string;
  start: number;
  end: number;
  start_insertion?: string;
  end_insertion?: string;
}

/** An insertion code, as PDB files give them: one letter or digit. */
const INSERTION = /^[A-Za-z0-9]$/;

/** What a structure was superposed on (another entity's index) and how closely. */
export interface Superposed {
  onto: number;
  rmsd?: number;
  residues?: number;
}

/** What a view keeps of one of its entities, as its viewer saves it. */
export interface TargetState {
  label?: string;
  feature?: FeatureRef;
  position?: Position;
  colour?: string;
  hidden?: boolean;
  /** How it was moved: a 4×4 matrix in column-major order, as Mol* keeps it. */
  transform?: number[];
  superposed?: Superposed;
  selection?: Span[];
}

/** A view's state, as this version of Iki saves it. */
export interface ViewState {
  version: 2;
  camera?: Camera;
  targets: TargetState[];
}

/** What a viewer shows now, as a view would save it: the camera, if it has one, and what
 * it keeps of each entity it shows, in order. */
export interface Captured {
  camera?: Camera;
  targets: TargetState[];
}
export type Capture = () => Promise<Captured>;

/** One of the entities a view shows: which, at what revision, in which viewer. */
export interface ViewTarget {
  id: string;
  revision: number;
  viewer: ViewerName;
}

const VIEWERS = new Set<string>(["sequence", "structure", "alignment"]);

/** The entities a view shows, in order. */
export function viewTargets(view: Entity): ViewTarget[] {
  const ids: unknown = view.values["targets"];
  const revisions: unknown = view.values["target_revisions"];
  const viewers: unknown = view.values["viewers"];
  if (!Array.isArray(ids) || !Array.isArray(revisions) || !Array.isArray(viewers)) return [];
  return (ids as unknown[]).flatMap((id, index) => {
    const revision: unknown = (revisions as unknown[])[index];
    const viewer: unknown = (viewers as unknown[])[index];
    return typeof id === "string" &&
      Number.isInteger(revision) &&
      typeof viewer === "string" &&
      VIEWERS.has(viewer)
      ? [{ id, revision: revision as number, viewer: viewer as ViewerName }]
      : [];
  });
}

function numbers(value: unknown, length: number): number[] | null {
  return Array.isArray(value) &&
    value.length === length &&
    value.every((v) => typeof v === "number" && Number.isFinite(v))
    ? (value as number[])
    : null;
}

/** A camera, if `value` is one: from a saved view, or from Mol*'s frame. */
export function readCamera(value: unknown): Camera | null {
  if (typeof value !== "object" || value === null) return null;
  const v = value as Record<string, unknown>;
  const position = numbers(v["position"], 3);
  const target = numbers(v["target"], 3);
  const up = numbers(v["up"], 3);
  if (!position || !target || !up) return null;
  const camera: Camera = { position, target, up };
  if (typeof v["fov"] === "number" && v["fov"] > 0 && v["fov"] <= 3.2) camera.fov = v["fov"];
  if (typeof v["radius"] === "number" && v["radius"] >= 0) camera.radius = v["radius"];
  if (v["mode"] === "perspective" || v["mode"] === "orthographic") camera.mode = v["mode"];
  return camera;
}

function readFeature(value: unknown): FeatureRef | null {
  if (typeof value !== "object" || value === null) return null;
  const v = value as Record<string, unknown>;
  const { type, start, end, name } = v;
  if (typeof type !== "string" || !Number.isInteger(start) || !Number.isInteger(end)) return null;
  return {
    type,
    start: start as number,
    end: end as number,
    ...(typeof name === "string" && name ? { name } : {}),
  };
}

function readPosition(value: unknown): Position | null {
  if (typeof value !== "object" || value === null) return null;
  const { column, row } = value as Record<string, unknown>;
  return Number.isInteger(column) &&
    Number.isInteger(row) &&
    (column as number) >= 1 &&
    (row as number) >= 1
    ? { column: column as number, row: row as number }
    : null;
}

/** A transform, if `value` is one: 16 finite numbers. */
export function readTransform(value: unknown): number[] | null {
  return numbers(value, 16);
}

/** Spans of residues, as a view or Mol*'s frame gives them: those that are spans. */
export function readSelection(value: unknown): Span[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((span: unknown) => {
    if (typeof span !== "object" || span === null) return [];
    const { chain, start, end, start_insertion, end_insertion } = span as Record<string, unknown>;
    const code = (c: unknown) =>
      c === undefined ? "" : typeof c === "string" && INSERTION.test(c) ? c : null;
    const from = code(start_insertion);
    const to = code(end_insertion);
    return typeof chain === "string" &&
      chain &&
      chain.length <= 8 &&
      Number.isInteger(start) &&
      Number.isInteger(end) &&
      from !== null &&
      to !== null &&
      ((start as number) < (end as number) || (start === end && from <= to))
      ? [
          {
            chain,
            start: start as number,
            end: end as number,
            ...(from ? { start_insertion: from } : {}),
            ...(to ? { end_insertion: to } : {}),
          },
        ]
      : [];
  });
}

export const COLOUR = /^#[0-9a-f]{6}$/;

function readTarget(value: unknown): TargetState {
  if (typeof value !== "object" || value === null) return {};
  const v = value as Record<string, unknown>;
  const feature = readFeature(v["feature"]);
  const position = readPosition(v["position"]);
  const transform = readTransform(v["transform"]);
  const selection = readSelection(v["selection"]);
  const label = v["label"];
  const colour = v["colour"];
  const superposed = v["superposed"] as Record<string, unknown> | null | undefined;
  const onto = superposed?.["onto"];
  return {
    ...(typeof label === "string" && label ? { label } : {}),
    ...(feature ? { feature } : {}),
    ...(position ? { position } : {}),
    ...(typeof colour === "string" && COLOUR.test(colour) ? { colour } : {}),
    ...(v["hidden"] === true ? { hidden: true } : {}),
    ...(transform ? { transform } : {}),
    ...(Number.isInteger(onto) && (onto as number) >= 0
      ? {
          superposed: {
            onto: onto as number,
            ...(typeof superposed?.["rmsd"] === "number" ? { rmsd: superposed["rmsd"] } : {}),
            ...(Number.isInteger(superposed?.["residues"])
              ? { residues: superposed?.["residues"] as number }
              : {}),
          },
        }
      : {}),
    ...(selection.length ? { selection } : {}),
  };
}

/**
 * A view's state, as far as this version of Iki reads it, for `count` entities. A view of
 * one entity saved before views could show several (version 1) keeps its feature, camera
 * or cursor at the top: it reads as the same thing in version 2's shape.
 */
export function readViewState(value: unknown, count = 1): ViewState {
  const v = typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
  const camera = readCamera(v["camera"]);
  const kept =
    v["version"] === 1 || !Array.isArray(v["targets"]) ? [v] : (v["targets"] as unknown[]);
  const targets = Array.from({ length: Math.max(count, 1) }, (_, i) => readTarget(kept[i]));
  return { version: 2, ...(camera ? { camera } : {}), targets };
}

/** A saved view of an entity in a viewer, and where in it the entity is. */
export interface SavedView {
  view: Entity;
  targets: ViewTarget[];
  /** Where the entity is among the view's targets. */
  index: number;
}

/** The saved views an entity is in, shown in a viewer, newest first. */
export const savedViewsQuery = (entity: Entity, viewer: ViewerName) =>
  queryOptions({
    queryKey: ["views", entity.id, viewer],
    queryFn: async (): Promise<SavedView[]> => {
      const found = unwrap(
        await iki.POST("/api/v1/entities/search", {
          body: {
            type: VIEW_TYPE,
            where: {
              all: [
                { field: "targets", op: "has", value: entity.id },
                { field: "viewers", op: "has", value: viewer },
              ],
            },
            sort: { field: "created_at", descending: true },
            limit: 50,
            exact_type: true,
          },
        }),
      ).items;
      // A view of it in one viewer and of something else in this one isn't a view of it here.
      return found.flatMap((view) => {
        const targets = viewTargets(view);
        const index = targets.findIndex((t) => t.id === entity.id && t.viewer === viewer);
        return index >= 0 ? [{ view, targets, index }] : [];
      });
    },
  });

/** Names and numbers in a list, as a sentence says them: "A", "A and B", "A, B and C". */
export function listed(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1] ?? ""}`;
}

/** Colours Iki gives the structures of a scene, in order: Okabe and Ito's, which people
 * with each kind of colour blindness tell apart. */
export const STRUCTURE_COLOURS: { colour: string; name: string }[] = [
  { colour: "#0072b2", name: "Blue" },
  { colour: "#e69f00", name: "Orange" },
  { colour: "#009e73", name: "Green" },
  { colour: "#cc79a7", name: "Pink" },
  { colour: "#56b4e9", name: "Sky blue" },
  { colour: "#d55e00", name: "Vermilion" },
  { colour: "#f0e442", name: "Yellow" },
  { colour: "#999999", name: "Grey" },
];
