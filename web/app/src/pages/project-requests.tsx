/**
 * A project's Requests tab (ADR 0132). As a service, the requests other projects sent it:
 * its queue, by status, assignee and asking project. As a project that asks, the requests
 * it sent, by status and service. A project that's both has both, a switch apart. Each is
 * the project's grid, held to its requests by fixed conditions.
 */

import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";

import type { Project } from "../api";
import { typesQuery, type EntitySearch, type EntityType } from "../entities";
import { askedByField, sentToField } from "../filters";
import { usePageTitle } from "../layout";
import {
  requestTypes,
  sentBy,
  sentFromQuery,
  sentTo,
  sentToQuery,
  useRequestSides,
  type Intake,
  type RequestsSearch,
  type Side,
} from "../requests";
import { Loading } from "../ui";
import { useProject } from "./project-overview";
import { takesWhat } from "./project-intake";
import { EntityGrid } from "./project-entities";

const REQUESTS = { one: "request", many: "requests" };

export function ProjectRequestsPage() {
  const project = useProject();
  usePageTitle(`Requests · ${project.name}`);
  const search = useSearch({ from: "/projects/$slug/requests" });
  const navigate = useNavigate({ from: "/projects/$slug/requests" });
  const types = useSuspenseQuery(typesQuery).data;
  const sides = useRequestSides(project);
  if (!project.can.read) {
    return <p className="empty">Only the project’s members can see its requests.</p>;
  }
  if (!sides.ready) return <Loading label="Finding its requests…" />;
  if (!sides.received && !sides.sent && !search.side) {
    return (
      <section className="card">
        <h2 className="section-title">No requests</h2>
        <p>
          {project.name} hasn’t sent requests to a service, nor does it take any. To ask a service,
          such as a core facility, make a record of a request type and send it from its page.
        </p>
        <p>
          {project.can.manage
            ? "To take requests from other projects, choose what it takes in "
            : "Its Admins choose what it takes from other projects, in its settings."}
          {project.can.manage ? (
            <>
              <Link to="/projects/$slug/settings/" params={{ slug: project.slug }}>
                Settings
              </Link>
              .
            </>
          ) : null}
        </p>
      </section>
    );
  }
  const side: Side = search.side ?? (sides.received ? "received" : "sent");
  // Each side keeps its own place in the address; switching starts the other afresh.
  const change = (update: (prev: EntitySearch) => EntitySearch, replace?: boolean) => {
    void navigate({
      search: (prev: RequestsSearch) => ({ ...update(prev), side: prev.side }),
      ...(replace ? { replace } : {}),
    });
  };
  return (
    <>
      {sides.received && sides.sent ? (
        <nav className="segmented" aria-label="Which requests">
          {(
            [
              ["received", "Sent to us"],
              ["sent", "We sent"],
            ] as const
          ).map(([value, label]) => (
            <Link
              key={value}
              to="/projects/$slug/requests/"
              params={{ slug: project.slug }}
              search={{ side: value }}
              // The side it opens on is current without `?side=` too.
              inactiveProps={side === value ? { "aria-current": "page" } : {}}
            >
              {label}
            </Link>
          ))}
        </nav>
      ) : null}
      {side === "received" ? (
        <Received
          key="received"
          project={project}
          types={types}
          intake={sides.intake}
          search={search}
          change={change}
        />
      ) : (
        <Sent key="sent" project={project} types={types} search={search} change={change} />
      )}
    </>
  );
}

interface SideProps {
  project: Project;
  types: EntityType[];
  search: RequestsSearch;
  change: (update: (prev: EntitySearch) => EntitySearch, replace?: boolean) => void;
}

/** The service's queue: what other projects sent it, of the types it takes. */
function Received({
  project,
  types,
  intake,
  search,
  change,
}: SideProps & { intake: Intake | undefined }) {
  const from = useQuery(sentFromQuery(project.id)).data ?? [];
  const taken = new Set(intake?.types.map((t) => t.name));
  // What it takes; or, once it takes none, any type it was sent.
  const choices = taken.size ? types.filter((t) => taken.has(t.name)) : requestTypes(types);
  return (
    <>
      <p className="muted">
        {intake ? `${takesWhat(project, intake, types)} ` : ""}Each request stays its asking
        project’s; {project.name} fills in the fields that are the service’s.
        {project.can.manage && !project.archived ? (
          <>
            {" "}
            <Link to="/projects/$slug/settings/" params={{ slug: project.slug }}>
              Choose what it takes
            </Link>
          </>
        ) : null}
      </p>
      <EntityGrid
        project={project}
        types={types}
        search={search}
        change={change}
        fixed={[sentTo(project.id)]}
        choices={choices}
        noun={REQUESTS}
        extra={[askedByField(from)]}
        sides={{ always: true, omit: "sent_to" }}
        empty={(narrowed) =>
          narrowed
            ? `No requests sent to ${project.name} match.`
            : `No requests were sent to ${project.name} yet.`
        }
      />
    </>
  );
}

/** What the project asked of services: its own requests, sent. */
function Sent({ project, types, search, change }: SideProps) {
  const services = useQuery(sentToQuery(project.id)).data ?? [];
  return (
    <>
      <p className="muted">
        The requests {project.name} sent to services. Each stays {project.name}’s; the service fills
        in the fields that are its own. To send another, make it, then send it from its page.
      </p>
      <EntityGrid
        project={project}
        types={types}
        search={search}
        change={change}
        fixed={sentBy(project.id)}
        choices={requestTypes(types)}
        noun={REQUESTS}
        extra={[sentToField(services, true)]}
        sides={{ always: true, omit: "asked_by" }}
        making
        empty={(narrowed) =>
          narrowed
            ? `No requests ${project.name} sent match.`
            : `${project.name} hasn’t sent a request yet.`
        }
      />
    </>
  );
}
