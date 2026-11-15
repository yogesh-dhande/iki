/**
 * Views (core.View, ADR 0086): a saved look at an entity in one of Iki's viewers, at the
 * revision it showed. Each viewer keeps its own part of the state: the sequence viewer the
 * feature it showed, the structure viewer where the camera was. `version` says which
 * shape the state is, so views saved now keep opening as the viewers grow.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions } from "@tanstack/react-query";

import { iki } from "./api";
import type { Entity } from "./entities";

export const VIEW_TYPE = "core.View";

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

export interface ViewState {
  version: 1;
  feature?: FeatureRef;
  camera?: Camera;
}

/** What a viewer shows now, as a view would save it. */
export type Capture = () => Promise<ViewState>;

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

/** A view's state, as far as this version of Iki reads it. */
export function readViewState(value: unknown): ViewState {
  if (typeof value !== "object" || value === null) return { version: 1 };
  const v = value as Record<string, unknown>;
  const feature = readFeature(v["feature"]);
  const camera = readCamera(v["camera"]);
  return { version: 1, ...(feature ? { feature } : {}), ...(camera ? { camera } : {}) };
}

/** The saved views of an entity in a viewer, newest first. */
export const savedViewsQuery = (entity: Entity, viewer: ViewerName) =>
  queryOptions({
    queryKey: ["views", entity.id, viewer],
    queryFn: async () =>
      unwrap(
        await iki.POST("/api/v1/entities/search", {
          body: {
            type: VIEW_TYPE,
            where: {
              all: [
                { field: "target", op: "eq", value: entity.id },
                { field: "viewer", op: "eq", value: viewer },
              ],
            },
            sort: { field: "created_at", descending: true },
            limit: 50,
            exact_type: true,
          },
        }),
      ).items,
  });
