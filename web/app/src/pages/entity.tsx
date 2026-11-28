/**
 * An entity's page (ADR 0176): its header and tabs, then its values. Who set each value,
 * how and when is on demand, from the history button beside it, and in full on its Audit
 * trail tab. A record of a registry entry shows the entry's values, then its project's,
 * with a picker for the reader's other projects' records of it (ADR 0115). Its rail
 * says where it's from and what it's linked to.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Link,
  Outlet,
  useMatches,
  useNavigate,
  useRouterState,
  useSearch,
} from "@tanstack/react-router";
import { lazy, Suspense, useId, useState } from "react";
import { Button as AriaButton, Dialog, DialogTrigger, Popover } from "react-aria-components";

import { iki, problemOf, useApiMutation, useOpenedRevision } from "../api";
import { lineageOf } from "../components";
import {
  apiRef,
  ENTITIES,
  entityProjectsQuery,
  hasIdentity,
  inSentence,
  originsQuery,
  makingQuery,
  bundleOriginsQuery,
  personName,
  provenanceQuery,
  recordSearch,
  revisionQuery,
  typeLabel,
  typesQuery,
  type Entity,
  type EntityType,
  type FieldDef,
  type Making,
  type Origin,
  type Ref,
  type Revision,
} from "../entities";

import { entryQuery, entryRevisionQuery } from "../registries";
import { usePageTitle } from "../layout";
import { isServiceField } from "../requests";
import { APP_TYPE } from "../apps";
import { VIEW_TYPE } from "../views";
import { AppPanel, AppSpec } from "./app";
import { COLLECTION_FIELDS, CollectionMembers, isCollection } from "./collections";
import { DetachedNote } from "./detached-note";
import { SavedViewPanel } from "./view";
import { EntityHolds } from "./entity-holds";
import { EntitySignatures, signaturesQuery } from "./entity-signatures";
import { EntityRequest, ResultNote } from "./entity-request";
import { EntityAmount } from "./entity-amount";
import { EntityLocation } from "./entity-location";
import { EntityState } from "./entity-state";
import { EntityWork } from "./entity-work";
import { EntityTabs, neighboursOf, neighboursQuery, trailCount, useEntity } from "./entity-header";
import { isOptionRef } from "../lab-lists";
import {
  drawnBy,
  DrawnBy,
  MappedFeatures,
  OptionRef,
  registryNote,
  shownIn,
  Value,
} from "./entity-value";
import { FeatureTable, MiniMap, useMiniMap, type MapOf } from "./record-map";
import { UtcTime, Via } from "./revision-text";
import { Button, ConfirmDialog, Loading, RelativeTime, TextField, Time } from "../ui";

const EntityGel = lazy(() => import("./entity-gel"));

/** What the page after a move shows: the records it changed to refer to the new entry. */
interface MovedState {
  from: string;
  to: string;
  repointed: Ref[];
  /** To an entry of another type (ADR 0165): the type's label. The move made the
   * project's record of it and archived the one it was moved from. */
  across?: string;
}

declare module "@tanstack/react-router" {
  interface HistoryState {
    moved?: MovedState;
  }
}

/**
 * A record's pages: its header and tabs, then the tab that's open. The header and tabs are
 * drawn here, once for all its tabs, not by each tab: a tab's page can wait on what it
 * reads (the record again, under the `?project=` its tabs' links carry, or its code) after
 * the address has changed, and a strip each tab drew afresh was replaced under the reader
 * when the new tab came, with whatever they were pressing or reading. Its edit page, and
 * its page at a past revision, have a header of their own and no tabs.
 */
export function RecordPages() {
  // Its edit page, and the record as it was at a revision, which isn't the record as its
  // tabs show it now.
  const editing = useMatches({
    select: (matches) =>
      matches.some(
        (m) =>
          m.routeId === "/entities/$ref/edit" || m.routeId === "/entities/$ref/revisions/$number",
      ),
  });
  return (
    <>
      {editing ? null : <RecordTabs />}
      <Outlet />
    </>
  );
}

