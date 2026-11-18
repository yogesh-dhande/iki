/**
 * My work (ADR 0122): everything assigned to you across your projects, soonest due first;
 * and the work open to your lab roles, which nobody is assigned. Filter by project, type
 * and state. It follows live updates: work assigned to you while it's open shows up.
 */

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useId } from "react";

import { problemOf, projectsQuery, useApiMutation } from "../api";
import { recordSearch, typeLabel, typesQuery, type EntityType } from "../entities";
import { usePageTitle } from "../layout";
import { AFTER_ASSIGNING, dueText, personLabel, take, workQuery, type WorkItem } from "../people";
import { Badge, Button, Loading, PageHeader, ProblemAlert, SelectField, useMessages } from "../ui";

export interface WorkSearch {
  open?: boolean | undefined;
  project?: string | undefined;
  type?: string | undefined;
  state?: string | undefined;
  sort?: "due" | "updated" | undefined;
}

export function validateWorkSearch(search: Record<string, unknown>): WorkSearch {
  const out: WorkSearch = {};
  if (search["open"] === true || search["open"] === "true") out.open = true;
  for (const key of ["project", "type", "state"] as const) {
    const value = search[key];
    if (typeof value === "string" && value && value.length <= 200) out[key] = value;
  }
  if (search["sort"] === "updated") out.sort = "updated";
  return out;
}

/** Each type with assignment fields, and the states any of them has. */
function assignedTypes(types: EntityType[] | undefined): EntityType[] {
  return (types ?? []).filter((t) => !t.abstract && t.fields.some((f) => f.assignment));
}

export function Work() {
  usePageTitle("My work");
  const search = useSearch({ from: "/work" });
  const navigate = useNavigate({ from: "/work" });
  const types = useQuery(typesQuery).data;
  const projects = useQuery(projectsQuery).data;
  const work = useInfiniteQuery(workQuery(search));
  const items = work.data?.pages.flatMap((p) => p.items) ?? [];
  const assigned = assignedTypes(types);
  const chosenType = assigned.find((t) => t.name === search.type);
  const states = chosenType
    ? chosenType.states
    : [...new Map(assigned.flatMap((t) => t.states).map((s) => [s.name, s] as const)).values()];
  const set = (change: Partial<WorkSearch>) => {
    void navigate({ search: (was) => ({ ...was, ...change }), replace: true });
  };

  return (
    <>
      <PageHeader title="My work" />
      <nav className="tabs" aria-label="Show">
        <Link
          to="/work/"
          search={{ ...search, open: undefined }}
          activeOptions={{ includeSearch: false }}
          activeProps={search.open ? {} : { "aria-current": "page" }}
        >
          Assigned to me
        </Link>
        <Link
          to="/work/"
          search={{ ...search, open: true }}
          activeOptions={{ includeSearch: false }}
          activeProps={search.open ? { "aria-current": "page" } : {}}
        >
          Open to my lab roles
        </Link>
      </nav>
      <div className="toolbar" role="group" aria-label="Filters">
        <SelectField
          label="Project"
          value={search.project ?? ""}
          onChange={(value) => {
            set({ project: value || undefined });
          }}
          options={[
            { value: "", label: "All projects" },
            ...(projects ?? [])
              .filter((p) => p.my_role)
              .map((p) => ({ value: p.id, label: p.name })),
          ]}
        />
        <SelectField
          label="Type"
          value={search.type ?? ""}
          onChange={(value) => {
            set({ type: value || undefined, state: undefined });
          }}
          options={[
            { value: "", label: "All types" },
            ...assigned.map((t) => ({ value: t.name, label: t.label_plural })),
          ]}
        />
        {states.length ? (
          <SelectField
            label="State"
            value={search.state ?? ""}
            onChange={(value) => {
              set({ state: value || undefined });
            }}
            options={[
              { value: "", label: "Any state" },
              ...states.map((s) => ({ value: s.name, label: s.label })),
            ]}
          />
        ) : null}
        <SelectField
          label="Sort"
          value={search.sort ?? "due"}
          onChange={(value) => {
            set({ sort: value === "updated" ? "updated" : undefined });
          }}
          options={[
            { value: "due", label: "Soonest due first" },
            { value: "updated", label: "Latest changed first" },
          ]}
        />
      </div>
      {work.isPending ? (
        <Loading label="Loading your work…" />
      ) : work.isError ? (
        <ProblemAlert problem={problemOf(work.error)} />
      ) : items.length === 0 ? (
        <section className="card empty">
          {search.open ? (
            <p>Nothing is open to your lab roles{filtered(search) ? " here" : ""}.</p>
          ) : (
            <>
              <h2>Nothing’s assigned to you{filtered(search) ? " here" : ""}</h2>
              <p>
                When someone names you on a record, such as a sample’s analyst, it’s here while it’s
                your work, and you’re told.
              </p>
            </>
          )}
        </section>
      ) : (
        <>
          <ul className="rows" aria-label={search.open ? "Open work" : "Your work"}>
            {items.map((item) => (
              <WorkRow key={item.entity.id} item={item} types={types} open={search.open ?? false} />
            ))}
          </ul>
          {work.hasNextPage ? (
            <p>
              <Button
                busy={work.isFetchingNextPage}
                busyText="Loading…"
                onClick={() => void work.fetchNextPage()}
              >
                Show more
              </Button>
            </p>
          ) : null}
        </>
      )}
    </>
  );
}

