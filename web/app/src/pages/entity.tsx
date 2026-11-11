/**
 * An entity's page: its values, each with who set it, how and when; the
 * projects it's in; and its history, revision by revision.
 */

import { ApiError, unwrap } from "@ikisci/sdk";
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
  useSuspenseQuery,
} from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useState } from "react";

import { iki, problemOf, useApiMutation } from "../api";
import {
  ENTITIES,
  entityProjectsQuery,
  entityQuery,
  formatSize,
  personName,
  provenanceQuery,
  quantity,
  revisionQuery,
  revisionsQuery,
  typesQuery,
  valueText,
  type Entity,
  type EntityType,
  type FieldDef,
  type Revision,
} from "../entities";
import { usePageTitle } from "../layout";
import { COLLECTION_FIELDS, CollectionMembers, isCollection } from "./collections";
import {
  Badge,
  Button,
  buttonClass,
  ConfirmDialog,
  Loading,
  PageHeader,
  RelativeTime,
  TextField,
  useMessages,
} from "../ui";

const VIA: Record<string, string> = {
  web: "in the web app",
  api: "through the API",
  mcp: "by an AI assistant (MCP)",
  cli: "from the command line",
  scim: "by provisioning",
  system: "by Iki",
};

/** How a revision was made; a run's links to the run. */
function Via({ revision }: { revision: Revision }) {
  if (revision.via === "run") {
    return revision.run ? (
      <>
        by{" "}
        <Link to="/runs/$ref/" params={{ ref: revision.run.hrid }}>
          {revision.run.hrid}
        </Link>
      </>
    ) : (
      <>by a run in a project you can’t see</>
    );
  }
  return <>{VIA[revision.via] ?? revision.via}</>;
}

const KIND: Record<string, string> = {
  created: "Created",
  edited: "Edited",
  archived: "Archived",
  restored: "Restored",
};

/** The entity the page is about, from the `$ref` in its address. */
export function useEntity(): { entity: Entity; type: EntityType | undefined; types: EntityType[] } {
  const { ref } = useParams({ from: "/entities/$ref" });
  const entity = useSuspenseQuery(entityQuery(ref)).data;
  const types = useSuspenseQuery(typesQuery).data;
  return { entity, type: types.find((t) => t.name === entity.type), types };
}

/** One value as it reads on the page: references link, files download, units show. */
export function Value({
  field,
  value,
  entity,
}: {
  field: FieldDef;
  value: unknown;
  entity: Pick<Entity, "refs" | "files">;
}) {
  if (value === null || value === undefined) {
    return <span className="muted">Not set</span>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="muted">None</span>;
    const item: FieldDef = { ...field, type: field.items ?? "string" };
    return (
      <ul className="value-list">
        {(value as unknown[]).map((v, index) => (
          // Items can repeat; their position tells them apart.
          <li key={index}>
            <Value field={item} value={v} entity={entity} />
          </li>
        ))}
      </ul>
    );
  }
  switch (field.type) {
    case "ref": {
      const found = entity.refs[value as string];
      if (!found) return <span className="muted">An entity you can’t read</span>;
      return (
        <span>
          <Link to="/entities/$ref/" params={{ ref: found.hrid }}>
            {found.title}
          </Link>
          {found.title !== found.hrid ? <span className="muted"> {found.hrid}</span> : null}
          {found.archived ? <span className="muted"> (archived)</span> : null}
        </span>
      );
    }
    case "file": {
      const found = entity.files[value as string];
      if (!found) return <span className="muted">A file you can’t read</span>;
      return (
        <span>
          <a href={`/api/v1/files/${value as string}/content`} download={found.name}>
            {found.name}
          </a>{" "}
          <span className="muted">
            {formatSize(found.size)} · {found.media_type}
          </span>
        </span>
      );
    }
    case "quantity": {
      const { shown, stored } = quantity(field, value as number);
      return (
        <span>
          {shown}
          {shown !== stored ? <span className="muted"> ({stored})</span> : null}
        </span>
      );
    }
    case "text":
      return <span className="preserve-lines">{value as string}</span>;
    case "json":
      return (
        <pre className="code">
          <code>{JSON.stringify(value, null, 2)}</code>
        </pre>
      );
    default:
      return <>{valueText(field, value, entity)}</>;
  }
}

