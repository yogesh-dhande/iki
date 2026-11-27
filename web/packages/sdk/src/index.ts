/**
 * TypeScript client for the Iki API (`/api/v1`).
 *
 * The types come from the server's OpenAPI document (`make client`), so a
 * request or response that doesn't match the server fails to compile.
 *
 * ```ts
 * const iki = createIkiClient({ accessToken: () => token });
 * const { data, error } = await iki.GET("/api/v1/projects", { params: { query: { member: true } } });
 * ```
 *
 * In Iki's own pages, the browser session signs requests in; pass
 * `csrfToken` so unsafe methods carry the page's CSRF token.
 */

import createClient, {
  type Client,
  defaultPathSerializer,
  type Middleware,
  type PathSerializer,
} from "openapi-fetch";

import type { components, paths } from "./schema.js";

export type { components, paths };

/** The API's request and response bodies, by schema name. */
export type Schemas = components["schemas"];

/** The REST API version this client speaks, matching the server's `/api/v1` prefix. */
export const API_VERSION = "v1";

/** An RFC 9457 problem: how every error under `/api/` is described. */
export interface Problem {
  type: string;
  title: string;
  status: number;
  detail: string;
  /** Stable, for programs: `validation_failed`, `refused`, `insufficient_scope`, … */
  code: string;
  /** For `validation_failed` and `hold_reason_required`: which fields, and why. A plugin's
   * rule's (ADR 0127) names the `rule`, and whether it would `hold` the record, rather
   * than refuse, given a `hold_reason`. */
  errors?: {
    detail: string;
    pointer?: string;
    parameter?: string;
    rule?: string;
    hold?: boolean;
  }[];
  /** Readable alternatives when a scan needs an explicit choice (`scan_ambiguous`). */
  candidates?: Schemas["ScanCandidateOut"][];
  /** Zero-based index of the scan that needs a choice in a submitted rack. */
  index?: number;
  /** Destination slot when an unregistered tube may be an earlier observation. */
  position?: string;
  /** Previous slots offered by a `count_unregistered_choice` response. */
  observations?: { line_id: string; revision: number; position: string }[];
  /** For `guard_refused`: the plugin's guard (ADR 0128) that refused the state. */
  guard?: string;
  /** For a receipt's or count's `stale` (412): the lines that changed, at their revisions now. */
  affected_lines?: { line_id: string; revision: number }[];
  /** For `inputs_outside_project`: the inputs that aren't in the run's project. */
  entities?: { id: string; hrid: string; type: string }[];
  /**
   * For `consent_required`: what the launch sends to outside APIs, and where (ADR 0130).
   * Launch again with `consent` naming each `api` to agree.
   */
  consent?: Schemas["ConsentStatementOut"][];
  /** For `outside_api_not_approved`: the APIs, and Org Admins who can approve them. */
  apis?: string[];
  approvers?: string[];
  /** For `starter_plan_changed` (409): what applying the starter does now, to review. */
  plan?: Schemas["StarterSetupOut"];
  /** For `moves_plan_changed` (409): what the moves do now, to review. */
  moves?: Schemas["MovesOut"];
  /** For a change of many things' `stale` (412): each that changed since it was read. */
  stale?: (StaleRecord | StaleOption)[];
  request_id?: string;
}

/** True when ``value`` has the shape of a problem the API answered with. */
/** A record a reassignment named that changed since it was read. */
export interface StaleRecord {
  id: string;
  hrid: string;
  expected_revision: number;
  current_revision: number;
}

/** A lab list's option a reorder named that changed, came or went since it was read: a
 * revision is null where it wasn't, or isn't, in the list. */
export interface StaleOption {
  code: string;
  expected_revision: number | null;
  current_revision: number | null;
}

export function isProblem(value: unknown): value is Problem {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Partial<Record<keyof Problem, unknown>>;
  return (
    typeof candidate.status === "number" &&
    typeof candidate.code === "string" &&
    typeof candidate.detail === "string"
  );
}

/** An API call that answered with a problem. */
export class ApiError extends Error {
  readonly problem: Problem;

  constructor(problem: Problem) {
    super(problem.detail);
    this.name = "ApiError";
    this.problem = problem;
  }

  get status(): number {
    return this.problem.status;
  }

  get code(): string {
    return this.problem.code;
  }
}

/**
 * `If-Match` for the revision you read (ADR 0289): every change to something with a
 * revision sends it, so it can't overwrite a change made since.
 *
 * ```ts
 * await iki.PATCH("/api/v1/entities/{ref}", { params: { path: { ref }, header: ifMatch(7) }, body });
 * ```
 */
export function ifMatch(revision: number): { "If-Match": string } {
  if (!Number.isInteger(revision) || revision < 1) {
    throw new RangeError(`A revision is a positive whole number, not ${String(revision)}.`);
  }
  return { "If-Match": `"${String(revision)}"` };
}

/** `If-None-Match: *`, for a change made where you read nothing recorded (an amount's first
 * count). */
export function ifNoneMatchAny(): { "If-None-Match": "*" } {
  return { "If-None-Match": "*" };
}

