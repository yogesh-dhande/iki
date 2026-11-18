/**
 * An entity's page: its values, each with who set it, how and when; the
 * projects it's in; and its history, revision by revision.
 */

import { ApiError, unwrap, type Schemas } from "@ikisci/sdk";
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
  useSuspenseQuery,
  type InfiniteData,
  type UseInfiniteQueryResult,
} from "@tanstack/react-query";
import { Link, useNavigate, useParams, useRouterState, useSearch } from "@tanstack/react-router";
import { useState } from "react";

import { Through } from "../agent";
import { iki, problemOf, useApiMutation } from "../api";
import { componentsQuery, entityTabs } from "../components";
import {
  apiRef,
  ENTITIES,
  entityProjectsQuery,
  entityQuery,
  formatSize,
  hasIdentity,
  originsQuery,
  makingQuery,
  bundleOriginsQuery,
  personName,
  provenanceQuery,
  quantity,
  recordSearch,
  revisionQuery,
  revisionsQuery,
  typesQuery,
  valueText,
  type Entity,
  type EntityType,
  type FieldDef,
  type Making,
  type Origin,
  type Ref,
  type Revision,
  type RevisionDetail,
} from "../entities";

import { entryRevisionQuery, entryRevisionsQuery, REGISTRY_MARK } from "../registries";
import { usePageTitle } from "../layout";
import { APP_TYPE, entityAppsQuery } from "../apps";
import { VIEW_TYPE } from "../views";
import { AppPanel, AppSpec } from "./app";
import { COLLECTION_FIELDS, CollectionMembers, isCollection } from "./collections";
import { DetachedNote } from "./types";
import { SavedViewPanel } from "./view";
import { EntityState, StateBadges } from "./entity-state";
import { EntityWork } from "./entity-work";
import {
  Badge,
  Button,
  buttonClass,
  ConfirmDialog,
  Loading,
  PageHeader,
  ProblemAlert,
  RelativeTime,
  TextField,
  Time,
  useMessages,
} from "../ui";

/** What the page after a move shows: the records it changed to refer to the new entry. */
interface MovedState {
  from: string;
  to: string;
  repointed: Ref[];
}

declare module "@tanstack/react-router" {
  interface HistoryState {
    moved?: MovedState;
  }
}

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
  const how = VIA[revision.via] ?? revision.via;
  // The agent it came through, if any: an AI assistant's MCP client, say.
  return (
    <>
      {how}
      <Through agent={revision.agent} />
    </>
  );
}

export const KIND: Record<string, string> = {
  created: "Created",
  edited: "Edited",
  archived: "Archived",
  restored: "Restored",
  curated: "Facts changed",
  superseded: "Superseded",
  moved: "Moved",
  state_changed: "State set",
};

/** What a revision did: its kind, or for a state change (ADR 0125), which field, from
 * which state to which: “Status set to Approved, from In review”. */
export function revisionWhat(revision: Revision): string {
  const s = revision.state;
  if (!s) return KIND[revision.kind] ?? revision.kind;
  const set = `${s.field.label} set to ${s.to.label}`;
  return s.from ? `${set}, from ${s.from.label}` : set;
}

/** The entity the page is about, from the `$ref` in its address, and `?project=` for a
 * record of a registry entry. */
export function useEntity(): { entity: Entity; type: EntityType | undefined; types: EntityType[] } {
  const { ref } = useParams({ from: "/entities/$ref" });
  const { project } = useSearch({ from: "/entities/$ref" });
  const entity = useSuspenseQuery(entityQuery(ref, project)).data;
  const types = useSuspenseQuery(typesQuery).data;
  return { entity, type: types.find((t) => t.name === entity.type), types };
}

/** Whose a field's value is, for a record of a registry entry (ADR 0115). */
export function registryNote(field: FieldDef): string | null {
  return field.registry ? REGISTRY_MARK[field.registry] : null;
}

/** A reference, linked. An entry's records share its HRID, so a link to one opens the
 * record in `project`: the referring record's, which holds what it refers to. */
