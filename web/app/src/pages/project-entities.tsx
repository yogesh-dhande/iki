/**
 * A project's Entities tab: the one table of records (ADR 0176), held to the project, with
 * the same type pills as All entities, counting the project's records, and its library
 * besides: its collections, saved searches and new entities.
 */

import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";

import { typesQuery } from "../entities";
import { sentToField } from "../filters";
import { usePageTitle } from "../layout";
import { sentToQuery } from "../requests";
import { EntityGrid } from "./entity-grid";
import { useProject } from "./project-overview";

/** The project's entities: every type, its collections and saved searches. */
export function ProjectEntities() {
  const project = useProject();
  const search = useSearch({ from: "/projects/$slug/entities" });
  const navigate = useNavigate({ from: "/projects/$slug/entities" });
  const types = useSuspenseQuery(typesQuery).data;
  const type = types.find((t) => t.name === search.type);
  usePageTitle(`${type ? type.label_plural : "Entities"} · ${project.name}`);
  // Requests (ADR 0132) are found by the service they were sent to: this project, as a
  // service, or one it sent requests to.
  const sentTo = useQuery({ ...sentToQuery(project.id), enabled: project.can.read }).data ?? [];
  const requests = type ? Boolean(type.request) : types.some((t) => t.request);
  return (
    <EntityGrid
      project={project}
      types={types}
      search={search}
      change={(update, replace) => {
        void navigate({ search: update, ...(replace ? { replace } : {}) });
      }}
      extra={requests ? [sentToField([project, ...sentTo.filter((p) => p.id !== project.id)])] : []}
      library
      pills
    />
  );
}
