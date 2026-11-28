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

/** An RFC 9457 problem: how every error under `/api/` is described (ADR 0027). It's the
 * OpenAPI document's `Problem`, a union discriminated by `code` (ADR 0290): narrow it with
 * {@link isProblem} to read the members a code carries. */
export type Problem = Schemas["Problem"] | ClientProblem;

/** A problem the client describes, where Iki answered with none: `unexpected_response`
 * for an answer that isn't a problem, such as a proxy's error page, with its status;
 * `network_error` (status 0) for no answer at all. */
export interface ClientProblem {
  type: "about:blank";
  title: string;
  status: number;
  detail: string;
  code: "unexpected_response" | "network_error";
  errors?: Schemas["ProblemError"][];
  request_id?: string;
}

/** Every code a problem may have. */
export type ProblemCode = Problem["code"];

/** The problem a code names, with the members it carries. */
export type ProblemOf<C extends ProblemCode> = Problem extends infer P
  ? P extends { code: infer K }
    ? C extends K
      ? Omit<P, "code"> & { code: C }
      : never
    : never
  : never;

/** A record a reassignment named that changed since it was read. */
export type StaleRecord = Schemas["StaleRecord"];

/** A lab list's option a reorder named that changed, came or went since it was read: a
 * revision is null where it wasn't, or isn't, in the list. */
export type StaleOption = Schemas["StaleOption"];

/** True when `value` has the shape of a problem the API answered with, and, given a
 * `code`, is that code's problem, whose members it then has:
 *
 * ```ts
 * if (isProblem(body, "scan_ambiguous")) choose(body.candidates);
 * ```
 */
export function isProblem(value: unknown): value is Problem;
export function isProblem<C extends ProblemCode>(value: unknown, code: C): value is ProblemOf<C>;
export function isProblem(value: unknown, code?: ProblemCode): boolean {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Partial<Record<keyof Problem, unknown>>;
  return (
    typeof candidate.status === "number" &&
    typeof candidate.code === "string" &&
    typeof candidate.detail === "string" &&
    (code === undefined || candidate.code === code)
  );
}

/** An API call that answered with a problem. Its `problem` is the {@link Problem} union,
 * so `instanceof` gives a typed problem to narrow by `code`; {@link isApiError} with a
 * code narrows it in one step. */
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

  get code(): ProblemCode {
    return this.problem.code;
  }
}

/** An {@link ApiError} whose problem is a code's, with the members that code carries. */
export type ApiErrorOf<C extends ProblemCode> = ApiError & {
  readonly problem: ProblemOf<C>;
  readonly code: C;
};

/** Whether `error` is an API call's problem, and, given a `code`, that code's, whose
 * members its `problem` then has. */
export function isApiError(error: unknown): error is ApiError;
export function isApiError<C extends ProblemCode>(error: unknown, code: C): error is ApiErrorOf<C>;
export function isApiError(error: unknown, code?: ProblemCode): boolean {
  return error instanceof ApiError && (code === undefined || error.code === code);
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
export function isStale(error: unknown): error is ApiErrorOf<"stale"> {
  return isApiError(error, "stale");
}

/** Whether a change was refused for not naming the revision it replaces (428
 * `precondition_required`). */
export function isPreconditionRequired(
  error: unknown,
): error is ApiErrorOf<"precondition_required"> {
  return isApiError(error, "precondition_required");
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
    const problem: Problem = isProblem(result.error)
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