function RefLink({ found, project }: { found: Ref; project: string | undefined }) {
  const types = useQuery(typesQuery).data;
  const search = project && hasIdentity(types, found.type) ? { project } : {};
  if (found.purged) {
    return (
      <span>
        <Link to="/entities/$ref/" params={{ ref: found.hrid }} search={search}>
          {found.hrid}
        </Link>
        <span className="muted"> (purged)</span>
      </span>
    );
  }
  return (
    <span>
      <Link to="/entities/$ref/" params={{ ref: found.hrid }} search={search}>
        {found.title}
      </Link>
      {found.title !== found.hrid ? <span className="muted"> {found.hrid}</span> : null}
      {found.archived ? <span className="muted"> (archived)</span> : null}
    </span>
  );
}

/** Where a value leads in another system, such as its LIMS record (ADR 0082): its field's
 * link with the value in it, encoded. Only ever https, as the API gives it. */
export function linkTo(field: FieldDef, value: unknown): string | null {
  if (typeof value !== "string" || !field.link?.startsWith("https://")) return null;
  return field.link.replaceAll("{value}", encodeURIComponent(value));
}

/** One value as it reads on the page: references link, files download, units show. */
export function Value({
  field,
  value,
  entity,
}: {
  field: FieldDef;
  value: unknown;
  /** With its owner project, references to registry entries open that project's records. */
  entity: Pick<Entity, "refs" | "files" | "people"> & { owner_project?: { slug: string } | null };
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
  const link = linkTo(field, value);
  if (link) {
    return (
      <a href={link} target="_blank" rel="noreferrer noopener">
        {value as string}
      </a>
    );
  }
  switch (field.type) {
    case "ref": {
      const found = entity.refs[value as string];
      if (!found) return <span className="muted">An entity you can’t read</span>;
      return <RefLink found={found} project={entity.owner_project?.slug} />;
    }
    case "person": {
      const found = entity.people?.[value as string];
      if (!found) return <span className="muted">Someone you can’t see</span>;
      return (
        <span>
          {found.name}
          {found.email && found.email !== found.name ? (
            <span className="muted"> {found.email}</span>
          ) : null}
          {found.active ? null : <span className="muted"> (deactivated)</span>}
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

/** One record an import read it from: which, where, when, by which run, and its checksum. */
function FromBundle({ origin }: { origin: Schemas["BundleOriginOut"] }) {
  const what =
    origin.action === "new"
      ? `${origin.revisions.toLocaleString()} revision${origin.revisions === 1 ? "" : "s"}`
      : `${origin.revisions.toLocaleString()} newer revision${origin.revisions === 1 ? "" : "s"}`;
  return (
    <span>
      {origin.deployment}’s {origin.project}, where it’s {origin.hrid}: {what}
      {origin.applied_at ? (
        <>
          , imported <RelativeTime value={origin.applied_at} />
        </>
      ) : null}
      {origin.applied_by ? <> by {origin.applied_by.name}</> : null}
      {origin.bundle_import ? (
        <>
          {" "}
          (
          <Link
            to="/projects/$slug/imports/$importId/"
            params={{ slug: origin.project_slug, importId: origin.bundle_import }}
          >
            the import
          </Link>
          )
        </>
      ) : null}
    </span>
  );
}

/** One line per run, or per record that says so: what made it, and who says so. */
function madeGroups(rows: Making[]): Making[][] {
  const groups = new Map<string, Making[]>();
  for (const row of rows) {
    const key =
      row.by === "iki"
        ? ["iki", row.run?.hrid ?? ""].join(":")
        : ["source", row.source?.source ?? "", row.source?.id ?? "", row.run?.hrid ?? ""].join(":");
    groups.set(key, [...(groups.get(key) ?? []), row]);
  }
  return [...groups.values()];
}

function tools(rows: Making[]): string {
  return rows
    .filter((r) => r.software)
    .map((r) => (r.version ? `${r.software} ${r.version}` : r.software))
    .join(", ");
}

function MadeHow({ rows }: { rows: Making[] }) {
  const [first] = rows;
  if (!first) return null;
  const kind = first.kind === "experimental" ? "Experimental" : "Computed";
  const what = [first.method, tools(rows)].filter(Boolean).join(", ");
  if (first.by === "iki") {
    return (
      <span>
        {kind} with {what}
        {first.run ? (
          <>
            , by{" "}
            <Link to="/runs/$ref/" params={{ ref: first.run.hrid }}>
              {first.run.hrid}
            </Link>{" "}
            (<code>{first.run.action}</code>)
          </>
        ) : null}
        <span className="cell-meta">Recorded by Iki</span>
      </span>
    );
  }
  const record = first.source
    ? first.source.version && first.source.version !== first.source.id
      ? `${first.source.id} (${first.source.version})`
      : first.source.id
    : "";
  return (
    <span>
      {kind}
      {what ? `: ${what}` : ""}, as {first.source?.source ?? "its source"}
      {record ? (
        <>
          {" "}
          {first.source?.url ? (
            <a href={first.source.url} target="_blank" rel="noreferrer noopener">
              {record}
            </a>
          ) : (
            record
          )}
        </>
      ) : null}{" "}
      states
      <span className="cell-meta">Stated by its source</span>
    </span>
  );
}

function ImportedFrom({ origin }: { origin: Origin }) {
  const record =
    origin.version && origin.version !== origin.id ? `${origin.id} (${origin.version})` : origin.id;
  return (
    <span>
      {origin.source}:{" "}
      {origin.url ? (
        <a href={origin.url} target="_blank" rel="noreferrer noopener">
          {record}
        </a>
      ) : (
        record
      )}
      {origin.file ? (
        <>
          {" "}
          in{" "}
          <a href={`/api/v1/files/${origin.file.id}/content`} download={origin.file.name}>
            {origin.file.name}
          </a>
        </>
      ) : null}
      {origin.retrieved_at ? (
        <>
          , retrieved <RelativeTime value={origin.retrieved_at} />
        </>
      ) : null}
      {origin.run ? (
        <>
          , by{" "}
          <Link to="/runs/$ref/" params={{ ref: origin.run.hrid }}>
            {origin.run.hrid}
          </Link>
        </>
      ) : null}
      {origin.revision === null ? (
        <span className="muted"> (found again: it was already here)</span>
      ) : null}
      {origin.sha256 ? (
        <span className="muted">
          {" "}
          · SHA-256 <code title={origin.sha256}>{origin.sha256.slice(0, 12)}…</code>
        </span>
      ) : null}
    </span>
  );
}

function Provenance({ revision }: { revision: Revision | undefined }) {
  if (!revision) return null;
  const which = `${revision.entry ? "entry revision" : "revision"} ${String(revision.number)}`;
  if (!revision.created_at) {
    // An entry's revision made through a project the reader can't read: what, not who.
    return <span className="cell-meta">Set in the registry entry ({which})</span>;
  }
  const who = personName(revision.user);
  return (
    <span className="cell-meta">
      {revision.kind === "created" ? "Set" : "Changed"} by {who}{" "}
      <RelativeTime value={revision.created_at} /> <Via revision={revision} />
      {revision.reason ? <>: “{revision.reason}”</> : null} ({which})
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
          search={recordSearch(entity)}
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
  const types = useQuery(typesQuery).data ?? [];
  // Plugins' tabs (ADR 0084). If they can't be listed, the page still works without them.
  const plugins = entityTabs(entity.type, types, useQuery(componentsQuery).data ?? []);
  // Apps about it (ADR 0090), likewise.
  const apps = useQuery(entityAppsQuery(apiRef(entity))).data ?? [];
  // The project's record, on every tab: other projects' records have its HRID too.
  const search = recordSearch(entity);
  return (
    <>
      <PageHeader
        eyebrow={
          type ? (
            <Link to="/types/$/" params={{ _splat: type.name }}>
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
            <StateBadges entity={entity} type={type} />
            {entity.archived ? (
              <>
                {" "}
                <Badge muted>Archived</Badge>
              </>
            ) : null}
            {entity.entry?.superseded ? (
              <>
                {" "}
                <Badge muted>Superseded</Badge>
              </>
            ) : null}
            {type?.detached ? (
              <>
                {" "}
                <Badge muted>Detached plugin</Badge>
              </>
            ) : null}
          </>
        }
      />
      <nav className="tabs" aria-label="Entity">
        <Link
          to="/entities/$ref/"
          params={{ ref: entity.hrid }}
          search={search}
          activeOptions={{ exact: true, includeSearch: false }}
          activeProps={tab}
        >
          Overview
        </Link>
        {(type?.viewers ?? []).map((v) => (
          <Link
            key={v.viewer}
            to="/entities/$ref/viewers/$viewer/"
            params={{ ref: entity.hrid, viewer: v.viewer }}
            search={search}
            activeOptions={{ includeSearch: false }}
            activeProps={tab}
          >
            {v.label}
          </Link>
        ))}
        <Link
          to="/entities/$ref/lineage/"
          params={{ ref: entity.hrid }}
          search={search}
          activeOptions={{ includeSearch: false }}
          activeProps={tab}
        >
          Lineage
        </Link>
        <Link
          to="/entities/$ref/history/"
          params={{ ref: entity.hrid }}
          search={search}
          activeOptions={{ includeSearch: false }}
          activeProps={tab}
        >
          History
        </Link>
        {plugins.map(({ component, label }) => (
          <Link
            key={component.name}
            to="/entities/$ref/components/$/"
            params={{ ref: entity.hrid, _splat: component.name }}
            search={search}
            activeOptions={{ includeSearch: false }}
            activeProps={tab}
          >
            {label}
          </Link>
        ))}
        {apps.map((app) => (
          <Link
            key={app.hrid}
            to="/entities/$ref/apps/$app/"
            params={{ ref: entity.hrid, app: app.hrid }}
            search={search}
            activeOptions={{ includeSearch: false }}
            activeProps={tab}
          >
            {/* Two apps of one name are told apart by their IDs. */}
            {apps.filter((a) => a.name === app.name).length > 1
              ? `${app.name} (${app.hrid})`
              : app.name}
          </Link>
        ))}
      </nav>
    </>
  );
}

/** Fields' values, each with who set it; those not set, in a line after. */
function ValueList({
  fields,
  entity,
  provenance,
  marks,
}: {
  fields: FieldDef[];
  entity: Entity;
  provenance: Record<string, Revision> | undefined;
  /** Say how each is kept in its registry entry: identity, derived or fact. */
  marks: boolean;
}) {
  const set = fields.filter((f) => entity.values[f.name] != null);
  const unset = fields.filter((f) => entity.values[f.name] == null);
  return (
    <>
      {set.length === 0 ? <p className="muted">No values are set.</p> : null}
      <dl className="summary values">
        {set.map((f) => {
          const mark = marks ? registryNote(f) : null;
          return (
            <div key={f.name} className="value-row">
              <dt>
                {f.label}
                {mark ? <span className="cell-meta">{mark}</span> : null}
                {f.computed && f.registry !== "derived" ? (
                  <span className="cell-meta">Computed</span>
                ) : null}
              </dt>
              <dd>
                <Value field={f} value={entity.values[f.name]} entity={entity} />
                <Provenance revision={provenance?.[f.name]} />
              </dd>
            </div>
          );
        })}
      </dl>
      {unset.length ? (
        <p className="muted">Not set: {unset.map((f) => f.label).join(", ")}.</p>
      ) : null}
    </>
  );
}

/** A registrar replaced the record's entry (ADR 0115): what with, and for Editors, moving
 * the record there. */
function Superseded({ entity }: { entity: Entity }) {
  const entry = entity.entry;
  const by = entry?.superseded;
  const navigate = useNavigate();
  const client = useQueryClient();
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const [missing, setMissing] = useState(false);
  // Entities are refreshed once the page is the moved record's: refreshing this one first
  // would ask for the old ID in the project, which no longer has it.
  const move = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/entities/{ref}/move", {
          params: { path: { ref: entity.id } },
          body: { reason: reason.trim() },
        }),
      ),
    [],
  );
  if (!entry || !by) return null;
  const project = entity.owner_project?.name ?? "this project";
  const close = () => {
    setAsking(false);
    setReason("");
    setMissing(false);
    move.reset();
  };
  const movable = entity.can.edit && !entity.archived;
  return (
    <div className="alert alert-warning" role="note" aria-labelledby="superseded-title">
      <h2 className="section-title" id="superseded-title">
        {entry.hrid} was superseded by {by.by.hrid}
      </h2>
      <p>
        A registrar of the {entry.registry.name} registry replaced it on <Time value={by.at} />: “
        {by.reason}”. It can’t be registered again.
      </p>
      <p>
        {movable
          ? `Move ${project}’s record to ${by.by.hrid}, so its data is under the right ID.`
          : entity.archived
            ? `This record is archived, so it keeps ${entry.hrid}. Restore it to move it to ${by.by.hrid}.`
            : `An Editor in ${project} can move this record to ${by.by.hrid}.`}
      </p>
      {movable ? (
        <div className="actions">
          <Button
            variant="primary"
            onClick={() => {
              setAsking(true);
            }}
          >
            Move to {by.by.hrid}
          </Button>
        </div>
      ) : null}
      <ConfirmDialog
        isOpen={asking}
        title={`Move to ${by.by.hrid}?`}
        confirmLabel={`Move to ${by.by.hrid}`}
        busy={move.isPending}
        problem={move.error ? problemOf(move.error) : undefined}
        onCancel={close}
        onConfirm={() => {
          if (!reason.trim()) {
            setMissing(true);
            return;
          }
          move.mutate(undefined, {
            onSuccess: (moved) => {
              close();
              void navigate({
                to: "/entities/$ref/",
                params: { ref: moved.hrid },
                search: recordSearch(moved),
                state: (prev) => ({
                  ...prev,
                  moved: { from: entry.hrid, to: moved.hrid, repointed: moved.repointed },
                }),
              }).then(() => client.invalidateQueries({ queryKey: ENTITIES }));
            },
          });
        }}
      >
        <p>
          {project}’s record keeps its values, history and runs, and holds {by.by.hrid} instead of{" "}
          {entry.hrid}. {project}’s own records that refer to {entry.hrid} will refer to{" "}
          {by.by.hrid}, each as a new revision with your reason, even where the reference can’t
          otherwise change.
        </p>
        <p>This can’t be undone. Other projects move their own records.</p>
        <TextField
          label="Reason"
          value={reason}
          onChange={(value) => {
            setReason(value);
            setMissing(false);
          }}
          error={missing ? "Say why, for the record." : undefined}
          maxLength={2000}
          multiline
          help="It’s kept in the history of every record this changes."
        />
      </ConfirmDialog>
    </div>
  );
}

/** After a move: the record holds the new entry, and which of the project's records were
 * changed to refer to it. */
function MovedNote({ moved, entity }: { moved: MovedState; entity: Entity }) {
  const project = entity.owner_project?.name ?? "This project";
  const types = useQuery(typesQuery).data;
  const slug = entity.owner_project?.slug;
  return (
    <div className="alert alert-success" role="status">
      <p>
        Moved to {moved.to}: {project}’s record holds it now instead of {moved.from}.
      </p>
      {moved.repointed.length ? (
        <>
          <p>
            {moved.repointed.length === 1
              ? `This record refers to ${moved.to} now:`
              : `These ${String(moved.repointed.length)} records refer to ${moved.to} now:`}
          </p>
          <ul className="value-list">
            {moved.repointed.map((r) => (
              <li key={r.hrid}>
                <Link
                  to="/entities/$ref/"
                  params={{ ref: r.hrid }}
                  search={slug && hasIdentity(types, r.type) ? { project: slug } : {}}
                >
                  {r.title}
                </Link>
                {r.title !== r.hrid ? <span className="muted"> {r.hrid}</span> : null}
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p>
          None of {project}’s other records referred to {moved.from}.
        </p>
      )}
    </div>
  );
}

export function EntityOverview() {
  const { entity, type, types } = useEntity();
  usePageTitle(`${entity.title} (${entity.hrid})`);
  const ref = apiRef(entity);
  const provenance = useQuery(provenanceQuery(ref)).data;
  const projects = useQuery(entityProjectsQuery(ref)).data;
  const origins = useQuery(originsQuery(ref)).data ?? [];
  const making = useQuery(makingQuery(ref)).data ?? [];
  const bundles = useQuery(bundleOriginsQuery(ref)).data ?? [];
  const moved = useRouterState({ select: (s) => s.location.state.moved });
  const collection = isCollection(type, types);
  const app = type?.name === APP_TYPE;
  // A collection's members and search have a section of their own, as an app's spec does.
  const fields = (type?.fields ?? []).filter(
    (f) => !(collection && COLLECTION_FIELDS.has(f.name)) && !(app && f.name === "spec"),
  );
  // A record of a registry entry (ADR 0115): the entry's values, then the project's own.
  const entry = entity.entry;
  const entryFields = entry ? fields.filter((f) => f.registry) : [];
  const own = entry ? fields.filter((f) => !f.registry) : fields;
  const project = entity.owner_project?.name ?? "This project";
  return (
    <>
      <EntityTabs entity={entity} type={type} />
      {moved?.to === entity.hrid ? <MovedNote moved={moved} entity={entity} /> : null}
      {entry?.superseded ? <Superseded entity={entity} /> : null}
      {entity.archived ? (
        <div className="alert alert-warning" role="note">
          <p>This entity is archived. Its values and history are kept, and it can’t change.</p>
        </div>
      ) : null}
      {type?.detached ? <DetachedNote plugin={type.plugin} what="entity" /> : null}
      {!type ? (
        <div className="alert alert-warning" role="note">
          <p>
            Its type, <code>{entity.type}</code>, isn’t installed any more, so its values can’t be
            shown.
          </p>
        </div>
      ) : null}
      {!entity.archived ? <EntityState entity={entity} type={type} /> : null}
      {!entity.archived && type ? <EntityWork entity={entity} type={type} /> : null}
      {type?.name === VIEW_TYPE ? <SavedViewPanel view={entity} types={types} /> : null}
      {app ? <AppPanel app={entity} types={types} /> : null}
      {entry ? (
        <section className="card" aria-labelledby="entry-title">
          <h2 className="section-title" id="entry-title">
            Registry entry {entry.hrid}
          </h2>
          <p className="muted">
            In the {entry.registry.name} registry. These values are the entry’s: every project with{" "}
            {entry.hrid} has the same ones.{" "}
            <Link to="/entries/$ref/" params={{ ref: entry.hrid }}>
              Open the entry
            </Link>
          </p>
          <ValueList fields={entryFields} entity={entity} provenance={provenance} marks />
        </section>
      ) : null}
      <section className="card" aria-labelledby="values-title">
        <h2 className="section-title" id="values-title">
          {entry ? `${project}’s values` : "Values"}
        </h2>
        {entry ? (
          <p className="muted">
            {project}’s own record of {entry.hrid}. Other projects with it keep their own.
          </p>
        ) : null}
        <ValueList fields={own} entity={entity} provenance={provenance} marks={false} />
      </section>
      {collection ? <CollectionMembers entity={entity} types={types} /> : null}
      {app ? <AppSpec app={entity} /> : null}
      <section className="card" aria-labelledby="details-title">
        <h2 className="section-title" id="details-title">
          Details
        </h2>
        <dl className="summary">
          <dt>ID</dt>
          <dd>
            <code>{entity.hrid}</code>
            {entry ? <span className="cell-meta">Its registry entry’s ID</span> : null}
          </dd>
          {entry ? (
            <>
              <dt>Registry</dt>
              <dd>{entry.registry.name}</dd>
            </>
          ) : null}
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
            {personName(entity.created_by)}, <RelativeTime value={entity.created_at} />
          </dd>
          <dt>Last changed</dt>
          <dd>
            <RelativeTime value={entity.updated_at} /> (revision {entity.revision})
          </dd>
          {making.length ? (
            <>
              <dt>How it was made</dt>
              <dd>
                <ul className="value-list">
                  {madeGroups(making).map((rows) => (
                    <li key={[rows[0]?.by, rows[0]?.run?.hrid, rows[0]?.source?.id].join("-")}>
                      <MadeHow rows={rows} />
                    </li>
                  ))}
                </ul>
              </dd>
            </>
          ) : null}
          {origins.length || bundles.length ? (
            <>
              <dt>Imported from</dt>
              <dd>
                <ul className="value-list">
                  {bundles.map((b, index) => (
                    <li key={`bundle-${String(index)}`}>
                      <FromBundle origin={b} />
                    </li>
                  ))}
                  {origins.map((o, index) => (
                    <li key={index}>
                      <ImportedFrom origin={o} />
                    </li>
                  ))}
                </ul>
              </dd>
            </>
          ) : null}
        </dl>
      </section>
    </>
  );
}

function Changes({ number, type }: { number: number; type: EntityType | undefined }) {
  const { entity } = useEntity();
  // Its own refs and files: a value it removed may not be in the entity's any more.
  const detail = useQuery(revisionQuery(apiRef(entity), number));
  return <ChangesTable detail={detail.data} type={type} owner={entity.owner_project} />;
}

/** A registry entry's revision's changes: a curated fact's before and after, say. */
function EntryChanges({
  entry,
  number,
  type,
  owner,
}: {
  entry: string;
  number: number;
  type: EntityType | undefined;
  owner: { slug: string } | null | undefined;
}) {
  const detail = useQuery(entryRevisionQuery(entry, number));
  if (detail.error) return <ProblemAlert problem={problemOf(detail.error)} />;
  return <ChangesTable detail={detail.data} type={type} owner={owner} />;
}

/** What a revision changed, field by field: before and after. */
function ChangesTable({
  detail,
  type,
  owner,
}: {
  detail: RevisionDetail | undefined;
  type: EntityType | undefined;
  /** Whose records references open: the record's project's. */
  owner: { slug: string } | null | undefined;
}) {
  if (!detail) return <Loading label="Loading the changes…" />;
  const fields = new Map((type?.fields ?? []).map((f) => [f.name, f]));
  const changes = Object.entries(detail.changes);
  if (changes.length === 0) return <p className="muted">No values changed.</p>;
  const shown = { ...detail, owner_project: owner ?? null };
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
                    <Value field={field} value={change.before} entity={shown} />
                  ) : (
                    JSON.stringify(change.before)
                  )}
                </td>
                <td>
                  {field ? (
                    <Value field={field} value={change.after} entity={shown} />
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
          Revision {revision.number}: {revisionWhat(revision)} by {who}
        </span>
        <span className="row-meta">
          {revision.created_at ? <RelativeTime value={revision.created_at} /> : null}{" "}
          <Via revision={revision} />
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

/**
 * A registry entry's revision (ADR 0115). Who made it, how and when are told for changes
 * made through the reader's projects, and for registrars' changes; of anything else, only
 * what it set.
 */
export function EntryRevisionRow({
  entry,
  revision,
  type,
  owner,
}: {
  /** The entry's ID. */
  entry: string;
  revision: Revision;
  type: EntityType | undefined;
  /** On a record's page, its project: references in the changes open its records. */
  owner?: { slug: string } | null | undefined;
}) {
  const [open, setOpen] = useState(false);
  const labels = new Map((type?.fields ?? []).map((f) => [f.name, f.label]));
  const changed = revision.changed_fields.map((n) => labels.get(n) ?? n);
  const what = KIND[revision.kind] ?? revision.kind;
  const disclosed = revision.created_at !== null;
  return (
    <li className="row row-stacked">
      <div className="row-main">
        <span className="row-title">
          Revision {revision.number}:{" "}
          {disclosed
            ? `${what} by ${personName(revision.user)}`
            : changed.length
              ? `Set ${changed.join(", ")}`
              : what}
        </span>
        {revision.created_at ? (
          <span className="row-meta">
            <RelativeTime value={revision.created_at} /> <Via revision={revision} />
            {changed.length ? ` · ${changed.join(", ")}` : ""}
          </span>
        ) : (
          // Made through a project the reader can't read: what, never who, how or when.
          <span className="row-meta">Through a project you can’t read</span>
        )}
        {revision.reason ? <p className="row-reason">“{revision.reason}”</p> : null}
        {open ? (
          <EntryChanges entry={entry} number={revision.number} type={type} owner={owner} />
        ) : null}
      </div>
      {revision.changed_fields.length ? (
        <div className="row-actions">
          <Button
            variant="quiet"
            label={`${open ? "Hide" : "Show"} changes in entry revision ${String(revision.number)}`}
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

/** A list of revisions, newest first, a page at a time. */
function Revisions({
  query,
  label,
  row,
}: {
  query: UseInfiniteQueryResult<InfiniteData<{ items: Revision[] }>>;

  label: string;
  row: (revision: Revision) => React.ReactNode;
}) {
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <>
      {query.isPending ? <Loading label={`Loading ${label}…`} /> : null}
      {query.error ? <ProblemAlert problem={problemOf(query.error)} /> : null}
      <ul className="rows">{items.map(row)}</ul>
      {query.hasNextPage ? (
        <div className="actions">
          <Button
            busy={query.isFetchingNextPage}
            busyText="Loading…"
            label={`Show older revisions of ${label}`}
            onClick={() => void query.fetchNextPage()}
          >
            Show older
          </Button>
        </div>
      ) : null}
    </>
  );
}

export function EntityHistory() {
  const { entity, type } = useEntity();
  usePageTitle(`History of ${entity.title} (${entity.hrid})`);
  const revisions = useInfiniteQuery(revisionsQuery(apiRef(entity)));
  const entry = entity.entry;
  const project = entity.owner_project?.name ?? "This project";
  return (
    <>
      <EntityTabs entity={entity} type={type} />
      <p className="muted">
        Every change, newest first. Revisions can’t be changed or removed, by anyone.
      </p>
      {entry ? (
        <h2 className="section-title" id="record-history-title">
          {project}’s record
        </h2>
      ) : null}
      <Revisions
        query={revisions}
        label="the history"
        row={(r) => <RevisionRow key={r.number} revision={r} type={type} />}
      />
      {entry ? <EntryHistory entity={entity} type={type} /> : null}
    </>
  );
}

/** The record's registry entry's revisions: its values are every holding project's. */
function EntryHistory({ entity, type }: { entity: Entity; type: EntityType | undefined }) {
  const entry = entity.entry;
  const revisions = useInfiniteQuery(entryRevisionsQuery(entry?.id ?? ""));
  if (!entry) return null;
  return (
    <section aria-labelledby="entry-history-title">
      <h2 className="section-title" id="entry-history-title">
        Registry entry {entry.hrid}
      </h2>
      <p className="muted">
        Its values are every holding project’s: its identity, what’s derived from it, and the facts
        its registrars curate. Changes made through projects you can’t read say only what they set.
      </p>
      <Revisions
        query={revisions}
        label={`registry entry ${entry.hrid}`}
        row={(r) => (
          <EntryRevisionRow
            key={r.number}
            entry={entry.id}
            revision={r}
            type={type}
            owner={entity.owner_project}
          />
        )}
      />
    </section>
  );
}
