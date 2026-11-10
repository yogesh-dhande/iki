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

import createClient, { type Client, type Middleware } from "openapi-fetch";

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
  /** For `validation_failed`: which fields, and why. */
  errors?: { detail: string; pointer?: string; parameter?: string }[];
  request_id?: string;
}

/** True when ``value`` has the shape of a problem the API answered with. */
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

export function createIkiClient(options: ClientOptions = {}): IkiClient {
  const client = createClient<paths>({
    baseUrl: options.baseUrl ?? "",
    credentials: options.accessToken ? "omit" : "same-origin",
    ...(options.fetch ? { fetch: options.fetch } : {}),
  });
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
      return request;
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
