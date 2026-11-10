/**
 * The API, as the app uses it: one client signed in with the browser session,
 * and TanStack Query hooks for each resource.
 */

import {
  ApiError,
  createIkiClient,
  unwrap,
  type IkiClient,
  type Problem,
  type Schemas,
} from "@ikisci/sdk";
import { QueryClient, queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";

export type Me = Schemas["MeOut"];
export type Project = Schemas["ProjectOut"];
export type Member = Schemas["ProjectMemberOut"];
export type TeamRef = Schemas["TeamRef"];
export type Role = Schemas["ProjectRoleOut"];
export type RoleName = Role["name"];

function csrfToken(): string | undefined {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content;
}

export const iki: IkiClient = createIkiClient({
  baseUrl: window.location.origin,
  csrfToken,
  // Looked up on each call, not captured once.
  fetch: (input, init) => globalThis.fetch(input, init),
});

let signingIn = false;

/** Signed out (the session ended, or the person was deactivated): sign in again, then come back. */
export function signInAgain(): void {
  // Every failed query reports the 401; one navigation is enough.
  if (signingIn) return;
  signingIn = true;
  const next = window.location.pathname + window.location.search;
  window.location.assign(`/login/?next=${encodeURIComponent(next)}`);
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: (failures, error) =>
        !(error instanceof ApiError && error.status < 500) && failures < 2,
    },
  },
});

queryClient.getQueryCache().subscribe((event) => {
  const error: unknown = event.query.state.error;
  if (error instanceof ApiError && error.status === 401) {
    signInAgain();
  }
});

/** Every page of a list, following `next_cursor`. */
async function everything<T>(
  fetchPage: (cursor: string | undefined) => Promise<{ items: T[]; next_cursor?: string | null }>,
): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | undefined;
  do {
    const page = await fetchPage(cursor);
    items.push(...page.items);
    cursor = page.next_cursor ?? undefined;
  } while (cursor);
  return items;
}

const byName = <T extends { name: string }>(a: T, b: T) => a.name.localeCompare(b.name);

export const meQuery = queryOptions({
  queryKey: ["me"],
  queryFn: async () => unwrap(await iki.GET("/api/v1/me")),
  staleTime: 5 * 60_000,
});

export const rolesQuery = queryOptions({
  queryKey: ["project-roles"],
  queryFn: async () => unwrap(await iki.GET("/api/v1/project-roles")),
  staleTime: Infinity,
});

export const projectsQuery = queryOptions({
  queryKey: ["projects"],
  queryFn: () =>
    everything(async (cursor) =>
      unwrap(
        await iki.GET("/api/v1/projects", {
          params: { query: { limit: 100, ...(cursor ? { cursor } : {}) } },
        }),
      ),
    ),
});

export function projectQuery(slug: string) {
  return queryOptions({
    queryKey: ["projects", slug],
    queryFn: async () => {
      const found = unwrap(await iki.GET("/api/v1/projects", { params: { query: { slug } } }));
      const project = found.items[0];
      if (!project) {
        throw new ApiError({
          type: "about:blank",
          title: "Not Found",
          status: 404,
          detail: "There’s no such project, or you can’t see it.",
          code: "not_found",
        });
      }
      return project;
    },
  });
}

export function membersQuery(projectId: string) {
  return queryOptions({
    queryKey: ["projects", projectId, "members"],
    queryFn: () =>
      everything(async (cursor) =>
        unwrap(
          await iki.GET("/api/v1/projects/{project_id}/members", {
            params: {
              path: { project_id: projectId },
              query: { limit: 100, ...(cursor ? { cursor } : {}) },
            },
          }),
        ),
      ),
  });
}

export function grantableTeamsQuery(projectId: string) {
  return queryOptions({
    queryKey: ["projects", projectId, "grantable-teams"],
    queryFn: async () =>
      (
        await everything(async (cursor) =>
          unwrap(
            await iki.GET("/api/v1/projects/{project_id}/grantable-teams", {
              params: {
                path: { project_id: projectId },
                query: { limit: 100, ...(cursor ? { cursor } : {}) },
              },
            }),
          ),
        )
      ).sort(byName),
  });
}

export const creatableTeamsQuery = queryOptions({
  queryKey: ["teams", "can-create-projects"],
  queryFn: async () =>
    (
      await everything(async (cursor) =>
        unwrap(
          await iki.GET("/api/v1/teams", {
            params: {
              query: { can_create_projects: true, limit: 100, ...(cursor ? { cursor } : {}) },
            },
          }),
        ),
      )
    ).sort(byName),
});

/** Looks a person or service account up by email address, for adding them to a project. */
export async function findPerson(email: string) {
  const found = unwrap(
    await iki.GET("/api/v1/users", { params: { query: { email: email.trim() } } }),
  );
  return found.items[0];
}

/** The problem an API call failed with, or a generic one for network errors. */
export function problemOf(error: unknown): Problem {
  if (error instanceof ApiError) {
    return error.problem;
  }
  return {
    type: "about:blank",
    title: "Network error",
    status: 0,
    detail: "Iki couldn’t be reached. Check your connection and try again.",
    code: "network_error",
  };
}

/** Field errors from a `validation_failed` problem, by body field name. */
export function fieldErrors(problem: Problem | undefined): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const error of problem?.errors ?? []) {
    const field = error.pointer?.replace(/^#\//, "") ?? error.parameter;
    if (field && !(field in errors)) {
      errors[field] = error.detail;
    }
  }
  return errors;
}

/** A mutation that refreshes the given queries when it succeeds. */
export function useApiMutation<TVariables, TResult>(
  mutationFn: (variables: TVariables) => Promise<TResult>,
  invalidate: readonly (readonly unknown[])[],
) {
  const client = useQueryClient();
  return useMutation<TResult, ApiError, TVariables>({
    mutationFn,
    onSuccess: async () => {
      await Promise.all(invalidate.map((queryKey) => client.invalidateQueries({ queryKey })));
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 401) {
        signInAgain();
      }
    },
  });
}
