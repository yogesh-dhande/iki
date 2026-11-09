/**
 * TypeScript client for the Iki API.
 *
 * The generated API client arrives with the React host. For now the package
 * exposes the API version it targets.
 */

/** The REST API version this client speaks, matching the server's `/api/v1` prefix. */
export const API_VERSION = "v1";

/** Builds the path of an API endpoint, for example `apiPath("projects")` → `/api/v1/projects`. */
export function apiPath(resource: string): string {
  const trimmed = resource.replace(/^\/+|\/+$/g, "");
  if (trimmed === "") {
    throw new Error("apiPath: resource must not be empty");
  }
  return `/api/${API_VERSION}/${trimmed}`;
}