/** Whether a change was refused because what it named changed since it was read (412
 * `stale`): nothing changed. Read it again, then decide. */
export function isStale(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 412;
}

/** Whether a change was refused for not naming the revision it replaces (428
 * `precondition_required`). */
export function isPreconditionRequired(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 428;
}

export interface ClientOptions {
  /** Where Iki is, such as `https://iki.example.com`. Empty means this page's origin. */
  baseUrl?: string;
  /** An OAuth access token, for programs. */
  accessToken?: () => string | undefined | Promise<string | undefined>;
  /** The page's CSRF token, for Iki's own pages signed in with the session. */
  csrfToken?: () => string | undefined;
  fetch?: typeof globalThis.fetch;
}

export type IkiClient = Client<paths>;

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const SIGNED_DOWNLOADS = new Set([
  "/api/v1/bundles/{export_id}/content",
  "/api/v1/files/{file_id}/content",
  "/api/v1/runs/{ref}/artifacts/{artifact_id}/content",
]);

/**
 * A name in a plugin, such as `iki/protein.Protein`, for a path: the `/` between its
 * namespace and the rest goes as it is (ADR 0113), as the API's own links write it.
 * Anything else is escaped whole, so it can't reach another endpoint.
 */
export function encodeName(name: string): string {
  const segments = name.split("/");
  if (segments.length === 2 && segments.every((s) => s !== "" && s !== "." && s !== "..")) {
    return segments.map(encodeURIComponent).join("/");
  }
  return encodeURIComponent(name);
}

/** The API's path params that are names in a plugin. */
const NAME_PARAMS = ["name"];

const serializePath: PathSerializer = (pathname, params) => {
  let path = pathname;
  for (const key of NAME_PARAMS) {
    const value = params[key];
    if (typeof value === "string") {
      path = path.replace(`{${key}}`, encodeName(value));
    }
  }
  return defaultPathSerializer(path, params);
};

export function createIkiClient(options: ClientOptions = {}): IkiClient {
  const client = createClient<paths>({
    baseUrl: options.baseUrl ?? "",
    pathSerializer: serializePath,
    credentials: options.accessToken ? "omit" : "same-origin",
    ...(options.fetch ? { fetch: options.fetch } : {}),
  });
  const redirectedReads = new WeakSet<Request>();
  const sign: Middleware = {
    async onRequest({ request }) {
      const token = await options.accessToken?.();
      if (token) {
        request.headers.set("Authorization", `Bearer ${token}`);
      }
      const csrf = options.csrfToken?.();
      if (csrf && !SAFE_METHODS.has(request.method)) {
        request.headers.set("X-CSRFToken", csrf);
      }
      if (request.redirect !== "follow") return request;
      // The browser can enforce its page origin on every hop, including for a CSRF header.
      // Pages read cross-origin storage using /content-url, or navigate a download link.
      const page = (globalThis as { location?: { origin: string } }).location;
      if (page?.origin === new URL(request.url).origin) {
        return new Request(request, { mode: "same-origin" });
      }
      // Programs have no page origin. Inspect one read redirect where Fetch exposes it.
      // A browser calling another origin can't inspect Location on an opaque redirect.
      if (request.method === "GET" || request.method === "HEAD") {
        const read = new Request(request, { redirect: "manual" });
        redirectedReads.add(read);
        return read;
      }
      return new Request(request, { redirect: "error" });
    },
    async onResponse({ request, response, schemaPath, options: { fetch } }) {
      if (!redirectedReads.has(request)) return response;
      if (response.type === "opaqueredirect")
        throw new Error("Iki's redirect could not be checked.");
      const location = response.headers.get("Location");
      if (!location || ![301, 302, 303, 307, 308].includes(response.status)) return response;
      await response.body?.cancel();
      const origin = new URL(request.url);
      const next = new URL(location, origin);
      const download = request.method === "GET" && SIGNED_DOWNLOADS.has(schemaPath);
      if (
        !["http:", "https:"].includes(next.protocol) ||
        (origin.protocol === "https:" && next.protocol === "http:") ||
        next.username ||
        next.password ||
        (!download && next.origin !== origin.origin)
      )
        throw new Error("Iki returned an unsafe redirect.");
      return fetch(
        new Request(next, {
          method: request.method,
          signal: request.signal,
          credentials: download ? "omit" : request.credentials,
          ...(download ? {} : { headers: request.headers }),
          redirect: "error",
        }),
      );
    },
  };
  client.use(sign);
  return client;
}

/**
 * The response's data, or an {@link ApiError} for its problem. Use it to turn
 * openapi-fetch's `{ data, error }` into a value or an exception:
 *
 * ```ts
 * const project = unwrap(await iki.GET("/api/v1/projects/{project_id}", …));
 * ```
 */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.error !== undefined || !result.response.ok) {
    const problem = isProblem(result.error)
      ? result.error
      : {
          type: "about:blank",
          title: result.response.statusText || "Error",
          status: result.response.status,
          detail: `The server answered HTTP ${String(result.response.status)}.`,
          code: "unexpected_response",
        };
    throw new ApiError(problem);
  }
  return result.data as T;
}
