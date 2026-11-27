/**
 * Apps (iki/core.App, ADR 0090): pages of Iki's own components, each panel bound to those
 * before it by CEL expressions between `{{` and `}}`. The server checked the spec when it
 * was saved; this reads it, and renders its templates with iki-wasm in a Web Worker.
 */

import { unwrap } from "@ikisci/sdk";
import { queryOptions } from "@tanstack/react-query";

import { everyPage, iki } from "./api";
import { COMPONENT_NAME, ENTITIES, type Entity } from "./entities";
import { newWorker } from "./reading";
import expressionsUrl from "./workers/expressions.ts?worker&url";

export const APP_TYPE = "iki/core.App";

export interface EntitiesProps {
  type: string;
  scope?: "project" | "all";
  where?: Record<string, unknown>;
  q?: string;
  sort?: string;
  descending?: boolean;
  limit?: number;
  select?: "none" | "one" | "many";
  columns?: ({ field: string; label?: string } | { label: string; value: string })[];
}

/** A plugin's component's props (ADR 0117): one entity, or a search as a grid's. */
export type PluginProps =
  | { entity: string }
  | Pick<EntitiesProps, "type" | "scope" | "where" | "q" | "sort" | "descending">;

export type Panel = { id: string; title?: string; width?: number } & (
  | { component: "iki.entities"; props: EntitiesProps }
  | {
      component: "iki.viewer";
      props: { entity: string; viewer?: "sequence" | "structure" | "alignment" };
    }
  | { component: "iki.text"; props: { text: string } }
  | { component: `${string}/${string}`; props: PluginProps }
);

export interface AppSpec {
  version: 1;
  /** The type of entity it's about: their pages show it as a tab, and its expressions
   * read that entity as `entity`. */
  entity?: { type: string };
  panels: Panel[];
}

/** The most apps an entity's page shows as tabs: more wouldn't fit as tabs. */
const APP_TABS_SHOWN = 20;

/** The apps about an entity, which its page shows as tabs: the first {@link APP_TABS_SHOWN}. */
export function entityAppsQuery(ref: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "apps", ref.toUpperCase()],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}/apps", {
          params: { path: { ref }, query: { limit: APP_TABS_SHOWN } },
          signal,
        }),
      ).items,
  });
}

/**
 * The key of what a project's page opens with and could. It's under the entities' key:
 * the choices are the project's apps, which are entities, so creating, editing,
 * archiving or restoring one (each of which refreshes the entities) refreshes them too.
 */
export function dashboardKey(projectId: string) {
  return [...ENTITIES, "dashboard", projectId] as const;
}

/** The app a project's page opens with, chosen by its Admins: none if there isn't one,
 * or the reader can't read it there now. */
export function dashboardQuery(projectId: string) {
  return queryOptions({
    queryKey: dashboardKey(projectId),
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/projects/{project_id}/dashboard", {
          params: { path: { project_id: projectId } },
          signal,
        }),
      ),
  });
}

/** The project's apps its Admins could choose for its page: not about an entity. */
export function dashboardChoicesQuery(projectId: string) {
  return queryOptions({
    queryKey: [...dashboardKey(projectId), "choices"],
    queryFn: ({ signal }) =>
      everyPage(async (page) =>
        unwrap(
          await iki.GET("/api/v1/projects/{project_id}/dashboard/choices", {
            params: { path: { project_id: projectId }, query: page },
            signal,
          }),
        ),
      ),
  });
}

/** A draft of an app, which only the person it's for can open, for a day. */
export function appDraftQuery(id: string) {
  return queryOptions({
    queryKey: ["app-drafts", id],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/apps/drafts/{draft_id}", {
          params: { path: { draft_id: id } },
          signal,
        }),
      ),
    retry: false,
  });
}

function isTemplate(value: unknown): value is string {
  return typeof value === "string" && value.includes("{{");
}

type Leaf = Record<string, unknown> & { value?: unknown };

/** A copy of a grid's conditions, each leaf as `leaf` makes it, in one order. */
function eachCondition(node: unknown, leaf: (node: Leaf) => Leaf): unknown {
  if (typeof node !== "object" || node === null || Array.isArray(node)) return node;
  const out: Leaf = { ...(node as Leaf) };
  for (const key of ["all", "any"] as const) {
    const children = out[key];
    if (Array.isArray(children)) out[key] = children.map((c) => eachCondition(c, leaf));
  }
  if ("not" in out) out["not"] = eachCondition(out["not"], leaf);
  return "value" in out ? leaf(out) : out;
}

/** The values of a grid's conditions that are expressions, in order. */
export function conditionTemplates(where: unknown): string[] {
  const found: string[] = [];
  eachCondition(where, (leaf) => {
    if (isTemplate(leaf.value)) found.push(leaf.value);
    return leaf;
  });
  return found;
}

/** A grid's conditions with each expression's value in its place, in the order
 * {@link conditionTemplates} lists them. */
export function fillConditions(where: unknown, values: unknown[]): unknown {
  let next = 0;
  return eachCondition(where, (leaf) =>
    isTemplate(leaf.value) ? { ...leaf, value: values[next++] } : leaf,
  );
}

const COMPONENTS = new Set(["iki.entities", "iki.viewer", "iki.text"]);

