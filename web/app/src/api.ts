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
import {
  infiniteQueryOptions,
  QueryClient,
  queryOptions,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { useCallback } from "react";

import { BACKGROUND_HEADER, isIdle } from "./activity";
import { fetchPageRead, pageReadSignal, runPageQuery } from "./page-reads";

export type Me = Schemas["MeOut"];
export type Project = Schemas["ProjectOut"];
export type Member = Schemas["ProjectMemberOut"];
export type TeamRef = Schemas["TeamRef"];
export type Role = Schemas["ProjectRoleOut"];
export type RoleName = Role["name"];
export type LabRole = Schemas["LabRoleOut"];
export type LabRoleGrant = Schemas["LabRoleGrantOut"];
export type Notification = Schemas["NotificationOut"];
export type Webhook = Schemas["WebhookOut"];
export type WebhookCreated = Schemas["WebhookCreated"];
export type Delivery = Schemas["DeliveryOut"];
export type DeliverySummary = Schemas["DeliverySummary"];
export type InventoryWebhook = Schemas["InventoryOut"];
export type WebhookScope = "project" | "org";

function csrfToken(): string | undefined {
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content;
}

export const iki: IkiClient = createIkiClient({
  baseUrl: window.location.origin,
  csrfToken,
  // Page data uses GET or HEAD by default. A query that sends JSON can select
  // fetchPageRead too. An independent operation such as a download supplies its own fetch.
  fetch: (input, init) => {
    const method = init?.method ?? (input instanceof Request ? input.method : "GET");
    return method === "GET" || method === "HEAD"
      ? fetchPageRead(input, init)
      : globalThis.fetch(input, init);
  },
});

// What the page asks for by itself doesn't keep the session open (ADR 0153).
iki.use({
  onRequest({ request }) {
    if (isIdle()) request.headers.set(BACKGROUND_HEADER, "1");
    return request;
  },
});

let signingIn = false;
let signedOut = false;
const signedOutListeners = new Set<() => void>();

/**
 * Found out in the background (the live-update stream, the bell's count) that
 * the session ended: say so, with a link to sign in, but don't leave the page,
 * which could hold something half typed.
 */
export function markSignedOut(): void {
  if (signedOut) return;
  signedOut = true;
  for (const listener of signedOutListeners) listener();
}

export function isSignedOut(): boolean {
  return signedOut;
}

export function onSignedOutChange(listener: () => void): () => void {
  signedOutListeners.add(listener);
  return () => {
    signedOutListeners.delete(listener);
  };
}

/** For tests. */
export function resetSignedOut(): void {
  signedOut = false;
  signingIn = false;
}

/** Where to sign in to come back to this page. */
export function signInUrl(): string {
  const next = window.location.pathname + window.location.search;
  return `/login/?next=${encodeURIComponent(next)}`;
}

/** Signed out (the session ended, or the person was deactivated): sign in again, then come back. */
export function signInAgain(): void {
  // Every failed query reports the 401; one navigation is enough.
  if (signingIn) return;
  signingIn = true;
  window.location.assign(signInUrl());
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // TanStack passes ordinary and infinite query executions through this hook.
      persister: (read, context) =>
        runPageQuery(
          () => read(context),
          () => context.signal,
        ),
      staleTime: 30_000,
      retry: (failures, error) =>
        !(error instanceof ApiError && error.status < 500) && failures < 2,
    },
  },
});

