/**
 * What a project's page shows besides its tabs (ADR 0176): how many of each type of record
 * it has, what changed in it lately, its latest requests, and in words, each change.
 */

import { ApiError, unwrap, type Problem, type Schemas } from "@ikisci/sdk";
import { queryOptions } from "@tanstack/react-query";

import { BACKGROUND_HEADER, isIdle } from "./activity";
import { iki } from "./api";
import { ENTITIES, personName, type Entity, type EntityType } from "./entities";
import { sentBy, sentTo } from "./requests";

export type ProjectChange = Schemas["ProjectChangeOut"];

/** How many records of each type there are, of those the reader can read. */
export interface EntityCounts {
  types: { type: string; label: string; count: number }[];
  total: number;
}

/**
 * The project's records, counted by type (ADR 0176): `GET /entities/counts?project=`, as the
 * sidebar counts every project's. `null` where the server doesn't count them, so the page
 * leaves the counts out rather than guess.
 */
export function entityCountsQuery(projectId: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "counts", projectId],
    queryFn: async (): Promise<EntityCounts | null> => {
      // Not in the OpenAPI document yet, so asked for as the client would: with the page's
      // idleness (ADR 0153).
      const query = new URLSearchParams({ project: projectId }).toString();
      const response = await globalThis.fetch(
        `${window.location.origin}/api/v1/entities/counts?${query}`,
        {
          credentials: "same-origin",
          headers: {
            Accept: "application/json",
            ...(isIdle() ? { [BACKGROUND_HEADER]: "1" } : {}),
          },
        },
      );
      if (response.status === 404) return null;
      if (!response.ok) {
        throw new ApiError(
          ((await response.json().catch(() => null)) as Problem | null) ?? {
            type: "about:blank",
            title: response.statusText || "Error",
            status: response.status,
            detail: `The server answered HTTP ${String(response.status)}.`,
            code: "unexpected_response",
          },
        );
      }
      return (await response.json()) as EntityCounts;
    },
  });
}

/** The latest changes to the project's records, newest first. */
export function projectChangesQuery(projectId: string, limit = 8) {
  return queryOptions({
    queryKey: [...ENTITIES, "project-changes", projectId, limit],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/projects/{project_id}/changes", {
          params: { path: { project_id: projectId }, query: { limit } },
        }),
      ),
  });
}

/** The project's latest requests, sent to it or by it (ADR 0132), the latest changed first. */
export function latestRequestsQuery(
  projectId: string,
  sides: { received: boolean; sent: boolean },
) {
  return queryOptions({
    queryKey: [...ENTITIES, "latest-requests", projectId, sides],
    queryFn: async (): Promise<Entity[]> => {
      const latest = async (where: Schemas["Condition"]) =>
        unwrap(
          await iki.POST("/api/v1/entities/search", {
            body: {
              project: projectId,
              exact_type: false,
              archived: false,
              limit: LATEST_REQUESTS,
              where,
              sort: { field: "updated_at", descending: true },
            },
          }),
        ).items;
      const found = await Promise.all([
        sides.received ? latest(sentTo(projectId) as Schemas["Condition"]) : [],
        sides.sent ? latest({ all: sentBy(projectId) } as Schemas["Condition"]) : [],
      ]);
      const byId = new Map(found.flat().map((e) => [e.id, e]));
      return [...byId.values()]
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
        .slice(0, LATEST_REQUESTS);
    },
  });
}

/** How many requests the Overview lists. */
export const LATEST_REQUESTS = 5;

/** Who made a change: the person, an agent acting for them, or Iki itself. */
export function changedBy(change: ProjectChange): string {
  if (!change.user) return change.via === "system" ? "Iki" : personName(null);
  const person = personName(change.user);
  if (change.agent) return `${change.agent.name} for ${person}`;
  return person;
}

/** A field's label on its type, or its name. */
function fieldLabel(type: EntityType | undefined, name: string): string {
  return type?.fields.find((f) => f.name === name)?.label ?? name;
}

/**
 * What a change did, as the verb before the record and the words after it: “edited” PLS-1
 * “(name, topology)”; “set Status of” PLS-1 “to Approved, signed”.
 */
export function changeWords(
  change: ProjectChange,
  type: EntityType | undefined,
): { verb: string; after: string } {
  const fields = change.changed_fields.map((f) => fieldLabel(type, f).toLowerCase());
  switch (change.kind) {
    case "created":
      return { verb: "created", after: "" };
    case "edited":
      return { verb: "edited", after: fields.length ? `: ${fields.join(", ")}` : "" };
    case "state_changed": {
      const state = change.state;
      if (!state) return { verb: "set a state of", after: "" };
      const signed = state.signatures.length ? ", signed" : "";
      return { verb: `set ${state.field.label} of`, after: ` to ${state.to.label}${signed}` };
    }
    case "archived":
      return { verb: "archived", after: "" };
    case "restored":
      return { verb: "restored", after: "" };
    case "moved":
      return { verb: "moved", after: " to another registry entry" };
    case "superseded":
      return { verb: "superseded", after: "" };
    case "curated":
      return { verb: "changed the facts of", after: "" };
  }
}
