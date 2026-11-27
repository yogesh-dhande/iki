/**
 * A record's Audit trail tab (ADR 0176): every revision, newest first, with who made it,
 * how, when (in UTC), why, the signatures that set a state and the holds it opened, and
 * what it changed, field by field; or By field, each value's last change. Narrowed to a
 * person or a field, in the address. A record of a registry entry has its entry's
 * revisions too, told as the entry's list tells them (ADR 0115). Nothing in it can be
 * changed or removed, by anyone, and it downloads whole, for inspection.
 */

import {
  useInfiniteQuery,
  useQuery,
  type InfiniteData,
  type UseInfiniteQueryResult,
} from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useId, useState, type ReactNode } from "react";

import { problemOf } from "../api";
import {
  contributorsQuery,
  entryTrailQuery,
  trailQuery,
  type AuditSearch,
  type TrailFilter,
} from "../audit-trail";
import {
  apiRef,
  personName,
  provenanceQuery,
  recordSearch,
  revisionQuery,
  type Entity,
  type EntityType,
  type FieldDef,
  type Revision,
  type RevisionDetail,
} from "../entities";
import { usePageTitle } from "../layout";
import { objectList, objectListText } from "../object-lists";
import { entryRevisionQuery } from "../registries";
import { DownloadIcon, ExportButton } from "./entity-export";
import { trailCsvUrl } from "./entity-header";
import { signaturesQuery } from "./entity-signatures";
import { shownIn, useEntity, Value } from "./entity";
import { KIND, revisionWhat, UtcTime, Via } from "./revision-text";
import { Button, buttonClass, Loading, ProblemAlert, RelativeTime } from "../ui";

export function EntityAudit() {
  const { entity, type } = useEntity();
  usePageTitle(`Audit trail of ${entity.title} (${entity.hrid})`);
  const search = useSearch({ from: "/entities/$ref/audit" });
  const navigate = useNavigate();
  const filter: TrailFilter = { person: search.person, field: search.field };
  const narrow = (next: Partial<AuditSearch>) => {
    const merged = { ...search, ...next };
    void navigate({
      to: "/entities/$ref/audit/",
      params: { ref: entity.hrid },
      search: {
        ...recordSearch(entity),
        ...(merged.view ? { view: merged.view } : {}),
        ...(merged.person ? { person: merged.person } : {}),
        ...(merged.field ? { field: merged.field } : {}),
      },
      replace: true,
    });
  };
  return (
    <>
      <TrailToolbar entity={entity} type={type} search={search} narrow={narrow} />
      {search.view === "fields" ? (
        <ByField entity={entity} type={type} filter={filter} />
      ) : (
        <Timeline entity={entity} type={type} filter={filter} />
      )}
    </>
  );
}

/** Every field, the entry's included: what the Field filter offers. */
function trailFields(type: EntityType | undefined): FieldDef[] {
  return type?.fields ?? [];
}

function TrailToolbar({
  entity,
  type,
  search,
  narrow,
}: {
  entity: Entity;
  type: EntityType | undefined;
  search: AuditSearch;
  narrow: (next: Partial<AuditSearch>) => void;
}) {
  const ids = useId();
  const people = useQuery(contributorsQuery(apiRef(entity))).data ?? [];
  const fields = trailFields(type);
  const fieldsView = search.view === "fields";
  return (
    <div className="trail-toolbar">
      <div className="segmented" role="group" aria-label="Show">
        <button
          type="button"
          aria-pressed={!fieldsView}
          onClick={() => {
            narrow({ view: undefined });
          }}
        >
          Timeline
        </button>
        <button
          type="button"
          aria-pressed={fieldsView}
          onClick={() => {
            narrow({ view: "fields" });
          }}
        >
          By field
        </button>
      </div>
      <span className="inline-field">
        <label htmlFor={`${ids}-person`}>Person</label>
        <select
          id={`${ids}-person`}
          value={search.person ?? ""}
          onChange={(event) => {
            narrow({ person: event.target.value || undefined });
          }}
        >
          <option value="">Anyone</option>
          {people.map((p) => (
            <option key={p.id} value={p.id}>
              {personName(p)}
            </option>
          ))}
          {/* Someone the address names who isn't listed, until the list comes. */}
          {search.person && !people.some((p) => p.id === search.person) ? (
            <option value={search.person}>Someone else</option>
          ) : null}
        </select>
      </span>
      <span className="inline-field">
        <label htmlFor={`${ids}-field`}>Field</label>
        <select
          id={`${ids}-field`}
          value={search.field ?? ""}
          onChange={(event) => {
            narrow({ field: event.target.value || undefined });
          }}
        >
          <option value="">Any field</option>
          {fields.map((f) => (
            <option key={f.name} value={f.name}>
              {f.label}
            </option>
          ))}
          {search.field && !fields.some((f) => f.name === search.field) ? (
            <option value={search.field}>{search.field}</option>
          ) : null}
        </select>
      </span>
      <div className="trail-export">
        <span className="muted" id={`${ids}-kept`}>
          Revisions can’t be changed or removed, by anyone.
        </span>
        <a
          className="button"
          href={trailCsvUrl(entity)}
          download
          aria-describedby={`${ids}-export`}
        >
          <DownloadIcon />
          Export for inspection
        </a>
        <span className="visually-hidden" id={`${ids}-export`}>
          The whole trail as CSV, whatever it’s narrowed to here. Each download is in the audit log.
        </span>
      </div>
    </div>
  );
}