queryClient.getQueryCache().subscribe((event) => {
  const error: unknown = event.query.state.error;
  if (error instanceof ApiError && error.status === 401) {
    if (event.query.meta?.["background"]) {
      markSignedOut();
    } else {
      signInAgain();
    }
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

/** The lab roles installed plugins declare (ADR 0116); none unless a plugin declares some. */
export const labRolesQuery = queryOptions({
  queryKey: ["lab-roles"],
  queryFn: async () => unwrap(await iki.GET("/api/v1/lab-roles")),
  staleTime: Infinity,
});

/** Every project the reader can see, each page fetched in turn: for choosing one in a
 * form. Browsing them is a page at a time, with {@link projectPagesQuery}. */
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

/** The projects the reader can see as people browse them, a page at a time ("Show
 * more"): archived ones last, then by team and name, in the server's order. `member` keeps
 * those they have a role in, and `archived: false` the active ones. */
export function projectPagesQuery(filter: { member?: boolean; archived?: boolean } = {}) {
  return infiniteQueryOptions({
    queryKey: ["projects", "pages", filter],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/projects", {
          params: {
            query: {
              sort: "name",
              limit: 50,
              ...filter,
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** Whether there are active projects the reader sees but has no role in, as an Org Admin
 * does: one row is enough to say so. */
export const othersProjectsQuery = queryOptions({
  queryKey: ["projects", "others"],
  queryFn: async () =>
    unwrap(
      await iki.GET("/api/v1/projects", {
        params: { query: { member: false, archived: false, limit: 1 } },
      }),
    ).items.length > 0,
});

/** Whether the reader can see a project, by its slug: an empty list if not, rather than a
 * refusal, as a request's sides may each be a project the other can't open. */
export function seesProjectQuery(slug: string) {
  return queryOptions({
    queryKey: ["projects", "sees", slug],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/projects", { params: { query: { slug, limit: 1 } } })).items
        .length > 0,
  });
}

/** One project, by ID: what a run, batch or workflow run says it's in. */
export function projectByIdQuery(id: string) {
  return queryOptions({
    queryKey: ["projects", "by-id", id],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/projects/{project_id}", { params: { path: { project_id: id } } }),
      ),
  });
}

/** One project, by its slug. Its key says "by-slug": a slug is whatever people name a
 * project, so it mustn't share a place with the other answers kept under "projects". */
export function projectQuery(slug: string) {
  return queryOptions({
    queryKey: ["projects", "by-slug", slug],
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

// --- Notifications ---------------------------------------------------------------

/** Every notification query: invalidated together when live updates say something changed. */
export const NOTIFICATIONS = ["notifications"] as const;

/** The header bell's count. Live updates keep it current (see events.ts). */
export const unreadQuery = queryOptions({
  queryKey: [...NOTIFICATIONS, "summary"],
  // On every page, refreshed in the background: a 401 here isn't the person's doing.
  meta: { background: true },
  queryFn: async () => unwrap(await iki.GET("/api/v1/notifications/summary")).unread,
});

export type InboxFilter = "unread" | "all";

/** The inbox, newest first, a page at a time ("Show older"). */
export function inboxQuery(filter: InboxFilter) {
  return infiniteQueryOptions({
    queryKey: [...NOTIFICATIONS, "list", filter],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/notifications", {
          params: {
            query: {
              limit: 50,
              ...(filter === "unread" ? { unread: true } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export async function markNotification(id: string, read: boolean): Promise<Notification> {
  return unwrap(
    await iki.PATCH("/api/v1/notifications/{notification_id}", {
      params: { path: { notification_id: id } },
      body: { read },
    }),
  );
}

/** Marks read what arrived by `upTo` (the newest the person has seen). */
export async function markAllNotifications(upTo: string | undefined) {
  return unwrap(await iki.POST("/api/v1/notifications/read", { body: { up_to: upTo ?? null } }));
}

export type Watching = Schemas["WatchOut"];
export type WatchMode = Watching["mode"];
export type NotificationSettings = Schemas["SettingsOut"];
export type NotifyBy = Schemas["KindOut"]["delivery"];

/** Whether you watch a record, or muted it (ADR 0129). */
export function watchQuery(ref: string) {
  return queryOptions({
    queryKey: [...NOTIFICATIONS, "watch", ref],
    meta: { background: true },
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/entities/{ref}/watch", {
          params: { path: { ref } },
          signal: pageReadSignal(signal),
        }),
      ),
  });
}

export async function chooseWatch(ref: string, mode: WatchMode): Promise<Watching> {
  return unwrap(
    await iki.PUT("/api/v1/entities/{ref}/watch", { params: { path: { ref } }, body: { mode } }),
  );
}

/** How you're told of each kind of notification, everywhere and in single projects. */
export const notificationSettingsQuery = queryOptions({
  queryKey: [...NOTIFICATIONS, "settings"],
  meta: { background: true },
  queryFn: async () => unwrap(await iki.GET("/api/v1/me/notification-settings")),
});

/** Chooses how you're told of kinds in one project; null follows your choice everywhere. */
export async function chooseInProject(
  project: string,
  choices: { kind: string; delivery: NotifyBy | null }[],
): Promise<NotificationSettings> {
  return unwrap(
    await iki.PATCH("/api/v1/me/notification-settings", {
      body: { projects: choices.map((c) => ({ project, ...c })) },
    }),
  );
}

// --- Webhooks ----------------------------------------------------------------------

export const WEBHOOKS = ["webhooks"] as const;

export function webhookEventsQuery(scope: WebhookScope) {
  return queryOptions({
    queryKey: ["webhook-events", scope],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/webhook-events", { params: { query: { scope } } })),
    staleTime: Infinity,
  });
}

export const webhookPolicyQuery = queryOptions({
  queryKey: [...WEBHOOKS, "policy"],
  queryFn: async () => unwrap(await iki.GET("/api/v1/webhook-policy")),
});

/** A project's webhooks, or the organization's (no project). */
export function webhooksQuery(projectId: string | undefined) {
  return queryOptions({
    queryKey: [...WEBHOOKS, "list", projectId ?? "org"],
    queryFn: () =>
      everything(async (cursor) => {
        const query = { limit: 100, ...(cursor ? { cursor } : {}) };
        return projectId
          ? unwrap(
              await iki.GET("/api/v1/projects/{project_id}/webhooks", {
                params: { path: { project_id: projectId }, query },
              }),
            )
          : unwrap(await iki.GET("/api/v1/webhooks", { params: { query } }));
      }),
  });
}

export const inventoryQuery = queryOptions({
  queryKey: [...WEBHOOKS, "inventory"],
  queryFn: () =>
    everything(async (cursor) =>
      unwrap(
        await iki.GET("/api/v1/project-webhooks", {
          params: { query: { limit: 100, ...(cursor ? { cursor } : {}) } },
        }),
      ),
    ),
});

export function webhookQuery(webhookId: string) {
  return queryOptions({
    queryKey: [...WEBHOOKS, "one", webhookId],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/webhooks/{webhook_id}", {
          params: { path: { webhook_id: webhookId } },
        }),
      ),
  });
}

export function deliveriesQuery(webhookId: string) {
  return infiniteQueryOptions({
    queryKey: [...WEBHOOKS, "deliveries", webhookId],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await iki.GET("/api/v1/webhooks/{webhook_id}/deliveries", {
          params: {
            path: { webhook_id: webhookId },
            query: { limit: 25, ...(pageParam ? { cursor: pageParam } : {}) },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export function deliveryQuery(webhookId: string, deliveryId: string) {
  return queryOptions({
    queryKey: [...WEBHOOKS, "delivery", deliveryId],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/webhooks/{webhook_id}/deliveries/{delivery_id}", {
          params: { path: { webhook_id: webhookId, delivery_id: deliveryId } },
        }),
      ),
  });
}

/** Looks a person or service account up by email address, for adding them to a project. */
export async function findPerson(email: string) {
  const found = unwrap(
    await iki.GET("/api/v1/users", {
      // The membership change needs this person's ID before it can submit the PUT.
      fetch: globalThis.fetch,
      params: { query: { email: email.trim() } },
    }),
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

/** What to do once a mutation ends: after the queries it refreshes have refetched. */
export interface MutateCallbacks<TResult, TVariables> {
  onSuccess?: (result: TResult, variables: TVariables) => void;
  onError?: (error: ApiError, variables: TVariables) => void;
}

/**
 * A mutation that refreshes the given queries when it succeeds.
 *
 * Its `mutate` calls back even if the component that called it has gone, as it
 * often has: the refreshed data can replace it (an approved run's banner, a
 * deleted row). TanStack Query's own `mutate` drops callbacks then, so a
 * "Saved" message would sometimes never show.
 */
export function useApiMutation<TVariables, TResult>(
  mutationFn: (variables: TVariables) => Promise<TResult>,
  invalidate: readonly (readonly unknown[])[],
) {
  const client = useQueryClient();
  const mutation = useMutation<TResult, ApiError, TVariables>({
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
  const { mutateAsync } = mutation;
  const mutate = useCallback(
    (variables: TVariables, callbacks?: MutateCallbacks<TResult, TVariables>) => {
      mutateAsync(variables).then(
        (result) => callbacks?.onSuccess?.(result, variables),
        (error: unknown) => {
          // The mutation's state holds it too, for the component to show.
          if (error instanceof ApiError) callbacks?.onError?.(error, variables);
        },
      );
    },
    [mutateAsync],
  );
  return { ...mutation, mutate };
}
