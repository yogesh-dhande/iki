/**
 * What a revision changed, field by field, before and after: in a record's Audit trail, and
 * for each of a registry entry's revisions, on the entry's own page (ADR 0115).
 */

import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";

import { problemOf } from "../api";
import { personName, type EntityType, type Revision, type RevisionDetail } from "../entities";
import { entryRevisionQuery } from "../registries";
import { Button, Loading, ProblemAlert, RelativeTime } from "../ui";
import { Value } from "./entity-value";
import { KIND, Via } from "./revision-text";

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
