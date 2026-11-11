/**
 * Collections (core.Collection, ADR 0038): named sets of entities. A static
 * one lists its members; a dynamic one is a search, run as whoever reads it.
 *
 * - The grid saves its search as a collection: kept up to date (the search),
 *   or a fixed list of what it finds now. The server runs it, as the caller.
 * - A collection's page says what it holds, links to its members, and
 *   freezes a dynamic one into a fixed list.
 * - The grid shows a collection's members (?collection=COL-000001), from
 *   every project the reader can read, with the same filters and sorting.
 */

import { ApiError, unwrap, type Schemas } from "@ikisci/sdk";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useId, useState } from "react";

import { iki, problemOf, useApiMutation, type Project } from "../api";
import {
  ENTITIES,
  entityProjectsQuery,
  entityQuery,
  typeLabel,
  type Entity,
  type EntitySearch,
  type EntityType,
} from "../entities";
import { Button, ConfirmDialog, RadioField, TextField, useMessages } from "../ui";

type Keep = "search" | "members";

export const COLLECTION = "core.Collection";

/** Collections' own fields, which their page shows in its own way. */
export const COLLECTION_FIELDS = new Set(["members", "search"]);

export function isCollection(type: EntityType | undefined, types: EntityType[]): boolean {
  let current = type;
  while (current) {
    if (current.name === COLLECTION) return true;
    const parent = current.extends;
    current = types.find((t) => t.name === parent);
  }
  return false;
}

/** A dynamic collection's search, as it keeps it. */
export interface StoredSearch {
  project?: string;
  type?: string;
  archived?: boolean | null;
  q?: string;
  where?: unknown;
  sort?: string;
  descending?: boolean;
}

/** The grid's search, kept by a dynamic collection; ``project`` limits it to one. */
export function toStoredSearch(search: EntitySearch, project?: string): StoredSearch {
  const show = search.show ?? "current";
  return {
    ...(project ? { project } : {}),
    ...(search.type ? { type: search.type } : {}),
    ...(show === "all" ? {} : { archived: show === "archived" }),
    ...(search.q ? { q: search.q } : {}),
    ...(search.filters?.length ? { where: { all: search.filters } } : {}),
    ...(search.sort ? { sort: search.sort, ...(search.desc ? { descending: true } : {}) } : {}),
  };
}

function storedSearch(entity: Entity): StoredSearch | null {
  const value = entity.values["search"];
  return value && typeof value === "object" ? value : null;
}

function members(entity: Entity): string[] {
  const value = entity.values["members"];
  return Array.isArray(value) ? (value as string[]) : [];
}

/** The grid's address for a collection's members: its type's columns, in its order. */
export function membersAddress(entity: Entity): EntitySearch {
  const stored = storedSearch(entity);
  if (!stored) return { collection: entity.hrid };
  return {
    collection: entity.hrid,
    ...(stored.type ? { type: stored.type } : {}),
    // Its own search says which it holds: current, archived or both.
    show: "all",
    ...(stored.sort ? { sort: stored.sort, ...(stored.descending ? { desc: true } : {}) } : {}),
  };
}

/** The grid's button: save what it shows as a collection in this project. */
export function SaveAsCollection({ project, search }: { project: Project; search: EntitySearch }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [here, setHere] = useState(true);
  const [keep, setKeep] = useState<Keep>("search");
  const hereId = useId();
  const navigate = useNavigate();
  const { say } = useMessages();
  const create = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/collections", {
          body: {
            project: project.id,
            name: name.trim(),
            ...(description.trim() ? { description: description.trim() } : {}),
            search: toStoredSearch(
              search,
              here ? project.id : undefined,
            ) as Schemas["CollectionSearch"],
            keep,
            reason: "",
          },
        }),
      ),
    [ENTITIES],
  );
  const close = () => {
    setOpen(false);
    setName("");
    setDescription("");
    setHere(true);
    setKeep("search");
    create.reset();
  };
  return (
    <>
      <Button
        onClick={() => {
          setOpen(true);
        }}
      >
        Save as collection
      </Button>
      <ConfirmDialog
        isOpen={open}
        title="Save as a collection"
        confirmLabel="Save collection"
        confirmVariant="primary"
        busy={create.isPending}
        problem={create.error ? problemOf(create.error) : undefined}
        onCancel={close}
        onConfirm={() => {
          create.mutate(undefined, {
            onSuccess: (made) => {
              close();
              void navigate({ to: "/entities/$ref/", params: { ref: made.hrid } }).then(() => {
                say(`Saved the collection ${made.hrid}.`);
              });
            },
          });
        }}
      >
        <p>
          A collection is an entity of its own, which people can find, link and use. Each person
          sees only the members they can read.
        </p>
        <TextField label="Name" value={name} onChange={setName} maxLength={200} />
        <RadioField<Keep>
          label="Keep"
          value={keep}
          onChange={setKeep}
          options={[
            {
              value: "search",
              label: "The search",
              description: "it stays up to date, with new matches as they’re registered",
            },
            {
              value: "members",
              label: "These entities",
              description: "a fixed list of what it finds now, to cite or run on (up to 10,000)",
            },
          ]}
        />
        <TextField
          label="Description"
          value={description}
          onChange={setDescription}
          maxLength={10000}
          optional
          multiline
        />
        <label className="choice" htmlFor={hereId}>
          <input
            id={hereId}
            type="checkbox"
            checked={here}
            onChange={(event) => {
              setHere(event.target.checked);
            }}
          />
          <span className="choice-text">
            Only entities in {project.name}
            <span className="choice-help">
              {keep === "search"
                ? "Otherwise it finds matches in every project its readers can read."
                : "Otherwise it takes matches from every project you can read."}
            </span>
          </span>
        </label>
      </ConfirmDialog>
    </>
  );
}