function Provenance({ revision }: { revision: Revision | undefined }) {
  if (!revision) return null;
  const who = personName(revision.user);
  return (
    <span className="cell-meta">
      {revision.kind === "created" ? "Set" : "Changed"} by {who}{" "}
      <RelativeTime value={revision.created_at} /> <Via revision={revision} />
      {revision.reason ? <>: “{revision.reason}”</> : null} (revision {revision.number})
    </span>
  );
}

/** Edit, archive and restore, for Editors in its owner project. */
function EntityActions({ entity, type }: { entity: Entity; type: EntityType | undefined }) {
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const { say } = useMessages();
  const client = useQueryClient();
  const archiving = !entity.archived;
  const change = useApiMutation(
    async () =>
      unwrap(
        await iki.PATCH("/api/v1/entities/{ref}", {
          params: {
            path: { ref: entity.id },
            header: { "If-Match": `"${String(entity.revision)}"` },
          },
          body: { archived: archiving, reason: reason.trim() },
        }),
      ),
    [ENTITIES],
  );
  const running =
    !entity.archived && entity.owner_project ? (
      <Link
        to="/projects/$slug/runs/new/"
        params={{ slug: entity.owner_project.slug }}
        search={{ entities: entity.hrid }}
        className={buttonClass()}
      >
        Run action
      </Link>
    ) : null;
  if (!entity.can.edit || !type) return running ? <div className="actions">{running}</div> : null;
  const close = () => {
    setAsking(false);
    setReason("");
    change.reset();
  };
  const stale = change.error instanceof ApiError && change.error.problem.code === "stale";
  return (
    <div className="actions">
      {entity.archived ? null : (
        <Link
          to="/entities/$ref/edit/"
          params={{ ref: entity.hrid }}
          className={buttonClass("primary")}
        >
          Edit
        </Link>
      )}
      {running}
      <Button
        {...(archiving ? { variant: "danger" as const } : {})}
        onClick={() => {
          setAsking(true);
        }}
      >
        {archiving ? "Archive" : "Restore"}
      </Button>
      <ConfirmDialog
        isOpen={asking}
        title={`${archiving ? "Archive" : "Restore"} ${entity.hrid}?`}
        confirmLabel={archiving ? "Archive" : "Restore"}
        confirmVariant={archiving ? "danger" : "primary"}
        busy={change.isPending}
        problem={
          stale && change.error
            ? {
                ...change.error.problem,
                detail:
                  "Someone changed it while this was open. The page now shows their change: check it, then try again.",
              }
            : change.error
              ? problemOf(change.error)
              : undefined
        }
        onCancel={close}
        onConfirm={() => {
          change.mutate(undefined, {
            onSuccess: (saved) => {
              client.setQueryData(entityQuery(saved.hrid).queryKey, saved);
              close();
              say(archiving ? `Archived ${saved.hrid}.` : `Restored ${saved.hrid}.`);
            },
            onError: (error) => {
              // Read it again, so trying again sends the revision it's at now.
              if (error.problem.code === "stale") {
                void client.invalidateQueries({ queryKey: ENTITIES });
              }
            },
          });
        }}
      >
        <p>
          {archiving
            ? "It stays in every project it’s in, with its values and history, but it can’t change and lists hide it. You can restore it later."
            : "It can change again, and lists show it again."}
        </p>
        <TextField
          label="Reason"
          value={reason}
          onChange={setReason}
          maxLength={2000}
          optional
          multiline
          help="Why, for the record. It’s kept in its history."
        />
      </ConfirmDialog>
    </div>
  );
}