function RecordTabs() {
  const { entity, type } = useEntity();
  return <EntityTabs entity={entity} type={type} />;
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
            , by {/* The run and its action kept together, each whole. */}
            <span className="made-run">
              <Link to="/runs/$ref/" params={{ ref: first.run.hrid }}>
                {first.run.hrid}
              </Link>{" "}
              (<code>{first.run.action}</code>)
            </span>
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

const AND = new Intl.ListFormat("en", { type: "conjunction" });

/** A registrar replaced the record's entry (ADR 0115): what with, and for Editors, moving
 * the record there. An entry of another type of its identity (ADR 0165) can't be this
 * record's: a move there makes the project's record of it, with what the two types'
 * records share, and archives this one. */
function Superseded({ entity }: { entity: Entity }) {
  const entry = entity.entry;
  const by = entry?.superseded;
  const navigate = useNavigate();
  const client = useQueryClient();
  const types = useQuery(typesQuery).data;
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const [missing, setMissing] = useState(false);
  // Entities are refreshed once the page is the moved record's: refreshing this one first
  // would ask for the old ID in the project, which no longer has it.
  const move = useApiMutation(
    async (header: { "If-Match": string }) =>
      unwrap(
        await iki.POST("/api/v1/entities/{ref}/move", {
          params: { path: { ref: entity.id }, header },
          body: { reason: reason.trim() },
        }),
      ),
    [],
  );
  // The record as it was when the move was asked for: one changed meanwhile is refused (412).
  const opened = useOpenedRevision(entity.revision, { open: asking, error: move.error });
  if (!entry || !by) return null;
  const project = entity.owner_project?.name ?? "this project";
  const close = () => {
    setAsking(false);
    setReason("");
    setMissing(false);
    move.reset();
  };
  const carries = by.carries ?? null;
  const target = types?.find((t) => t.name === by.by.type);
  const kind = inSentence(target?.label ?? typeLabel(types, by.by.type));
  const carried = (carries ?? []).map((name) =>
    inSentence(target?.fields.find((f) => f.name === name)?.label ?? name),
  );
  const movable = entity.can.edit && !entity.archived && !by.held;
  const theirs = (
    <Link to="/entities/$ref/" params={{ ref: by.by.hrid }} search={recordSearch(entity)}>
      {project}’s record of {by.by.hrid}
    </Link>
  );
  return (
    <div className="alert alert-warning" role="note" aria-labelledby="superseded-title">
      <h2 className="section-title" id="superseded-title">
        {entry.hrid} was superseded by {by.by.hrid}
      </h2>
      <p>
        A registrar of the {entry.registry.name} registry replaced it on <Time value={by.at} />: “
        {by.reason}”. It can’t be registered again.
        {carries ? ` ${by.by.hrid} is its identity registered as ${aOrAn(kind)} ${kind}.` : null}
      </p>
      <p>
        {by.held && entity.archived ? (
          <>
            {theirs} took this one’s place. This one keeps its values and history under {entry.hrid}
            .
          </>
        ) : by.held ? (
          <>
            {project} has a record of {by.by.hrid} already, so this one can’t move there: {theirs}.
          </>
        ) : movable && carries ? (
          `Move ${project}’s record to ${by.by.hrid}, so its data is under the right ID. A record can’t change its type, so the move makes ${project}’s record of the ${kind} and archives this one.`
        ) : movable ? (
          `Move ${project}’s record to ${by.by.hrid}, so its data is under the right ID.`
        ) : entity.archived ? (
          `This record is archived, so it keeps ${entry.hrid}. Restore it to move it to ${by.by.hrid}.`
        ) : (
          `An Editor in ${project} can move this record to ${by.by.hrid}.`
        )}
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
          move.mutate(opened.ifMatch, {
            onSuccess: (moved) => {
              close();
              void navigate({
                to: "/entities/$ref/",
                params: { ref: moved.hrid },
                search: recordSearch(moved),
                state: (prev) => ({
                  ...prev,
                  moved: {
                    from: entry.hrid,
                    to: moved.hrid,
                    repointed: moved.repointed,
                    ...(carries ? { across: kind } : {}),
                  },
                }),
              }).then(() => client.invalidateQueries({ queryKey: ENTITIES }));
            },
          });
        }}
      >
        {carries ? (
          <p>
            {by.by.hrid} is {aOrAn(kind)} {kind}, and a record can’t change its type. This makes{" "}
            {project}’s record of {by.by.hrid}
            {carried.length
              ? `, with this record’s ${AND.format(carried)}`
              : ", which has none of this record’s values, as the two types’ records share none"}
            , and archives this one, which keeps its values, history and runs under {entry.hrid}.{" "}
            {project}’s own records that refer to {entry.hrid} will refer to {by.by.hrid}, each as a
            new revision with your reason, even where the reference can’t otherwise change.
          </p>
        ) : (
          <p>
            {project}’s record keeps its values, history and runs, and holds {by.by.hrid} instead of{" "}
            {entry.hrid}. {project}’s own records that refer to {entry.hrid} will refer to{" "}
            {by.by.hrid}, each as a new revision with your reason, even where the reference can’t
            otherwise change.
          </p>
        )}
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

function aOrAn(word: string): string {
  return /^[aeiou]/i.test(word) ? "an" : "a";
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
        {moved.across
          ? `Moved to ${moved.to}: this is ${project}’s record of it, a new ${moved.across} record with what the two types’ records share. Its record of ${moved.from} is archived.`
          : `Moved to ${moved.to}: ${project}’s record holds it now instead of ${moved.from}.`}
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

/** A clock: a field's history. */
function ClockIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </svg>
  );
}