function filtered(search: WorkSearch): boolean {
  return Boolean(search.project ?? search.type ?? search.state);
}

function WorkRow({
  item,
  types,
  open,
}: {
  item: WorkItem;
  types: EntityType[] | undefined;
  open: boolean;
}) {
  const { entity } = item;
  const id = useId();
  const { say } = useMessages();
  // Open work is taken here, as on the record's page: it's a revision, as any edit.
  const taking = useApiMutation((field: string) => take(entity, field), AFTER_ASSIGNING);
  const type = types?.find((t) => t.name === entity.type);
  const fields = item.fields.map(
    (name) => type?.fields.find((f) => f.name === name)?.label ?? name,
  );
  const due = item.due ? dueText(item.due) : null;
  const others = open
    ? []
    : (type?.fields ?? [])
        .filter((f) => f.assignment && !item.fields.includes(f.name))
        .flatMap((f) => {
          const value = entity.values[f.name];
          if (typeof value !== "string") return [];
          return [`${f.label}: ${personLabel(value, entity)}`];
        });
  return (
    <li className="row">
      <div className="row-main">
        <span className="row-title" id={`${id}-title`}>
          <Link to="/entities/$ref/" params={{ ref: entity.hrid }} search={recordSearch(entity)}>
            {entity.title !== entity.hrid ? `${entity.title} (${entity.hrid})` : entity.hrid}
          </Link>{" "}
          {entity.state ? <Badge muted>{entity.state.label}</Badge> : null}
          {due?.late ? (
            <>
              {" "}
              <Badge>Overdue</Badge>
            </>
          ) : null}
        </span>
        <span className="row-meta">
          {[
            typeLabel(types, entity.type),
            entity.owner_project?.name,
            open ? `Open as ${fields.join(" and ")}` : `You’re its ${fields.join(" and ")}`,
            due ? `Due ${due.text}` : undefined,
            ...others,
          ]
            .filter(Boolean)
            .join(" · ")}
        </span>
        {taking.error ? <ProblemAlert problem={problemOf(taking.error)} /> : null}
      </div>
      {open ? (
        <div className="row-actions">
          {item.fields.map((name, index) => (
            <Button
              key={name}
              describedBy={`${id}-title`}
              label={
                item.fields.length > 1
                  ? `Take as ${fields[index] ?? name}: ${entity.hrid}`
                  : `Take ${entity.hrid} as ${fields[index] ?? name}`
              }
              busy={taking.isPending && taking.variables === name}
              busyText="Taking…"
              onClick={() => {
                taking.mutate(name, {
                  onSuccess: () => {
                    say(`${entity.hrid} is yours, as ${fields[index] ?? name}.`);
                  },
                });
              }}
            >
              {item.fields.length > 1 ? `Take as ${fields[index] ?? name}` : "Take"}
            </Button>
          ))}
        </div>
      ) : null}
    </li>
  );
}