export function EntityTabs({ entity, type }: { entity: Entity; type: EntityType | undefined }) {
  const tab = { "aria-current": "page" } as const;
  return (
    <>
      <PageHeader
        eyebrow={
          type ? (
            <Link to="/types/$name/" params={{ name: type.name }}>
              {type.label}
            </Link>
          ) : (
            entity.type
          )
        }
        title={entity.title}
        actions={<EntityActions entity={entity} type={type} />}
        badge={
          <>
            {entity.title !== entity.hrid ? <Badge>{entity.hrid}</Badge> : null}
            {entity.archived ? (
              <>
                {" "}
                <Badge muted>Archived</Badge>
              </>
            ) : null}
          </>
        }
      />
      <nav className="tabs" aria-label="Entity">
        <Link
          to="/entities/$ref/"
          params={{ ref: entity.hrid }}
          activeOptions={{ exact: true }}
          activeProps={tab}
        >
          Overview
        </Link>
        <Link to="/entities/$ref/lineage/" params={{ ref: entity.hrid }} activeProps={tab}>
          Lineage
        </Link>
        <Link to="/entities/$ref/history/" params={{ ref: entity.hrid }} activeProps={tab}>
          History
        </Link>
      </nav>
    </>
  );
}

export function EntityOverview() {
  const { entity, type, types } = useEntity();
  usePageTitle(`${entity.title} (${entity.hrid})`);
  const provenance = useQuery(provenanceQuery(entity.hrid)).data;
  const projects = useQuery(entityProjectsQuery(entity.hrid)).data;
  const collection = isCollection(type, types);
  // A collection's members and search have a section of their own.
  const fields = (type?.fields ?? []).filter((f) => !collection || !COLLECTION_FIELDS.has(f.name));
  const set = fields.filter((f) => entity.values[f.name] != null);
  const unset = fields.filter((f) => entity.values[f.name] == null);
  return (
    <>
      <EntityTabs entity={entity} type={type} />
      {entity.archived ? (
        <div className="alert alert-warning" role="note">
          <p>This entity is archived. Its values and history are kept, and it can’t change.</p>
        </div>
      ) : null}
      {!type ? (
        <div className="alert alert-warning" role="note">
          <p>
            Its type, <code>{entity.type}</code>, isn’t installed any more, so its values can’t be
            shown.
          </p>
        </div>
      ) : null}
      <section className="card" aria-labelledby="values-title">
        <h2 className="section-title" id="values-title">
          Values
        </h2>
        {set.length === 0 ? <p className="muted">No values are set.</p> : null}
        <dl className="summary values">
          {set.map((f) => (
            <div key={f.name} className="value-row">
              <dt>
                {f.label}
                {f.computed ? <span className="cell-meta">Computed</span> : null}
              </dt>
              <dd>
                <Value field={f} value={entity.values[f.name]} entity={entity} />
                <Provenance revision={provenance?.[f.name]} />
              </dd>
            </div>
          ))}
        </dl>
        {unset.length ? (
          <p className="muted">Not set: {unset.map((f) => f.label).join(", ")}.</p>
        ) : null}
      </section>
      {collection ? <CollectionMembers entity={entity} types={types} /> : null}
      <section className="card" aria-labelledby="details-title">
        <h2 className="section-title" id="details-title">
          Details
        </h2>
        <dl className="summary">
          <dt>ID</dt>
          <dd>
            <code>{entity.hrid}</code>
          </dd>
          <dt>Owner project</dt>
          <dd>
            {entity.owner_project ? (
              <Link to="/projects/$slug/" params={{ slug: entity.owner_project.slug }}>
                {entity.owner_project.name}
              </Link>
            ) : (
              <span className="muted">A project you can’t see</span>
            )}
          </dd>
          {projects && projects.length > 1 ? (
            <>
              <dt>Also in</dt>
              <dd>
                {projects
                  .filter((p) => !p.owner)
                  .map((p, index) => (
                    <span key={p.project.id}>
                      {index ? ", " : ""}
                      <Link to="/projects/$slug/" params={{ slug: p.project.slug }}>
                        {p.project.name}
                      </Link>
                    </span>
                  ))}
              </dd>
            </>
          ) : null}
          <dt>Created</dt>
          <dd>
            {personName(entity.created_by)},
            <RelativeTime value={entity.created_at} />
          </dd>
          <dt>Last changed</dt>
          <dd>
            <RelativeTime value={entity.updated_at} /> (revision {entity.revision})
          </dd>
        </dl>
      </section>
    </>
  );
}