// --- Timeline ----------------------------------------------------------------------------------

function Timeline({
  entity,
  type,
  filter,
}: {
  entity: Entity;
  type: EntityType | undefined;
  filter: TrailFilter;
}) {
  const revisions = useInfiniteQuery(trailQuery(apiRef(entity), filter));
  const entry = entity.entry;
  const project = entity.owner_project?.name ?? "This project";
  return (
    <>
      {/* Its tab counts both lists: each says how many are its. */}
      {entry ? (
        <div className="trail-heading">
          <h2 className="section-title" id="record-history-title">
            {project}’s record
          </h2>
          <span className="trail-heading-count">{revisionsText(entity.revision)}</span>
        </div>
      ) : null}
      <Revisions
        query={revisions}
        label="the audit trail"
        row={(r) => <TrailRow key={r.number} entity={entity} revision={r} type={type} />}
      />
      {entry ? <EntryTimeline entity={entity} type={type} filter={filter} /> : null}
    </>
  );
}

/** "3 revisions": how many a list of them holds, beside its heading. */
function revisionsText(n: number): string {
  return `${n.toLocaleString()} ${n === 1 ? "revision" : "revisions"}`;
}

/** The record's registry entry's revisions: its values are every holding project's. */
function EntryTimeline({
  entity,
  type,
  filter,
}: {
  entity: Entity;
  type: EntityType | undefined;
  filter: TrailFilter;
}) {
  const entry = entity.entry;
  const revisions = useInfiniteQuery(entryTrailQuery(entry?.id ?? "", filter));
  if (!entry) return null;
  return (
    <section aria-labelledby="entry-history-title">
      <div className="trail-heading">
        <h2 className="section-title" id="entry-history-title">
          Registry entry {entry.hrid}
        </h2>
        <span className="trail-heading-count">{revisionsText(entry.revision)}</span>
      </div>
      <p className="muted">
        Its values are every holding project’s: its identity, what’s derived from it, and the facts
        its registrars curate. Changes made through projects you can’t read say only what they set.
      </p>
      <Revisions
        query={revisions}
        label={`registry entry ${entry.hrid}`}
        row={(r) => (
          <TrailRow key={r.number} entity={entity} revision={r} type={type} entry={entry.id} />
        )}
      />
    </section>
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
  row: (revision: Revision) => ReactNode;
}) {
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <>
      {query.isPending ? <Loading label={`Loading ${label}…`} /> : null}
      {query.error ? <ProblemAlert problem={problemOf(query.error)} /> : null}
      {query.isSuccess && items.length === 0 ? (
        <p className="muted">No revisions of it match.</p>
      ) : null}
      <ol className="trail">{items.map(row)}</ol>
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

/** A value as given, cut short: a genome's bases needn't fill the page. */
function submittedText(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 80 ? `${text.slice(0, 80)}…` : text;
}

/** Failing observations from this revision (ADR 0127) and the hold review. */
function HoldsOpened({ revision }: { revision: Revision }) {
  const holds = revision.holds ?? [];
  if (!holds.length) return null;
  return (
    <ul className="row-holds" aria-label={`Holds at revision ${String(revision.number)}`}>
      {holds.map((h) => (
        <li key={h.occurrence_id}>
          Held for review: {h.label}. {h.message}{" "}
          {h.legacy_gap
            ? "Earlier hold evidence is incomplete"
            : h.held_by
              ? `Saved anyway by ${personName(h.held_by)}`
              : "Held by Iki’s check of its plugins’ rules"}
          {h.agent ? `, through ${h.agent}` : ""}
          {h.reason === null ? ". Reason unavailable." : <>: “{h.reason}”</>}
          {h.entry_revision == null
            ? ""
            : ` Registry entry revision ${String(h.entry_revision)}.`}{" "}
          {h.reviewer && h.reviewed_at ? (
            <>
              Reviewed by {personName(h.reviewer)} <RelativeTime value={h.reviewed_at} />: “
              {h.review_reason}”
            </>
          ) : (
            "Still held, for review."
          )}
        </li>
      ))}
    </ul>
  );
}

/** The electronic signatures that set a revision's state (ADR 0126), as people read them,
 * with each signer's reason. */
function SignedWith({ entity, revision }: { entity: Entity; revision: Revision }) {
  const listed = revision.state?.signatures ?? [];
  const given = useQuery({
    ...signaturesQuery(apiRef(entity), false),
    enabled: listed.length > 0,
  }).data?.items;
  if (!listed.length) return null;
  const byId = new Map((given ?? []).map((s) => [s.id, s]));
  return (
    <ul
      className="trail-signatures"
      aria-label={`Signatures of revision ${String(revision.number)}`}
    >
      {listed.map((s) => {
        const found = byId.get(s.id);
        return (
          <li key={s.id}>
            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M12 3 4 6v6c0 4.5 3.4 8 8 9 4.6-1 8-4.5 8-9V6z" />
              <path d="m9 12 2 2 4-4" />
            </svg>
            <span>
              {s.present
                ? s.manifestation
                : `Signature ${s.id} is gone from the database: something outside Iki deleted it.`}
              {found?.reason ? <> Reason: “{found.reason}”</> : null}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** What a revision did and who did it, as its title says: “Edited by Ada Lovelace”, or of an
 * entry's revision made through a project the reader can't read, only what it set. */
export function revisionTitle(
  revision: Revision,
  labels: ReadonlyMap<string, string>,
  entry: boolean,
): string {
  const changed = revision.changed_fields.map((n) => labels.get(n) ?? n);
  // An entry's revision made through a project the reader can't read: what, never who,
  // how or when.
  if (revision.created_at === null) {
    return changed.length ? `Set ${changed.join(", ")}` : (KIND[revision.kind] ?? revision.kind);
  }
  const what = entry ? (KIND[revision.kind] ?? revision.kind) : revisionWhat(revision);
  return `${what} by ${personName(revision.user)}`;
}

/** What the trail says of a revision under its title: when, how and which fields, why,
 * its identity as it was given, and the signatures and holds it carries. Its own page says
 * the same. */
export function RevisionFacts({
  entity,
  revision,
  labels,
}: {
  /** The record, whose signatures set its states: none for a registry entry's own page. */
  entity: Entity | undefined;
  revision: Revision;
  labels: ReadonlyMap<string, string>;
}) {
  const changed = revision.changed_fields.map((n) => labels.get(n) ?? n);
  return (
    <>
      <p className="trail-meta">
        {revision.created_at ? (
          <>
            <UtcTime value={revision.created_at} /> · <Via revision={revision} />
          </>
        ) : (
          "Through a project you can’t read"
        )}
        {changed.length ? ` · ${changed.join(", ")}` : ""}
      </p>
      {revision.reason ? <p className="row-reason">“{revision.reason}”</p> : null}
      {/* Its identity as it was given, spelled other than its registry entry (ADR 0138). */}
      {revision.submitted ? (
        <p className="row-submitted">
          Given as{" "}
          {Object.entries(revision.submitted).map(([name, value], index) => (
            <span key={name}>
              {index ? "; " : ""}
              {labels.get(name) ?? name}: <code>{submittedText(value)}</code>
            </span>
          ))}
          , which its registry entry spells another way.
        </p>
      ) : null}
      {entity ? <SignedWith entity={entity} revision={revision} /> : null}
      <HoldsOpened revision={revision} />
    </>
  );
}

/** One revision in the timeline: the record's, or with `entry`, its registry entry's. */
function TrailRow({
  entity,
  revision,
  type,
  entry,
}: {
  entity: Entity;
  revision: Revision;
  type: EntityType | undefined;
  /** For a registry entry's revision: the entry's ID. */
  entry?: string;
}) {
  const [open, setOpen] = useState(false);
  const labels = new Map((type?.fields ?? []).map((f) => [f.name, f.label]));
  const which = `${entry ? "entry revision" : "revision"} ${String(revision.number)}`;
  const number = String(revision.number);
  return (
    <li className="trail-rev">
      <span className="trail-node" aria-hidden="true">
        {revision.number}
      </span>
      <div className="trail-body">
        <div className="trail-title">
          <span className="row-title">
            Revision {revision.number}: {revisionTitle(revision, labels, Boolean(entry))}
          </span>
          <span className="trail-actions">
            {revision.changed_fields.length ? (
              <Button
                variant="quiet"
                label={`${open ? "Hide" : "Show"} changes in ${which}`}
                onClick={() => {
                  setOpen(!open);
                }}
              >
                {open ? "Hide changes" : `Show changes (${String(revision.changed_fields.length)})`}
              </Button>
            ) : null}
            {/* It as it was, on a page of its own (ADR 0189). */}
            {entry ? (
              <Link
                className={buttonClass("quiet")}
                to="/entries/$ref/revisions/$number/"
                params={{ ref: entry, number }}
                aria-label={`Open ${which} as it was`}
              >
                Open
              </Link>
            ) : (
              <Link
                className={buttonClass("quiet")}
                to="/entities/$ref/revisions/$number/"
                params={{ ref: entity.hrid, number }}
                search={recordSearch(entity)}
                aria-label={`Open ${which} as it was`}
              >
                Open
              </Link>
            )}
            {/* Each of the record's revisions as it was, as a file (ADR 0171): the latest
                too, as every revision is. An entry's revisions export as its records do. */}
            {entry ? null : (
              <ExportButton
                entity={entity}
                type={type}
                revision={revision.number}
                change={
                  revision.state
                    ? { field: revision.state.field.name, to: revision.state.to.name }
                    : null
                }
              />
            )}
          </span>
        </div>
        <RevisionFacts entity={entity} revision={revision} labels={labels} />
        {open ? (
          entry ? (
            <EntryChanges entry={entry} number={revision.number} type={type} entity={entity} />
          ) : (
            <Changes number={revision.number} type={type} entity={entity} />
          )
        ) : null}
      </div>
    </li>
  );
}

function Changes({
  number,
  type,
  entity,
}: {
  number: number;
  type: EntityType | undefined;
  entity: Entity;
}) {
  // Its own refs and files: a value it removed may not be in the entity's any more.
  const detail = useQuery(revisionQuery(apiRef(entity), number));
  if (detail.error) return <ProblemAlert problem={problemOf(detail.error)} />;
  return <ChangesTable detail={detail.data} type={type} owner={entity.owner_project} />;
}

/** A registry entry's revision's changes: a curated fact's before and after, say. */
function EntryChanges({
  entry,
  number,
  type,
  entity,
}: {
  entry: string;
  number: number;
  type: EntityType | undefined;
  entity: Entity;
}) {
  const detail = useQuery(entryRevisionQuery(entry, number));
  if (detail.error) return <ProblemAlert problem={problemOf(detail.error)} />;
  return <ChangesTable detail={detail.data} type={type} owner={entity.owner_project} />;
}

/** What a revision changed, field by field: before and after. */
export function ChangesTable({
  detail,
  type,
  owner,
}: {
  detail: RevisionDetail | undefined;
  type: EntityType | undefined;
  /** Whose records references open: the record's project's. */
  owner: { slug: string } | null | undefined;
}) {
  const caption = useId();
  if (!detail) return <Loading label="Loading the changes…" />;
  const fields = new Map((type?.fields ?? []).map((f) => [f.name, f]));
  const changes = Object.entries(detail.changes);
  if (changes.length === 0) return <p className="muted">No values changed.</p>;
  const shown = { ...detail, owner_project: owner ?? null };
  return (
    // A long value scrolls across within it, by keyboard too.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable region
    <div className="table-scroll trail-diff" tabIndex={0} role="group" aria-labelledby={caption}>
      <table className="table">
        <caption className="visually-hidden" id={caption}>
          Changes in {detail.entry ? "entry revision" : "revision"} {detail.number}
        </caption>
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
                <td className="diff-before">
                  {field ? (
                    <Value field={field} value={change.before} entity={shown} />
                  ) : (
                    JSON.stringify(change.before)
                  )}
                </td>
                <td className="diff-after">
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

/**
 * A registry entry's revision, on the entry's own page (ADR 0115). Who made it, how and
 * when are told for changes made through the reader's projects, and for registrars'
 * changes; of anything else, only what it set.
 */
export function EntryRevisionRow({
  entry,
  revision,
  type,
}: {
  /** The entry's ID. */
  entry: string;
  revision: Revision;
  type: EntityType | undefined;
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
        {open ? <EntryOnlyChanges entry={entry} number={revision.number} type={type} /> : null}
      </div>
      <div className="row-actions">
        {revision.changed_fields.length ? (
          <Button
            variant="quiet"
            label={`${open ? "Hide" : "Show"} changes in entry revision ${String(revision.number)}`}
            onClick={() => {
              setOpen(!open);
            }}
          >
            {open ? "Hide changes" : "Show changes"}
          </Button>
        ) : null}
        {/* It as it was, on a page of its own (ADR 0189). */}
        <Link
          className={buttonClass("quiet")}
          to="/entries/$ref/revisions/$number/"
          params={{ ref: entry, number: String(revision.number) }}
          aria-label={`Open entry revision ${String(revision.number)} as it was`}
        >
          Open
        </Link>
      </div>
    </li>
  );
}

function EntryOnlyChanges({
  entry,
  number,
  type,
}: {
  entry: string;
  number: number;
  type: EntityType | undefined;
}) {
  const detail = useQuery(entryRevisionQuery(entry, number));
  if (detail.error) return <ProblemAlert problem={problemOf(detail.error)} />;
  return <ChangesTable detail={detail.data} type={type} owner={null} />;
}

// --- By field ----------------------------------------------------------------------------------

/** A long value, cut short for a table: the field's tab or the Overview has it in full. */
const SHORT = 40;

function ShortValue({
  field,
  entity,
  type,
}: {
  field: FieldDef;
  entity: Entity;
  type: EntityType | undefined;
}) {
  const value = entity.values[field.name];
  if (typeof value === "string" && (field.type === "string" || field.type === "text")) {
    if (value.length > SHORT) {
      const noun = shownIn(entity, type, field.name)?.noun ?? "characters";
      // eslint-disable-next-line @typescript-eslint/no-misused-spread -- code points, as the API counts
      const length = [...value].length;
      return (
        <span>
          <code>{value.slice(0, SHORT - 16)}…</code>{" "}
          <span className="muted">
            ({length.toLocaleString()} {noun})
          </span>
        </span>
      );
    }
  }
  // A list of objects, such as a sequence's features, by how many and their names; other
  // lists of JSON by how many: its revisions have them whole.
  const objects = objectList(value);
  if (objects) return <span>{objectListText(objects)}</span>;
  if (Array.isArray(value) && (field.type === "json" || field.items === "json")) {
    return (
      <span className="muted">
        {value.length.toLocaleString()} {value.length === 1 ? "item" : "items"}
      </span>
    );
  }
  if (field.type === "json" && value != null) return <span className="muted">JSON</span>;
  return <Value field={field} value={value} entity={entity} />;
}

/** Each field's current value and its last change: who set it, how, when and in which
 * revision. */
function ByField({
  entity,
  type,
  filter,
}: {
  entity: Entity;
  type: EntityType | undefined;
  filter: TrailFilter;
}) {
  const provenance = useQuery(provenanceQuery(apiRef(entity)));
  if (provenance.isPending) return <Loading label="Loading each field’s last change…" />;
  if (provenance.error) return <ProblemAlert problem={problemOf(provenance.error)} />;
  const set = provenance.data;
  const rows = trailFields(type).filter(
    (f) =>
      (entity.values[f.name] != null || set[f.name]) &&
      (!filter.field || f.name === filter.field) &&
      (!filter.person || set[f.name]?.user?.id === filter.person),
  );
  if (!rows.length) return <p className="muted">No fields match.</p>;
  return (
    <div className="table-scroll trail-fields">
      <table className="table">
        <caption className="visually-hidden">Each field’s last change</caption>
        <thead>
          <tr>
            <th scope="col">Field</th>
            <th scope="col">Current value</th>
            <th scope="col">Last set by</th>
            <th scope="col">How</th>
            <th scope="col">When (UTC)</th>
            <th scope="col" className="numeric">
              Revision
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((f) => {
            const r = set[f.name];
            return (
              <tr key={f.name}>
                <th scope="row">
                  <Link
                    to="/entities/$ref/audit/"
                    params={{ ref: entity.hrid }}
                    search={(prev) => ({ ...prev, view: undefined, field: f.name })}
                  >
                    {f.label}
                  </Link>
                </th>
                <td className="trail-value">
                  <ShortValue field={f} entity={entity} type={type} />
                </td>
                {r?.created_at ? (
                  <>
                    <td>{personName(r.user)}</td>
                    <td>
                      <Via revision={r} />
                    </td>
                    <td className="nowrap">
                      <UtcTime value={r.created_at} />
                    </td>
                  </>
                ) : (
                  <td colSpan={3} className="muted">
                    {r ? "Through a project you can’t read" : "Not recorded"}
                  </td>
                )}
                <td className="numeric">
                  {r ? (r.entry ? `entry ${String(r.number)}` : r.number) : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
