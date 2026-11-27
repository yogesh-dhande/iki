/**
 * A record, or a registry entry, as it was at one of its revisions, read only (ADR 0189):
 * what a witness opens from the evidence they sign, and what an audit trail's row opens.
 * It's what the revision API gives the person, as the trail shows a revision: who made it,
 * how, when and why, its signatures and holds, then every value it had, then what it
 * changed. It says plainly that it isn't the record as it is now, and links to that and
 * to the revision as JSON.
 */

import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useId, type ReactNode } from "react";

import { problemOf } from "../api";
import {
  apiRef,
  recordSearch,
  revisionQuery,
  typesQuery,
  type Entity,
  type EntityType,
  type RevisionDetail,
} from "../entities";
import { usePageTitle } from "../layout";
import { TopBar } from "../page-top";
import { entryQuery, entryRevisionQuery, type Entry } from "../registries";
import { ChangesTable, RevisionFacts, revisionTitle } from "./entity-audit";
import { ExportButton } from "./entity-export";
import { useEntity, Value } from "./entity";
import { NotFound } from "./errors";
import { UtcTime } from "./revision-text";
import { buttonClass, Loading, PageHeader, PageTitle, ProblemAlert } from "../ui";

/** A revision's number from its address: a whole number from 1, or null. */
export function revisionNumber(param: string): number | null {
  return /^[1-9][0-9]{0,14}$/.test(param) ? Number(param) : null;
}

/** The record at `/entities/$ref/revisions/$number/`. */
export function RecordRevision() {
  const { entity, type } = useEntity();
  const { number } = useParams({ from: "/entities/$ref/revisions/$number" });
  const n = revisionNumber(number);
  if (n === null) return <NotFound />;
  return <RecordAt entity={entity} type={type} number={n} />;
}

function RecordAt({
  entity,
  type,
  number,
}: {
  entity: Entity;
  type: EntityType | undefined;
  number: number;
}) {
  usePageTitle(`Revision ${String(number)} of ${entity.title} (${entity.hrid})`);
  const detail = useQuery(revisionQuery(apiRef(entity), number));
  const search = recordSearch(entity);
  const project = entity.owner_project;
  const entry = entity.entry;
  return (
    <>
      <TopBar
        crumbs={[
          <Link to="/entities/">Entities</Link>,
          <Link to="/entities/types/$/" params={{ _splat: entity.type }} search={{}}>
            {type?.label_plural ?? type?.label ?? entity.type}
          </Link>,
          <Link to="/entities/$ref/" params={{ ref: entity.hrid }} search={search}>
            {entity.hrid}
          </Link>,
          `Revision ${String(number)}`,
        ]}
      />
      <div className="record-heading">
        <div className="record-title">
          <PageTitle>
            Revision {number} of {entity.hrid}
          </PageTitle>
        </div>
        <p className="record-meta">
          {type?.label ?? <code>{entity.type}</code>}
          {project ? ` · ${project.name}` : null}
        </p>
      </div>
      <PastNote
        what={entity.hrid}
        number={number}
        latest={entity.revision}
        detail={detail.data}
        current={
          <Link
            className={buttonClass("primary")}
            to="/entities/$ref/"
            params={{ ref: entity.hrid }}
            search={search}
          >
            Open {entity.hrid} as it is now
          </Link>
        }
        json={`/api/v1/entities/${entity.id}/revisions/${String(number)}`}
      />
      {detail.error ? <ProblemAlert problem={problemOf(detail.error)} /> : null}
      {detail.isPending ? <Loading label={`Loading revision ${String(number)}…`} /> : null}
      {detail.data ? (
        <RevisionBody
          detail={detail.data}
          type={type}
          owner={project}
          entity={entity}
          actions={
            <ExportButton
              entity={entity}
              type={type}
              revision={number}
              change={
                detail.data.state
                  ? { field: detail.data.state.field.name, to: detail.data.state.to.name }
                  : null
              }
            />
          }
          note={
            entry ? (
              <p className="muted">
                These are {project?.name ?? "this project"}’s record’s values. The values every
                project with it shares are registry entry {entry.hrid}’s, in{" "}
                <Link to="/entries/$ref/" params={{ ref: entry.hrid }}>
                  the entry’s own revisions
                </Link>
                .
              </p>
            ) : null
          }
        />
      ) : null}
    </>
  );
}

/** The registry entry at `/entries/$ref/revisions/$number/`. */
export function EntryRevision() {
  const { ref, number } = useParams({ from: "/entries/$ref/revisions/$number" });
  const entry = useSuspenseQuery(entryQuery(ref)).data;
  const types = useSuspenseQuery(typesQuery).data;
  const n = revisionNumber(number);
  if (n === null) return <NotFound />;
  return <EntryAt entry={entry} type={types.find((t) => t.name === entry.type)} number={n} />;
}