/** Above the grid when it shows a collection's members. */
export function CollectionBanner({ reference, project }: { reference: string; project: Project }) {
  const found = useQuery(entityQuery(reference));
  const entity = found.data;
  const stored = entity ? storedSearch(entity) : null;
  return (
    <div className="alert alert-info collection-banner" role="note">
      <p>
        Members of{" "}
        {entity ? (
          <Link to="/entities/$ref/" params={{ ref: entity.hrid }}>
            {entity.title}
          </Link>
        ) : (
          <code>{reference}</code>
        )}
        {entity ? (stored ? ": everything its search finds" : ": the entities it lists") : ""}, from
        every project you can read.
      </p>
      <Link to="/projects/$slug/entities/" params={{ slug: project.slug }}>
        Show all of {project.name}’s entities
      </Link>
    </div>
  );
}

/** On a collection's page: what it holds, and a link to its members. */
export function CollectionMembers({ entity, types }: { entity: Entity; types: EntityType[] }) {
  const projects = useQuery(entityProjectsQuery(entity.hrid)).data;
  const slug = entity.owner_project?.slug ?? projects?.[0]?.project.slug;
  const stored = storedSearch(entity);
  const listed = members(entity);
  const conditions = stored?.where ? countConditions(stored.where) : 0;
  return (
    <section className="card" aria-label="Members">
      <h2 className="section-title">Members</h2>
      {stored ? (
        <>
          <p>
            Everything this search finds when it’s opened. Each person sees only the members they
            can read.
          </p>
          <dl className="summary">
            <dt>Type</dt>
            <dd>{stored.type ? typeLabel(types, stored.type) : "Any type"}</dd>
            <dt>Where</dt>
            <dd>{stored.project ? "In one project" : "In every project its reader can read"}</dd>
            {stored.q ? (
              <>
                <dt>Text</dt>
                <dd>“{stored.q}”</dd>
              </>
            ) : null}
            {conditions ? (
              <>
                <dt>Conditions</dt>
                <dd>{conditions === 1 ? "1 condition" : `${String(conditions)} conditions`}</dd>
              </>
            ) : null}
            <dt>Archived</dt>
            <dd>
              {stored.archived === true
                ? "Only archived entities"
                : stored.archived === false
                  ? "Not included"
                  : "Included"}
            </dd>
          </dl>
        </>
      ) : (
        <p>
          {listed.length === 0
            ? "It lists no entities yet. Edit it to add some."
            : `It lists ${listed.length === 1 ? "1 entity" : `${String(listed.length)} entities`}. You see those you can read.`}
        </p>
      )}
      {slug && (stored || listed.length) ? (
        <p>
          <Link to="/projects/$slug/entities/" params={{ slug }} search={membersAddress(entity)}>
            Show its members
          </Link>
        </p>
      ) : null}
      {stored && entity.can.edit && !entity.archived ? <Freeze entity={entity} /> : null}
    </section>
  );
}

/** Replaces a dynamic collection's search with what it finds now, on the server. */
function Freeze({ entity }: { entity: Entity }) {
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const client = useQueryClient();
  const { say } = useMessages();
  const freeze = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/collections/{ref}/freeze", {
          params: {
            path: { ref: entity.id },
            header: { "If-Match": `"${String(entity.revision)}"` },
          },
          body: { reason: reason.trim() },
        }),
      ),
    [ENTITIES],
  );
  const close = () => {
    setAsking(false);
    setReason("");
    freeze.reset();
  };
  const stale = freeze.error instanceof ApiError && freeze.error.problem.code === "stale";
  return (
    <>
      <Button
        onClick={() => {
          setAsking(true);
        }}
      >
        Freeze as a fixed list
      </Button>
      <ConfirmDialog
        isOpen={asking}
        title={`Freeze ${entity.title}?`}
        confirmLabel="Freeze"
        confirmVariant="primary"
        busy={freeze.isPending}
        problem={
          stale && freeze.error
            ? {
                ...freeze.error.problem,
                detail:
                  "Someone changed it while this was open. The page now shows their change: check it, then try again.",
              }
            : freeze.error
              ? problemOf(freeze.error)
              : undefined
        }
        onCancel={close}
        onConfirm={() => {
          freeze.mutate(undefined, {
            onSuccess: (saved) => {
              client.setQueryData(entityQuery(saved.hrid).queryKey, saved);
              close();
              const count = Array.isArray(saved.values["members"])
                ? saved.values["members"].length
                : 0;
              say(
                `Froze ${saved.hrid}: it lists ${count === 1 ? "1 entity" : `${String(count)} entities`}.`,
              );
            },
            onError: (error) => {
              if (error.problem.code === "stale") {
                void client.invalidateQueries({ queryKey: ENTITIES });
              }
            },
          });
        }}
      >
        <p>
          Its search is replaced by the entities it finds for you now, in its order, so it stops
          changing. A run or a paper can then cite exactly those. Its history keeps the search.
        </p>
        <TextField label="Reason" value={reason} onChange={setReason} maxLength={2000} optional />
      </ConfirmDialog>
    </>
  );
}

function countConditions(node: unknown): number {
  if (!node || typeof node !== "object") return 0;
  const record = node as Record<string, unknown>;
  for (const key of ["all", "any"] as const) {
    const children = record[key];
    if (Array.isArray(children))
      return children.reduce((n: number, c) => n + countConditions(c), 0);
  }
  if ("not" in record) return countConditions(record["not"]);
  return "field" in record ? 1 : 0;
}