/**
 * A field's history button (ADR 0176): its last change, before and after, who made it,
 * how and when, and a link to the field in the audit trail. A popover a press opens and
 * Escape closes, never one hovering opens (WCAG 1.4.13).
 */
function FieldHistory({
  field,
  entity,
  revision,
}: {
  field: FieldDef;
  entity: Entity;
  revision: Revision | undefined;
}) {
  const label = `History of ${field.label}`;
  return (
    <DialogTrigger>
      <AriaButton className="history-button" aria-label={label}>
        <ClockIcon />
      </AriaButton>
      <Popover className="history-popover" placement="bottom end">
        <Dialog className="history-dialog" aria-label={label}>
          <LastChange field={field} entity={entity} revision={revision} />
        </Dialog>
      </Popover>
    </DialogTrigger>
  );
}

function LastChange({
  field,
  entity,
  revision,
}: {
  field: FieldDef;
  entity: Entity;
  revision: Revision | undefined;
}) {
  const trail = (
    <Link
      to="/entities/$ref/audit/"
      params={{ ref: entity.hrid }}
      search={{ ...recordSearch(entity), field: field.name }}
    >
      {field.label} in the audit trail
    </Link>
  );
  if (!revision) {
    return (
      <>
        <p className="history-title">{field.label}</p>
        <p className="muted">No revision that set it is recorded here.</p>
        {trail}
      </>
    );
  }
  return (
    <>
      <p className="history-title">Last change to {field.label}</p>
      <ChangedValue field={field} entity={entity} revision={revision} />
      <dl className="history-facts">
        {revision.created_at ? (
          <>
            <dt>Who</dt>
            <dd>{personName(revision.user)}</dd>
            <dt>How</dt>
            <dd>
              <Via revision={revision} />
            </dd>
            <dt>When</dt>
            <dd>
              <UtcTime value={revision.created_at} />
            </dd>
          </>
        ) : null}
        <dt>Revision</dt>
        <dd>
          {revision.number}
          {revision.entry ? " of its registry entry" : ""}
        </dd>
      </dl>
      {revision.created_at ? null : (
        // An entry's revision made through a project the reader can't read: what, not who.
        <p className="muted">
          Set in its registry entry through a project you can’t read, so who, how and when aren’t
          shown.
        </p>
      )}
      {revision.reason ? <p className="history-reason">“{revision.reason}”</p> : null}
      {trail}
    </>
  );
}

/** What the revision changed the field from, and to: read when its history is opened. */
function ChangedValue({
  field,
  entity,
  revision,
}: {
  field: FieldDef;
  entity: Entity;
  revision: Revision;
}) {
  const entry = revision.entry ? (entity.entry?.id ?? null) : null;
  const own = useQuery({ ...revisionQuery(apiRef(entity), revision.number), enabled: !entry });
  const theirs = useQuery({
    ...entryRevisionQuery(entry ?? "", revision.number),
    enabled: entry !== null,
  });
  const detail = entry ? theirs : own;
  if (detail.isPending) return <Loading label="Loading the change…" />;
  const change = detail.data?.changes[field.name];
  if (!detail.data || !change) return null;
  // Its own refs and files: a value it removed may not be in the record's any more.
  const shown = { ...detail.data, owner_project: entity.owner_project };
  return (
    <dl className="history-change">
      <dt>Before</dt>
      <dd>
        <Value field={field} value={change.before} entity={shown} />
      </dd>
      <dt>After</dt>
      <dd>
        <Value field={field} value={change.after} entity={shown} />
      </dd>
    </dl>
  );
}