function Changes({ number, type }: { number: number; type: EntityType | undefined }) {
  const { ref } = useParams({ from: "/entities/$ref" });
  const entity = useSuspenseQuery(entityQuery(ref)).data;
  // Its own refs and files: a value it removed may not be in the entity's any more.
  const detail = useQuery(revisionQuery(entity.hrid, number));
  if (!detail.data) return <Loading label="Loading the changes…" />;
  const fields = new Map((type?.fields ?? []).map((f) => [f.name, f]));
  const changes = Object.entries(detail.data.changes);
  if (changes.length === 0) return <p className="muted">No values changed.</p>;
  return (
    <div className="table-scroll">
      <table className="table">
        <thead>
          <tr>
            <th scope="col">Field</th>
            <th scope="col">Before</th>
            <th scope="col">After</th>
          </tr>
        </thead>
        <tbody>
          {changes.map(([name, change]) => {
            const field = fields.get(name);
            return (
              <tr key={name}>
                <th scope="row">{field?.label ?? name}</th>
                <td>
                  {field ? (
                    <Value field={field} value={change.before} entity={detail.data} />
                  ) : (
                    JSON.stringify(change.before)
                  )}
                </td>
                <td>
                  {field ? (
                    <Value field={field} value={change.after} entity={detail.data} />
                  ) : (
                    JSON.stringify(change.after)
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RevisionRow({ revision, type }: { revision: Revision; type: EntityType | undefined }) {
  const [open, setOpen] = useState(false);
  const who = personName(revision.user);
  const labels = new Map((type?.fields ?? []).map((f) => [f.name, f.label]));
  const changed = revision.changed_fields.map((n) => labels.get(n) ?? n);
  return (
    <li className="row row-stacked">
      <div className="row-main">
        <span className="row-title">
          Revision {revision.number}: {KIND[revision.kind] ?? revision.kind} by {who}
        </span>
        <span className="row-meta">
          <RelativeTime value={revision.created_at} /> <Via revision={revision} />
          {changed.length ? ` · ${changed.join(", ")}` : ""}
        </span>
        {revision.reason ? <p className="row-reason">“{revision.reason}”</p> : null}
        {open ? <Changes number={revision.number} type={type} /> : null}
      </div>
      {revision.changed_fields.length ? (
        <div className="row-actions">
          <Button
            variant="quiet"
            label={`${open ? "Hide" : "Show"} changes in revision ${String(revision.number)}`}
            onClick={() => {
              setOpen(!open);
            }}
          >
            {open ? "Hide changes" : "Show changes"}
          </Button>
        </div>
      ) : null}
    </li>
  );
}

export function EntityHistory() {
  const { entity, type } = useEntity();
  usePageTitle(`History of ${entity.title} (${entity.hrid})`);
  const revisions = useInfiniteQuery(revisionsQuery(entity.hrid));
  const items = revisions.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <>
      <EntityTabs entity={entity} type={type} />
      <p className="muted">
        Every change, newest first. Revisions can’t be changed or removed, by anyone.
      </p>
      {revisions.isPending ? <Loading label="Loading the history…" /> : null}
      <ul className="rows">
        {items.map((r) => (
          <RevisionRow key={r.number} revision={r} type={type} />
        ))}
      </ul>
      {revisions.hasNextPage ? (
        <div className="actions">
          <Button
            busy={revisions.isFetchingNextPage}
            busyText="Loading…"
            onClick={() => void revisions.fetchNextPage()}
          >
            Show older
          </Button>
        </div>
      ) : null}
    </>
  );
}