/** An app's spec, as the server checked it; `null` for one this version can't show. */
export function readSpec(value: unknown): AppSpec | null {
  if (typeof value !== "object" || value === null) return null;
  const spec = value as { version?: unknown; panels?: unknown };
  if (spec.version !== 1 || !Array.isArray(spec.panels)) return null;
  // Iki's own components, or a plugin's by its name; whether it's installed, its panel says.
  const known = (spec.panels as { component?: unknown }[]).every(
    (p) =>
      typeof p.component === "string" &&
      (COMPONENTS.has(p.component) || COMPONENT_NAME.test(p.component)),
  );
  return known ? (value as AppSpec) : null;
}

/** What an entity is to expressions: a grid's rows and selection, a viewer's entity. */
export interface EntityValue {
  id: string;
  hrid: string;
  type: string;
  title: string;
  revision: number;
  values: Record<string, unknown>;
}

export function entityValue(entity: Entity): EntityValue {
  return {
    id: entity.id,
    hrid: entity.hrid,
    type: entity.type,
    title: entity.title,
    revision: entity.revision,
    values: entity.values,
  };
}

/** What earlier panels offer later ones, by panel ID. */
export type Offers = Record<string, Record<string, unknown>>;

export type Rendered = { value: unknown } | { error: string };

/** How long a rendering may run, in milliseconds, before its worker is ended. It runs
 * off the page's thread, so this stops runaway expressions rather than keeping the page
 * smooth, and it's well over what the server lets the largest app's expressions take. */
export const LIMIT_MS = 1000;

interface Pending {
  resolve: (found: unknown) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout> | undefined;
  request: string;
  /** Templates, plugins' rules (ADR 0127), or HELM (ADR 0144). */
  call: "render" | "rules" | "helm";
}

/** One worker, kept between renderings and replaced when one runs too long. Requests
 * wait their turn, so each one's time is its own. */
class Renderer {
  private worker: Worker | null = null;
  private ready: Promise<void> | null = null;
  private queue: Pending[] = [];
  private busy: Pending | null = null;

  render(request: string): Promise<Rendered[][]> {
    return this.ask(request, "render") as Promise<Rendered[][]>;
  }

  /** Plugins' rules' expressions, evaluated in each context (see `rules.ts`). */
  rules(request: string): Promise<unknown> {
    return this.ask(request, "rules");
  }

  /** HELM strings read (see `helm.ts`). */
  helm(request: string): Promise<unknown> {
    return this.ask(request, "helm");
  }

  private ask(request: string, call: Pending["call"]): Promise<unknown> {
    return new Promise((resolve, reject) => {
      this.queue.push({ resolve, reject, timer: undefined, request, call });
      this.next();
    });
  }

  private start(): Promise<void> {
    if (this.ready) return this.ready;
    const worker = newWorker(expressionsUrl);
    this.worker = worker;
    this.ready = new Promise((resolve, reject) => {
      worker.onmessage = (event: MessageEvent) => {
        const data = event.data as { ready?: true; failed?: string; json?: string; error?: string };
        if (data.ready) resolve();
        else if (data.failed !== undefined) reject(new Error("Expressions didn’t load."));
        else this.finish(data);
      };
      worker.onerror = () => {
        reject(new Error("Expressions didn’t load."));
        this.stop(new Error("Expressions didn’t load."));
      };
    });
    return this.ready;
  }

  private next(): void {
    if (this.busy) return;
    const pending = this.queue.shift();
    if (!pending) return;
    this.busy = pending;
    this.start().then(
      () => {
        pending.timer = setTimeout(() => {
          this.stop(
            new Error(`Its expressions ran for over ${String(LIMIT_MS)} ms, so they were stopped.`),
          );
        }, LIMIT_MS);
        this.worker?.postMessage({ request: pending.request, call: pending.call });
      },
      (error: unknown) => {
        this.stop(error instanceof Error ? error : new Error(String(error)));
      },
    );
  }

  private finish(data: { json?: string; error?: string }): void {
    const pending = this.busy;
    if (!pending) return;
    clearTimeout(pending.timer);
    this.busy = null;
    if (data.json === undefined) {
      pending.reject(new Error(data.error ?? "Expressions failed."));
    } else {
      const found = JSON.parse(data.json) as {
        values?: Rendered[][];
        results?: unknown;
        readings?: unknown;
        error?: string;
      };
      const answer = { rules: found.results, helm: found.readings, render: found.values }[
        pending.call
      ];
      if (answer !== undefined) pending.resolve(answer);
      else pending.reject(new Error(found.error ?? "Expressions failed."));
    }
    this.next();
  }

  /** Ends the worker, failing the rendering it was on; the next one starts another. */
  private stop(error: Error): void {
    this.worker?.terminate();
    this.worker = null;
    this.ready = null;
    const pending = this.busy;
    this.busy = null;
    if (pending) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.next();
  }
}

let renderer: Renderer | null = null;

/** The page's one worker for expressions: apps', rules' and HELM's. */
export function expressionRenderer(): Renderer {
  renderer ??= new Renderer();
  return renderer;
}

/** Each template's value, once or for each row: a whole expression's value as it is,
 * text with each expression's value written in, or why it has none. */
export function renderTemplates(
  templates: string[],
  variables: Offers,
  rows?: unknown[],
): Promise<Rendered[][]> {
  if (templates.length === 0) return Promise.resolve([]);
  return expressionRenderer().render(
    JSON.stringify({ templates, variables, ...(rows ? { rows } : {}) }),
  );
}