/** Fields' values, each with its history button; those not set, in a line after. */
function FieldRows({
  fields,
  entity,
  type,
  provenance,
  marks,
  map,
}: {
  fields: FieldDef[];
  entity: Entity;
  type: EntityType | undefined;
  provenance: Record<string, Revision> | undefined;
  /** Say how each is kept in its registry entry: identity, derived or fact. */
  marks: boolean;
  /** Its features' map, whose field reads as how many, with the Features card below. */
  map: MapOf | null;
}) {
  const id = useId();
  // The fields whose descriptions are open: a toggletip each, opened by a press, so it
  // stays until it's closed (WCAG 1.4.13), never by hovering alone.
  const [about, setAbout] = useState<ReadonlySet<string>>(new Set());
  const set = fields.filter((f) => entity.values[f.name] != null);
  const unset = fields.filter((f) => entity.values[f.name] == null);
  return (
    <>
      {set.length === 0 ? <p className="muted field-rows-empty">No values are set.</p> : null}
      <dl className="field-rows">
        {set.map((f) => {
          const mark = marks ? registryNote(f) : null;
          const open = about.has(f.name);
          const aboutId = `${id}-${f.name}-about`;
          const drawn = drawnBy(type, f.name);
          return (
            <div key={f.name} className="field-row">
              <dt>
                {f.label}
                {f.description ? (
                  <button
                    type="button"
                    className="about-field"
                    aria-label={`About ${f.label}`}
                    aria-expanded={open}
                    aria-controls={aboutId}
                    onClick={() => {
                      const next = new Set(about);
                      if (open) next.delete(f.name);
                      else next.add(f.name);
                      setAbout(next);
                    }}
                  >
                    <svg
                      viewBox="0 0 24 24"
                      aria-hidden="true"
                      focusable="false"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                    >
                      <circle cx="12" cy="12" r="9" />
                      <path d="M12 11v5" />
                      <path d="M12 7.5v.01" />
                    </svg>
                  </button>
                ) : null}
                {mark ? <span className="cell-meta">{mark}</span> : null}
                {f.computed && f.registry !== "derived" ? (
                  <span className="cell-meta">Computed</span>
                ) : null}
              </dt>
              <dd>
                <div className="field-value">
                  {f.description ? (
                    <p className="field-about" id={aboutId} hidden={!open}>
                      {f.description}
                    </p>
                  ) : null}
                  {map?.field === f.name ? (
                    <MappedFeatures map={map} />
                  ) : drawn ? (
                    <DrawnBy entity={entity} value={entity.values[f.name]} viewer={drawn} />
                  ) : (
                    <Value
                      field={f}
                      value={entity.values[f.name]}
                      entity={entity}
                      shown={shownIn(entity, type, f.name)}
                    />
                  )}
                </div>
                {provenance ? (
                  <FieldHistory field={f} entity={entity} revision={provenance[f.name]} />
                ) : null}
              </dd>
            </div>
          );
        })}
      </dl>
      {unset.length ? (
        <p className="muted field-rows-unset">Not set: {unset.map((f) => f.label).join(", ")}.</p>
      ) : null}
    </>
  );
}

/** The reader's projects' records of the entry: the one shown, and the others to switch to. */
function ProjectPicker({ entity }: { entity: Entity }) {
  const id = useId();
  const navigate = useNavigate();
  const records = useQuery({
    ...entryQuery(entity.entry?.id ?? ""),
    enabled: Boolean(entity.entry),
  }).data?.records;
  if (!records || records.length < 2 || !entity.owner_project) return null;
  const sorted = [...records].sort((a, b) => a.project.name.localeCompare(b.project.name));
  return (
    <span className="section-picker">
      <label htmlFor={id}>Project</label>
      <select
        id={id}
        value={entity.owner_project.slug}
        onChange={(event) => {
          void navigate({
            to: "/entities/$ref/",
            params: { ref: entity.hrid },
            search: { project: event.target.value },
          });
        }}
      >
        {sorted.map((r) => (
          <option key={r.id} value={r.project.slug}>
            {r.project.name}
          </option>
        ))}
      </select>
    </span>
  );
}

