/**
 * A fake Iki API for component tests: answers by method and path, and records
 * what the app sent, so tests check the calls the pages make.
 */

import type { Schemas } from "@ikisci/sdk";

/** JSON to answer with, a Response, or a function of the request giving either. */
type Answer = unknown;

export interface Sent {
  method: string;
  path: string;
  query: URLSearchParams;
  body: unknown;
  headers: Headers;
}

let routes = new Map<string, Answer>();
export let sent: Sent[] = [];

/** Answers `METHOD /api/v1/path` with JSON (or a `respond()` for other statuses). */
export function answer(route: string, value: Answer): void {
  routes.set(route, value);
}

export function respond(status: number, body?: unknown): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": status >= 400 ? "application/problem+json" : "application/json",
    },
  });
}

export function problem(
  status: number,
  code: string,
  detail: string,
  errors?: { pointer?: string; parameter?: string; detail: string }[],
): Response {
  const title = status === 409 ? "Conflict" : "Error";
  return respond(status, { type: "about:blank", title, status, code, detail, errors });
}

export function resetApi(): void {
  routes = new Map();
  sent = [];
}

export function calls(method: string, path?: string): Sent[] {
  return sent.filter((s) => s.method === method && (path === undefined || s.path === path));
}

export const serverFetch: typeof fetch = async (input, init) => {
  const request = new Request(input, init);
  const url = new URL(request.url);
  const text = request.method === "GET" ? "" : await request.text();
  const call: Sent = {
    method: request.method,
    path: url.pathname,
    query: url.searchParams,
    body: text ? (JSON.parse(text) as unknown) : undefined,
    headers: request.headers,
  };
  sent.push(call);
  const route = routes.get(`${call.method} ${call.path}`);
  if (route === undefined) {
    return problem(404, "not_found", `No fake for ${call.method} ${call.path}`);
  }
  const value = typeof route === "function" ? (route as (r: Sent) => unknown)(call) : route;
  return value instanceof Response ? value : respond(200, value);
};

// --- Fixtures --------------------------------------------------------------------

export const ME: Schemas["MeOut"] = {
  id: "00000000-0000-7000-8000-000000000001",
  email: "rosalind@example.com",
  name: "Rosalind Franklin",
  kind: "person",
  active: true,
  org_roles: [],
  can: { manage_org: false, read_audit: false, create_projects: true },
};

export const TEAM: Schemas["TeamRef"] = {
  id: "00000000-0000-7000-8000-0000000000aa",
  name: "Structural Biology",
  slug: "structural-biology",
};

export const ROLES: Schemas["ProjectRoleOut"][] = [
  { name: "viewer", label: "Viewer", description: "Sees everything in the project." },
  { name: "editor", label: "Editor", description: "Also changes entities and views." },
  { name: "runner", label: "Runner", description: "Also starts runs." },
  { name: "admin", label: "Admin", description: "Also manages members and settings." },
];

export function project(overrides: Partial<Schemas["ProjectOut"]> = {}): Schemas["ProjectOut"] {
  return {
    id: "00000000-0000-7000-8000-0000000000p1",
    name: "Kinase Atlas",
    slug: "kinase-atlas",
    description: "",
    team: TEAM,
    archived: false,
    created_at: "2026-03-14T09:00:00Z",
    my_role: "admin",
    can: { read: true, manage: true, manage_members: true },
    ...overrides,
  };
}

export function page<T>(items: T[]): { items: T[]; next_cursor: null } {
  return { items, next_cursor: null };
}

/** The calls every page makes. */
export function signedIn(me: Partial<Schemas["MeOut"]> = {}): void {
  answer("GET /api/v1/me", { ...ME, ...me });
  answer("GET /api/v1/project-roles", ROLES);
  answer("GET /api/v1/notifications/summary", { unread: 0 });
}

export function notification(
  overrides: Partial<Schemas["NotificationOut"]> = {},
): Schemas["NotificationOut"] {
  return {
    id: "00000000-0000-7000-8000-0000000000n1",
    kind: "project.added",
    title: "Maurice Wilkins added you to Kinase Atlas as Editor",
    body: "",
    path: "/projects/kinase-atlas/",
    project: {
      id: "00000000-0000-7000-8000-0000000000p1",
      name: "Kinase Atlas",
      slug: "kinase-atlas",
    },
    actor: { id: "00000000-0000-7000-8000-000000000002", name: "Maurice Wilkins" },
    created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    read_at: null,
    ...overrides,
  };
}

/** A stand-in for the browser's EventSource: tests send it events. */
export class FakeEventSource extends EventTarget {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static opened: FakeEventSource[] = [];
  readyState = FakeEventSource.OPEN;

  constructor(
    readonly url: string,
    readonly init?: EventSourceInit,
  ) {
    super();
    FakeEventSource.opened.push(this);
  }

  /** The stream the app has open now. */
  static current(): FakeEventSource | undefined {
    return FakeEventSource.opened.filter((s) => s.readyState !== FakeEventSource.CLOSED).at(-1);
  }

  send(event: string, data: unknown): void {
    this.dispatchEvent(new MessageEvent(event, { data: JSON.stringify(data) }));
  }

  /** The server refused the stream or went away for good. */
  fail(): void {
    this.readyState = FakeEventSource.CLOSED;
    this.dispatchEvent(new Event("error"));
  }

  close(): void {
    this.readyState = FakeEventSource.CLOSED;
  }
}