function EntryAt({
  entry,
  type,
  number,
}: {
  entry: Entry;
  type: EntityType | undefined;
  number: number;
}) {
  usePageTitle(`Revision ${String(number)} of ${entry.hrid}`);
  const detail = useQuery(entryRevisionQuery(entry.id, number));
  return (
    <>
      <PageHeader
        crumbs={[
          <Link to="/entries/$ref/" params={{ ref: entry.hrid }}>
            {entry.hrid}
          </Link>,
          `Revision ${String(number)}`,
        ]}
        title={`Revision ${String(number)} of ${entry.hrid}`}
        description={
          <>
            {type?.label ?? entry.type} · {entry.registry.name}
            {type?.lab_list ? " list" : " registry"}
          </>
        }
      />
      <PastNote
        what={entry.hrid}
        number={number}
        latest={entry.revision}
        detail={detail.data}
        current={
          <Link className={buttonClass("primary")} to="/entries/$ref/" params={{ ref: entry.hrid }}>
            Open {entry.hrid} as it is now
          </Link>
        }
        json={`/api/v1/entries/${entry.id}/revisions/${String(number)}`}
      />
      {detail.error ? <ProblemAlert problem={problemOf(detail.error)} /> : null}
      {detail.isPending ? <Loading label={`Loading revision ${String(number)}…`} /> : null}
      {detail.data ? (
        <RevisionBody
          detail={detail.data}
          type={type}
          owner={null}
          entity={undefined}
          note={
            <p className="muted">
              Every project with {entry.hrid} had these. Each project’s own values are in its
              record.
            </p>
          }
        />
      ) : null}
    </>
  );
}

/** What says, above all else, that this is a past revision, not the record as it is now. */
function PastNote({
  what,
  number,
  latest,
  detail,
  current,
  json,
}: {
  what: string;
  number: number;
  /** The revision it's at now. */
  latest: number;
  detail: RevisionDetail | undefined;
  current: ReactNode;
  json: string;
}) {
  const saved = detail?.created_at;
  const later = latest - number;
  return (
    <div className="alert alert-warning" role="note" aria-label="A past revision">
      <p>
        <strong>
          You’re reading revision {number} of {what}
          {later === 0 ? ", its latest," : ""} as it was
          {saved ? (
            <>
              {" "}
              saved on <UtcTime value={saved} />
            </>
          ) : null}
          .
        </strong>{" "}
        Nothing on this page can be changed.
        {later > 0
          ? ` It’s at revision ${String(latest)} now: ${String(later)} ${later === 1 ? "revision" : "revisions"} later.`
          : ""}
      </p>
      <p className="actions">
        {current}
        <a className={buttonClass()} href={json}>
          Revision {number} as JSON
        </a>
      </p>
    </div>
  );
}

/** A revision as the trail tells it, every value it had, and what it changed. */
function RevisionBody({
  detail,
  type,
  owner,
  entity,
  actions,
  note,
}: {
  detail: RevisionDetail;
  type: EntityType | undefined;
  /** Whose records its references open: the record's project's. */
  owner: { slug: string } | null | undefined;
  /** The record, whose signatures set its states: none for a registry entry. */
  entity: Entity | undefined;
  actions?: ReactNode;
  note: ReactNode;
}) {
  const ids = useId();
  const labels = new Map((type?.fields ?? []).map((f) => [f.name, f.label]));
  const number = String(detail.number);
  return (
    <>
      <section className="card" aria-labelledby={`${ids}-about`}>
        <div className="trail-title">
          <h2 className="section-title" id={`${ids}-about`}>
            Revision {number}: {revisionTitle(detail, labels, detail.entry)}
          </h2>
          {actions ? <span className="trail-actions">{actions}</span> : null}
        </div>
        <RevisionFacts entity={entity} revision={detail} labels={labels} />
      </section>
      <section className="card record-section" aria-labelledby={`${ids}-values`}>
        <div className="section-head">
          <h2 className="section-title" id={`${ids}-values`}>
            Values at revision {number}
          </h2>
          {note}
        </div>
        <RevisionValues detail={detail} type={type} owner={owner} />
      </section>
      <section className="card" aria-labelledby={`${ids}-changes`}>
        <h2 className="section-title" id={`${ids}-changes`}>
          What revision {number} changed
        </h2>
        <ChangesTable detail={detail} type={type} owner={owner} />
      </section>
    </>
  );
}

/** Every value it had: the type's fields, then any since removed, as history keeps them. */
function RevisionValues({
  detail,
  type,
  owner,
}: {
  detail: RevisionDetail;
  type: EntityType | undefined;
  owner: { slug: string } | null | undefined;
}) {
  const values = detail.values;
  const known = (type?.fields ?? []).filter((f) => f.name in values);
  const names = new Set(known.map((f) => f.name));
  const removed = Object.keys(values).filter((n) => !names.has(n) && values[n] != null);
  const set = known.filter((f) => values[f.name] != null);
  const unset = known.filter((f) => values[f.name] == null);
  const shown = { ...detail, owner_project: owner ?? null };
  return (
    <>
      {set.length + removed.length === 0 ? (
        <p className="muted field-rows-empty">No values were set.</p>
      ) : null}
      <dl className="field-rows">
        {set.map((f) => (
          <div key={f.name} className="field-row">
            <dt>{f.label}</dt>
            <dd>
              <div className="field-value">
                <Value field={f} value={values[f.name]} entity={shown} />
              </div>
            </dd>
          </div>
        ))}
        {removed.map((name) => (
          <div key={name} className="field-row">
            <dt>
              <code>{name}</code>
              <span className="cell-meta">No longer one of its type’s fields</span>
            </dt>
            <dd>
              <div className="field-value">
                <pre className="code">
                  <code>{JSON.stringify(values[name], null, 2)}</code>
                </pre>
              </div>
            </dd>
          </div>
        ))}
      </dl>
      {unset.length ? (
        <p className="muted field-rows-unset">Not set: {unset.map((f) => f.label).join(", ")}.</p>
      ) : null}
    </>
  );
}