/** Its details, in the rail: where it is, who made it and when, and where it came from. */
function Details({ entity, type }: { entity: Entity; type: EntityType | undefined }) {
  const ref = apiRef(entity);
  const projects = useQuery(entityProjectsQuery(ref)).data;
  const origins = useQuery(originsQuery(ref)).data ?? [];
  const making = useQuery(makingQuery(ref)).data ?? [];
  const bundles = useQuery(bundleOriginsQuery(ref)).data ?? [];
  const signatures = useQuery(signaturesQuery(ref, false)).data?.signatures ?? [];
  const carried = signatures.filter((s) => s.status === "carried");
  const entry = entity.entry;
  const also = (projects ?? []).filter((p) => !p.owner);
  return (
    <section className="card rail-card" aria-labelledby="details-title">
      <h2 className="section-title" id="details-title">
        Details
      </h2>
      <dl className="rail-facts">
        <dt>ID</dt>
        <dd>
          <code>{entity.hrid}</code>
          {entry ? <span className="cell-meta">Its registry entry’s ID</span> : null}
        </dd>
        <dt>Type</dt>
        <dd>
          {type ? (
            <Link to="/types/$/" params={{ _splat: type.name }}>
              {type.label}
            </Link>
          ) : (
            <code>{entity.type}</code>
          )}
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
        {also.length ? (
          <>
            <dt>Also in</dt>
            <dd>
              {also.map((p, index) => (
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
          <RelativeTime value={entity.updated_at} />
        </dd>
        <dt>Revision</dt>
        <dd>
          {entity.revision} ·{" "}
          <Link
            to="/entities/$ref/audit/"
            params={{ ref: entity.hrid }}
            search={recordSearch(entity)}
          >
            audit trail
          </Link>
          {/* Its trail counts its entry's revisions too, as its tab does: say so. */}
          {entry ? (
            <span className="cell-meta">
              Its audit trail has {trailCount(entity).toLocaleString()}: {entity.revision} of this
              record, {entry.revision} of its entry
            </span>
          ) : null}
        </dd>
        {carried.length ? (
          <>
            <dt className="rail-wide">Signed</dt>
            <dd className="rail-wide">
              <ul className="value-list rail-list">
                {carried.map((s) => (
                  <li key={s.id}>
                    {s.meaning_label} by {s.signer_name || s.signer_email},{" "}
                    <Time value={s.signed_at} />
                  </li>
                ))}
              </ul>
            </dd>
          </>
        ) : null}
        {making.length ? (
          <>
            <dt className="rail-wide">How it was made</dt>
            <dd className="rail-wide">
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
            <dt className="rail-wide">Imported from</dt>
            <dd className="rail-wide">
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
  );
}

/** At most this many linked records in the rail; the Lineage tab has the rest. */
const RAIL_LINKS = 8;

/** What it's linked to, one step either way, in the rail: the Lineage tab has the rest. */
function LineageCard({ entity }: { entity: Entity }) {
  const graph = useQuery(neighboursQuery(entity)).data;
  const types = useQuery(typesQuery).data;
  if (!graph) return null;
  const linked = neighboursOf(graph, entity, types ?? []);
  const groups = [
    { label: "Derived from", items: linked.filter((n) => n.node.at < 0) },
    { label: "Used in", items: linked.filter((n) => n.node.at > 0) },
  ].filter((g) => g.items.length);
  const slug = entity.owner_project?.slug;
  return (
    <section className="card rail-card" aria-labelledby="rail-lineage-title">
      <h2 className="section-title" id="rail-lineage-title">
        Lineage
      </h2>
      {linked.length === 0 ? <p className="muted">Nothing you can read is linked to it.</p> : null}
      {groups.map((g) => (
        <div key={g.label} className="rail-group">
          <h3 className="rail-group-title">{g.label}</h3>
          <ul className="rail-links">
            {g.items.slice(0, RAIL_LINKS).map(({ node: e, via }) => (
              <li key={e.id}>
                {/* An option is read by its label, and leads to its list, as its field does. */}
                {isOptionRef(e) ? (
                  <OptionRef found={e} project={slug} />
                ) : (
                  <Link
                    to="/entities/$ref/"
                    params={{ ref: e.hrid }}
                    search={slug && hasIdentity(types, e.type) ? { project: slug } : {}}
                  >
                    <code>{e.hrid}</code>
                    {e.title !== e.hrid ? ` · ${e.title}` : ""}
                  </Link>
                )}
                {via.length ? <span className="rail-via"> via {via.join(", ")}</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ))}
      <Link
        to="/entities/$ref/lineage/"
        params={{ ref: entity.hrid }}
        search={recordSearch(entity)}
      >
        Open its lineage
      </Link>
    </section>
  );
}

export function EntityOverview() {
  const { entity, type, types } = useEntity();
  usePageTitle(`${entity.title} (${entity.hrid})`);
  const ref = apiRef(entity);
  const provenance = useQuery(provenanceQuery(ref)).data;
  const moved = useRouterState({ select: (s) => s.location.state.moved });
  const { signing } = useSearch({ from: "/entities/$ref" });
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
  // A request between projects (ADR 0132): the asking project's values, then the service's.
  const sides = entity.request;
  const asker = sides?.asking_project?.name ?? project;
  const asked = sides ? own.filter((f) => !isServiceField(sides, f.name)) : own;
  const served = sides ? own.filter((f) => isServiceField(sides, f.name)) : [];
  const map = useMiniMap(entity, type);
  const lineage = lineageOf(entity.type, types);
  const gel = lineage.includes("iki/plasmid.Assembly")
    ? "assembly"
    : lineage.includes("iki/plasmid.Plasmid")
      ? "plasmid"
      : null;
  return (
    <>
      <div className="record-body">
        <div className="record-main">
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
                Its type, <code>{entity.type}</code>, isn’t installed any more, so its values can’t
                be shown.
              </p>
            </div>
          ) : null}
          {entity.needs_recalculation?.length ? (
            <p role="status">
              Referenced inputs changed. Recalculate with {entity.needs_recalculation.join(", ")}{" "}
              before using these results.
            </p>
          ) : null}
          {!entity.archived ? <EntityHolds entity={entity} /> : null}
          {type?.result ? <ResultNote entity={entity} type={type} /> : null}
          {type && sides ? <EntityRequest entity={entity} type={type} /> : null}
          {!entity.archived ? <EntityState entity={entity} type={type} /> : null}
          <EntityLocation entity={entity} type={type} />
          <EntityAmount entity={entity} type={type} types={types} />
          {!entity.archived && type ? <EntityWork entity={entity} type={type} /> : null}
          <EntitySignatures entity={entity} {...(signing ? { signing } : {})} />
          {type?.name === VIEW_TYPE ? <SavedViewPanel view={entity} types={types} /> : null}
          {app ? <AppPanel app={entity} types={types} /> : null}
          {entry ? (
            <section className="card record-section" aria-labelledby="entry-title">
              <div className="section-head">
                <h2 className="section-title" id="entry-title">
                  Registry entry {entry.hrid}
                </h2>
                <p className="muted">
                  In the {entry.registry.name} registry. These values are the entry’s: every project
                  with {entry.hrid} has the same ones.{" "}
                  <Link to="/entries/$ref/" params={{ ref: entry.hrid }}>
                    Open the entry
                  </Link>
                </p>
              </div>
              <FieldRows
                fields={entryFields}
                entity={entity}
                type={type}
                provenance={provenance}
                marks
                map={map}
              />
            </section>
          ) : null}
          {gel ? (
            <Suspense fallback={<Loading label="Loading virtual gel…" />}>
              <EntityGel key={entity.id} entity={entity} kind={gel} />
            </Suspense>
          ) : null}
          <section className="card record-section" aria-labelledby="values-title">
            <div className="section-head">
              <h2 className="section-title" id="values-title">
                {entry ? "Project values" : sides ? `${asker}’s values` : "Values"}
              </h2>
              {entry ? (
                <>
                  <p className="muted">
                    {project}’s own record of {entry.hrid}. Other projects with it keep their own.
                  </p>
                  <ProjectPicker entity={entity} />
                </>
              ) : null}
            </div>
            <FieldRows
              fields={asked}
              entity={entity}
              type={type}
              provenance={provenance}
              marks={false}
              map={map}
            />
          </section>
          {/* A request's service fills in its own fields (ADR 0132). */}
          {sides && served.length ? (
            <section className="card record-section" aria-labelledby="service-values-title">
              <div className="section-head">
                <h2 className="section-title" id="service-values-title">
                  {sides.service ? `${sides.service.name}’s values` : "The service’s values"}
                </h2>
                <p className="muted">
                  {sides.service
                    ? `${sides.service.name}, the service it’s sent to, fills these in.`
                    : "The service it’s sent to fills these in, once it’s sent."}
                </p>
              </div>
              <FieldRows
                fields={served}
                entity={entity}
                type={type}
                provenance={provenance}
                marks={false}
                map={map}
              />
            </section>
          ) : null}
          {map ? <FeatureTable map={map} entity={entity} /> : null}
          {collection ? <CollectionMembers entity={entity} types={types} /> : null}
          {app ? <AppSpec app={entity} /> : null}
        </div>
        <aside className="record-rail" aria-label="About this record">
          {map ? <MiniMap map={map} entity={entity} /> : null}
          <Details entity={entity} type={type} />
          <LineageCard entity={entity} />
        </aside>
      </div>
    </>
  );
}
