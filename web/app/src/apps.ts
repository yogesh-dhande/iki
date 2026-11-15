/**
 * Apps (core.App, ADR 0090): pages of Iki's own components, each panel bound to those
 * before it by CEL expressions between `{{` and `}}`. The server checked the spec when it
 * was saved; this reads it, and renders its templates with iki-wasm in a Web Worker.
 */

import type { Entity } from "./entities";
import { newWorker } from "./reading";
import expressionsUrl from "./workers/expressions.ts?worker&url";

export const APP_TYPE = "core.App";

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

export type Panel = { id: string; title?: string; width?: number } & (
  | { component: "iki.entities"; props: EntitiesProps }
  | {
      component: "iki.viewer";
      props: { entity: string; viewer?: "sequence" | "structure" | "alignment" };
    }
  | { component: "iki.text"; props: { text: string } }
);

export interface AppSpec {
  version: 1;
  panels: Panel[];
}

const COMPONENTS = new Set(["iki.entities", "iki.viewer", "iki.text"]);

/** An app's spec, as the server checked it; `null` for one this version can't show. */
export function readSpec(value: unknown): AppSpec | null {
  if (typeof value !== "object" || value === null) return null;
  const spec = value as { version?: unknown; panels?: unknown };
  if (spec.version !== 1 || !Array.isArray(spec.panels)) return null;
  const known = (spec.panels as { component?: unknown }[]).every(
    (p) => typeof p.component === "string" && COMPONENTS.has(p.component),
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
  resolve: (values: Rendered[][]) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout> | undefined;
  request: string;
}

/** One worker, kept between renderings and replaced when one runs too long. Requests
 * wait their turn, so each one's time is its own. */
class Renderer {
  private worker: Worker | null = null;
  private ready: Promise<void> | null = null;
  private queue: Pending[] = [];
  private busy: Pending | null = null;

  render(request: string): Promise<Rendered[][]> {
    return new Promise((resolve, reject) => {
      this.queue.push({ resolve, reject, timer: undefined, request });
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
        this.worker?.postMessage({ request: pending.request });
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
      const found = JSON.parse(data.json) as { values?: Rendered[][]; error?: string };
      if (found.values) pending.resolve(found.values);
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

/** Each template's value, once or for each row: a whole expression's value as it is,
 * text with each expression's value written in, or why it has none. */
export function renderTemplates(
  templates: string[],
  variables: Offers,
  rows?: unknown[],
): Promise<Rendered[][]> {
  if (templates.length === 0) return Promise.resolve([]);
  renderer ??= new Renderer();
  return renderer.render(JSON.stringify({ templates, variables, ...(rows ? { rows } : {}) }));
}
