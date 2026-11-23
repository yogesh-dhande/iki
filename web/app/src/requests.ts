/**
 * Requests between projects (ADR 0132): a project asks another's service for work, such
 * as a core facility's assay, by sending it a record of a request type. The asking project
 * owns it and writes its own fields; the service writes the fields its type names as the
 * service's, once it's sent. Its results are the service's until it releases them back.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions, useQuery } from "@tanstack/react-query";

import { iki, type Project } from "./api";
import {
  searchEntities,
  ENTITIES,
  typesQuery,
  validateEntitySearch,
  type Entity,
  type EntitySearch,
  type EntityType,
  type FieldDef,
} from "./entities";
import { formFields } from "./entity-values";
import type { Filter } from "./filters";
import { pageReadSignal } from "./page-reads";

export type Intake = Schemas["IntakeOut"];
export type Service = Schemas["ServiceOut"];
export type RequestSides = Schemas["RequestSidesOut"];
export type TwoSides = Schemas["RequestOut"];

export function intakeQuery(projectId: string) {
  return queryOptions({
    queryKey: ["projects", projectId, "intake"],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/projects/{project_id}/intake", {
          params: { path: { project_id: projectId } },
          signal: pageReadSignal(signal),
        }),
      ),
  });
}

/** The services a project may send requests of `type` to. */
export function servicesQuery(projectId: string, type: string) {
  return queryOptions({
    queryKey: ["projects", projectId, "services", type],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/projects/{project_id}/services", {
          params: { path: { project_id: projectId }, query: { type } },
        }),
      ),
  });
}

/** A request's two sides: what it sent the service, and its results. */
export function twoSidesQuery(ref: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "request", ref.toUpperCase()],
    queryFn: async () =>
      unwrap(await iki.GET("/api/v1/entities/{ref}/request", { params: { path: { ref } } })),
  });
}

/** A project's Requests tab: the requests sent to it as a service, or those it sent. */
export type Side = "received" | "sent";

/** The Requests tab's address: `?side=`, and its grid's search as the Entities tab's,
 * without a collection. */
export interface RequestsSearch extends EntitySearch {
  side?: Side | undefined;
}

export function validateRequestsSearch(search: Record<string, unknown>): RequestsSearch {
  const found: RequestsSearch = validateEntitySearch(search);
  delete found.collection;
  if (search["side"] === "received" || search["side"] === "sent") found.side = search["side"];
  return found;
}

/** The condition that finds a service's queue: the requests sent to it. */
export function sentTo(projectId: string): Filter {
  return { field: "sent_to", op: "eq", value: projectId };
}

/** The conditions that find the requests a project sent: its own, sent to a service. */
export function sentBy(projectId: string): Filter[] {
  return [
    { field: "owner_project", op: "eq", value: projectId },
    { field: "sent_to", op: "is_set", value: true },
  ];
}

/** The services a project sent requests to, by name. */
export function sentToQuery(projectId: string) {
  return queryOptions({
    queryKey: ["projects", projectId, "sent-to"],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/projects/{project_id}/sent-to", {
          params: { path: { project_id: projectId } },
          signal: pageReadSignal(signal),
        }),
      ),
  });
}

/** The projects that sent a project requests, as its service, by name. */
export function sentFromQuery(projectId: string) {
  return queryOptions({
    queryKey: ["projects", projectId, "sent-from"],
    queryFn: async () =>
      unwrap(
        await iki.GET("/api/v1/projects/{project_id}/sent-from", {
          params: { path: { project_id: projectId } },
        }),
      ),
  });
}

/** Whether the project owns any record of a request type, sent or not: one per type. */
export function ownsRequestsQuery(projectId: string, types: readonly string[]) {
  return queryOptions({
    queryKey: [...ENTITIES, "owns-requests", projectId, types],
    queryFn: async ({ signal }) => {
      const read = pageReadSignal(signal);
      const found = await Promise.all(
        types.map(async (type) =>
          unwrap(
            await searchEntities(
              {
                project: projectId,
                type,
                exact_type: false,
                limit: 1,
                where: { field: "owner_project", op: "eq", value: projectId },
              },
              read,
            ),
          ),
        ),
      );
      return found.some((page) => page.items.length > 0);
    },
  });
}

/**
 * A project's two sides as to requests (ADR 0132): `received`, as a service that takes
 * them or was sent some; `sent`, as one that asks, with requests of its own. Its Requests
 * tab shows when either holds. Only its members can tell.
 */
export function useRequestSides(project: Project) {
  const types = useQuery(typesQuery).data;
  // A request type and its subtypes are found together: ask of the topmost ones.
  const requests = (types ?? []).filter((t) => t.request);
  const names = requests
    .filter((t) => !requests.some((parent) => parent.name === t.extends))
    .map((t) => t.name);
  // Without a request type in the deployment, no project takes or sends a request: ask
  // nothing more.
  const enabled = project.can.read && names.length > 0;
  const intake = useQuery({ ...intakeQuery(project.id), enabled });
  const queued = useQuery({ ...queuedQuery(project.id), enabled });
  const sentTo = useQuery({ ...sentToQuery(project.id), enabled });
  const owns = useQuery({ ...ownsRequestsQuery(project.id, names), enabled });
  const all = [intake, queued, sentTo, owns];
  return {
    received: Boolean(intake.data?.types.length) || queued.data === true,
    sent: Boolean(sentTo.data?.length) || owns.data === true,
    // A query that isn't asked yet, or wasn't allowed, isn't loading.
    ready: types !== undefined && all.every((q) => !q.isLoading),
    intake: intake.data,
  };
}

/** Whether any request was sent to the project, as far as its reader can read. */
export function queuedQuery(projectId: string) {
  return queryOptions({
    queryKey: [...ENTITIES, "queued", projectId],
    queryFn: async ({ signal }) =>
      unwrap(
        await searchEntities(
          {
            project: projectId,
            exact_type: false,
            limit: 1,
            where: sentTo(projectId) as Schemas["Condition"],
          },
          pageReadSignal(signal),
        ),
      ).items.length > 0,
  });
}

/** The installed request types, by label: the ones a service can take. */
export function requestTypes(types: EntityType[]): EntityType[] {
  return types.filter((t) => t.request).sort((a, b) => a.label.localeCompare(b.label));
}

/** Whether a field of a request is the service's to write. */
export function isServiceField(sides: Pick<RequestSides, "service_fields">, name: string): boolean {
  return sides.service_fields.includes(name);
}

/**
 * The fields a form offers: for a request (ADR 0132), only those of the sides the reader
 * writes it as. A new request is the asking project's alone: no one writes the service's
 * fields before it's sent.
 */
export function writableFields(type: EntityType, entity?: Entity): FieldDef[] {
  const fields = formFields(type);
  const service = new Set(entity?.request?.service_fields ?? type.request?.service_fields ?? []);
  if (!service.size) return fields;
  const yours = entity?.request?.yours ?? ["asker"];
  return fields.filter((f) => yours.includes(service.has(f.name) ? "service" : "asker"));
}

/** Labels of a type's fields, by name, in a list as people read it: "Samples and Protein". */
export function fieldLabels(type: EntityType, names: readonly string[]): string {
  const labels = names.map((n) => type.fields.find((f) => f.name === n)?.label ?? n);
  return listed(labels);
}

/** "a", "a and b", "a, b and c". */
export function listed(words: readonly string[]): string {
  if (words.length <= 1) return words.join("");
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1] ?? ""}`;
}
