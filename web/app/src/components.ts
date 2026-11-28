/**
 * Plugins' components (ADR 0084, ADR 0117): which show where, and what fills each place.
 * What their frames may send, and how the page reads it, is @ikisci/component's frame
 * protocol (ADR 0286): the page imports its validators, so what a plugin's component is
 * written against is what the page checks.
 */

import { RECORD_UUID } from "@ikisci/component";
import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions } from "@tanstack/react-query";

import { iki } from "./api";
import type { EntityType } from "./entities";

export type PluginComponent = Schemas["ComponentOut"];
export type ComponentSlot = PluginComponent["slots"][number];
export type SlotKind = ComponentSlot["slot"];
/** An entity as a component that shows many gets it: only the fields it reads. */
export type ComponentEntity = Schemas["ComponentEntityOut"];
export type Condition = Schemas["Condition"];
/** The grid's search, as a component's place gives it. */
export type PlaceSearch = Schemas["PlaceSearchIn"];

/** What fills a component's place: exactly one of these (ADR 0117). */
export type PlaceRequest =
  { entity: string } | { search: PlaceSearch } | { project: string } | { home: true };

/** The installed components: they change only when the deployment does. */
export const componentsQuery = queryOptions({
  queryKey: ["components"],
  queryFn: async ({ signal }) => unwrap(await iki.GET("/api/v1/components", { signal: signal })),
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
  slot: ComponentSlot;
}

/** A tab for this type is on every record's page where its search finds something for
 * the record, unless the component has a tab for a nearer type (ADR 0237). */
export const EVERY_RECORD = "iki/core.Entity";

/** Whether the tab is on every record, shown only where its search finds something. */
export function onEveryRecord(tab: EntityTab): boolean {
  return tab.slot.type === EVERY_RECORD;
}

/** The component's tab on pages of entities of `type`: the one for the nearest type it
 * extends, or else its tab on every record, as the server picks it. */
export function tabFor(
  component: PluginComponent,
  type: string,
  types: EntityType[],
): ComponentSlot | undefined {
  const tabs = component.slots.filter((s) => s.slot === "entity.tab");
  for (const name of [...lineageOf(type, types), EVERY_RECORD]) {
    const found = tabs.find((s) => s.type === name);
    if (found) return found;
  }
  return undefined;
}

/** The tabs plugins add to the pages of entities of `type`, its parents' included. */
export function entityTabs(
  type: string,
  types: EntityType[],
  components: PluginComponent[],
): EntityTab[] {
  return components.flatMap((component) => {
    const slot = tabFor(component, type, types);
    return slot ? [{ component, label: slot.label, slot }] : [];
  });
}

export interface GridView {
  component: PluginComponent;
  label: string;
}

/** The components that can show a grid's results of `type`: those that show it, or a type
 * it extends. */
export function gridViews(
  type: string | undefined,
  types: EntityType[],
  components: PluginComponent[],
): GridView[] {
  if (!type) return [];
  const lineage = new Set(lineageOf(type, types));
  return components.flatMap((component) => {
    const slot = component.slots.find((s) => s.slot === "search.results");
    return slot && component.entities && lineage.has(component.entities.type)
      ? [{ component, label: slot.label }]
      : [];
  });
}

/** The most sections plugins' components add to a project's page or a person's home. */
export const MAX_SECTIONS = 8;

export interface Section {
  component: PluginComponent;
  label: string;
}

/** The sections plugins' components add to a project's page or a person's home. */
export function sectionsFor(
  kind: "project.page" | "person.home",
  components: PluginComponent[],
): Section[] {
  return components
    .flatMap((component) => {
      const slot = component.slots.find((s) => s.slot === kind);
      return slot && component.entities ? [{ component, label: slot.label }] : [];
    })
    .slice(0, MAX_SECTIONS);
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export interface PinnedContext {
  values?: Record<string, unknown>;
  refs?: Record<string, { purged: boolean }>;
}

/** A stored revision a component asked to open (ADR 0189): a project record's, or its
 * registry entry's, by UUID. */
export interface PinnedRevision {
  of: "record" | "entry";
  id: string;
  revision: number;
}

/** The revision a component asked to open, if it's one a pin in the host's copy of the
 * entity names, of a reference the person can read: a record's `record_id` and
 * `record_revision`, or a registry pin's `entry_id` and `entry_revision`. Iki opens its page,
 * whose revision API checks the person's access to that exact record again. */
export function pinnedRevision(
  given: PinnedContext | undefined,
  id: string,
  revision: number,
): PinnedRevision | null {
  if (!RECORD_UUID.test(id) || !Number.isSafeInteger(revision) || revision < 1) return null;
  if (!given?.refs || !given.values) return null;
  for (const rows of Object.values(given.values)) {
    if (!Array.isArray(rows)) continue;
    for (const row of rows) {
      if (!isObject(row)) continue;
      const ref = row["entry_id"] ?? row["record_id"];
      if (typeof ref !== "string" || !RECORD_UUID.test(ref)) continue;
      const readable = given.refs[ref];
      if (!readable || readable.purged) continue;
      if (row["record_id"] === id && row["record_revision"] === revision) {
        return { of: "record", id, revision };
      }
      if (row["entry_id"] === id && row["entry_revision"] === revision) {
        return { of: "entry", id, revision };
      }
    }
  }
  return null;
}
