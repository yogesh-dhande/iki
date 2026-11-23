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
  /** For `guard_refused`: the plugin's guard (ADR 0128) that refused the state. */
  guard?: string;
  /** On a storage observation conflict, the affected lines and their current revisions. */
  affected_lines?: { line_id: string; line_revision: number }[];
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
