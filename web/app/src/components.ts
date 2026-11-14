/**
 * Plugins' components (ADR 0084): which show where, and the messages their frames may
 * send. A component's code runs only in a sandboxed frame; the page checks everything
 * it says, because the frame is the plugin's, not Iki's.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions } from "@tanstack/react-query";

import { iki } from "./api";
import type { EntityType } from "./entities";

export type PluginComponent = Schemas["ComponentOut"];

/** The installed components: they change only when the deployment does. */
export const componentsQuery = queryOptions({
  queryKey: ["components"],
  queryFn: async () => unwrap(await iki.GET("/api/v1/components")),
  staleTime: 10 * 60_000,
});

/** A type and the types it extends, nearest first. */
export function lineageOf(name: string, types: EntityType[]): string[] {
  const out: string[] = [];
  let current: string | null | undefined = name;
  while (current && !out.includes(current)) {
    out.push(current);
    const at: string = current;
    current = types.find((t) => t.name === at)?.extends;
  }
  return out;
}

export interface EntityTab {
  component: PluginComponent;
  label: string;
}

/** The tabs plugins add to the pages of entities of `type`, its parents' included. */
export function entityTabs(
  type: string,
  types: EntityType[],
  components: PluginComponent[],
): EntityTab[] {
  const lineage = new Set(lineageOf(type, types));
  return components.flatMap((component) => {
    // `entity.tab` is the only slot so far.
    const slot = component.slots.find((s) => lineage.has(s.type));
    return slot ? [{ component, label: slot.label }] : [];
  });
}

/** The most a frame may grow to, and starts at. */
export const MAX_FRAME_HEIGHT = 20_000;
export const MIN_FRAME_HEIGHT = 160;
/** The most entities one selection may hold. */
export const MAX_SELECTED = 1000;

export type FrameMessage =
  { type: "ready" } | { type: "resize"; height: number } | { type: "select"; entities: string[] };

/** A frame's message, if it's one the component may send; null otherwise. */
export function readMessage(component: PluginComponent, data: unknown): FrameMessage | null {
  if (typeof data !== "object" || data === null) return null;
  const message = data as Record<string, unknown>;
  switch (message["type"]) {
    case "ready":
      return { type: "ready" };
    case "resize": {
      const height = message["height"];
      if (typeof height !== "number" || !Number.isFinite(height)) return null;
      const clamped = Math.round(Math.min(Math.max(height, MIN_FRAME_HEIGHT), MAX_FRAME_HEIGHT));
      return { type: "resize", height: clamped };
    }
    case "select": {
      const entities = message["entities"];
      if (!component.selects || !Array.isArray(entities)) return null;
      if (entities.length > MAX_SELECTED) return null;
      const refs = entities.filter(
        (e): e is string => typeof e === "string" && e.length > 0 && e.length <= 100,
      );
      if (refs.length !== entities.length) return null;
      return { type: "select", entities: [...new Set(refs)] };
    }
    default:
      return null;
  }
}
