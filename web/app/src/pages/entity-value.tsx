/**
 * How an entity's values read on its pages: references link, files download, units show,
 * long text is cut short, lists of objects and rows are tables, and a value a viewer tab
 * shows in full says where.
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useId, useState } from "react";

import {
  formatSize,
  hasIdentity,
  quantity,
  recordSearch,
  typesQuery,
  valueText,
  type Entity,
  type EntityPageSearch,
  type EntityType,
  type FieldDef,
  type Ref,
} from "../entities";
import { isOptionRef, listSearch } from "../lab-lists";
import { objectList, type ObjectList } from "../object-lists";
import { PreviewButton } from "../previews/preview-places";
import { FIXED_FACT, REGISTRY_MARK } from "../registries";
import { Button } from "../ui";
import { FEATURES_ID, type MapOf } from "./record-map";

/** The most lines of a JSON value an entity's page shows unfolded. */
const JSON_LINES_SHOWN = 20;

/** Whose a field's value is, for a record of a registry entry (ADR 0115). */
export function registryNote(field: FieldDef): string | null {
  if (field.registry === "fact" && field.immutable) return FIXED_FACT;
  return field.registry ? REGISTRY_MARK[field.registry] : null;
}

/** A reference, linked. An entry's records share its HRID, so a link to one opens the
 * record in `project`: the referring record's, which holds what it refers to. */
function RefLink({ found, project }: { found: Ref; project: string | undefined }) {
  const types = useQuery(typesQuery).data;
  const search = project && hasIdentity(types, found.type) ? { project } : {};
  if (isOptionRef(found)) return <OptionRef found={found} project={project} />;
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

/** A lab list's option (ADR 0270): its label, as it's called now, and what's become of it
 * if no dropdown offers it any more. It's nobody's record: it leads to its list, in the
 * registry of the project the record is in, where its history and curators are. */
export function OptionRef({ found, project }: { found: Ref; project: string | undefined }) {
  const retired = found.superseded ? "superseded" : found.archived ? "archived" : null;
  return (
    <span>
      <Link
        to="/types/$/"
        params={{ _splat: found.type }}
        search={listSearch(project)}
        hash={`option-${found.code ?? ""}`}
      >
        {found.title}
      </Link>
      {retired ? <span className="muted"> ({retired})</span> : null}
    </span>
  );
}

/** Where a value leads in another system, such as its LIMS record (ADR 0082): its field's
 * link with the value in it, encoded. Only ever https, as the API gives it. */
export function linkTo(field: FieldDef, value: unknown): string | null {
  if (typeof value !== "string" || !field.link?.startsWith("https://")) return null;
  return field.link.replaceAll("{value}", encodeURIComponent(value));
}

/** A text value longer than this is cut short on a page, as a genome's bases are: shown in
 * full on asking, or in the viewer that shows it, as GitHub folds a long file and Benchling
 * keeps a sequence in its sequence view. */
const LONG_VALUE = 300;
/** How much of a long value shows before it's cut short. */
const LONG_SHOWN = 200;

/** A viewer tab that shows a field's value in full, such as a sequence's. */
export interface ShownIn {
  viewer: string;
  label: string;
  /** What its characters are: bases, residues. */
  noun: string;
  search: EntityPageSearch;
  hrid: string;
}

/** The viewer tab that shows a field, if a sequence viewer of its type does. */
export function shownIn(
  entity: Entity,
  type: EntityType | undefined,
  field: string,
): ShownIn | undefined {
  const viewer = (type?.viewers ?? []).find(
    (v) => v.viewer === "sequence" && "sequence" in v && v.sequence === field,
  );
  if (!viewer || !("alphabet" in viewer)) return undefined;
  return {
    viewer: viewer.viewer,
    label: viewer.label,
    noun: viewer.alphabet === "protein" ? "residues" : "bases",
    search: recordSearch(entity),
    hrid: entity.hrid,
  };
}

/** The sequence viewer that draws a field as its features, if one of its type's does. */
export function drawnBy(type: EntityType | undefined, field: string) {
  return (type?.viewers ?? []).find(
    (v) => v.viewer === "sequence" && "features" in v && v.features === field,
  );
}

/** A field a viewer draws, such as a plasmid's features: how many, and where to see them.
 * The rail's map shows them too; the audit trail has their JSON. */
export function DrawnBy({
  entity,
  value,
  viewer,
}: {
  entity: Entity;
  value: unknown;
  viewer: { viewer: string; label: string };
}) {
  if (!Array.isArray(value) || value.length === 0) {
    return <span className="muted">None</span>;
  }
  return (
    <span>
      {value.length.toLocaleString()} {value.length === 1 ? "feature" : "features"}.{" "}
      <Link
        to="/entities/$ref/viewers/$viewer/"
        params={{ ref: entity.hrid, viewer: viewer.viewer }}
        search={recordSearch(entity)}
      >
        Open in {viewer.label}
      </Link>
    </span>
  );
}

/** A field the page's map draws, such as a plasmid's annotated features: how many, and
 * where they're listed, in the Features card below. */
export function MappedFeatures({ map }: { map: MapOf }) {
  const n = map.features.length;
  return (
    <span>
      {n.toLocaleString()} {n === 1 ? "feature" : "features"}, drawn in its map and listed in{" "}
      <a href={`#${FEATURES_ID}`}>Features</a>
    </span>
  );
}

/** A long text value, cut short: it wraps within its cell, never widening the page. */
function LongValue({
  value,
  label,
  text,
  shown,
}: {
  value: string;
  label: string;
  text: boolean;
  shown: ShownIn | undefined;
}) {
  const [all, setAll] = useState(false);
  // eslint-disable-next-line @typescript-eslint/no-misused-spread -- code points, as the API counts
  const length = [...value].length;
  const counted = `${length.toLocaleString()} ${shown?.noun ?? "characters"}`;
  const cut = all ? value : `${value.slice(0, LONG_SHOWN)}…`;
  return (
    <span className="value-long">
      {text ? <span className="preserve-lines">{cut}</span> : <code>{cut}</code>}
      <span className="cell-meta">
        {counted}.{" "}
        <Button
          variant="quiet"
          label={`${all ? "Show less of" : "Show all of"} ${label}`}
          onClick={() => {
            setAll(!all);
          }}
        >
          {/* Its count is just before it: its name, "Show all of Bases", holds what it reads
              (WCAG 2.5.3). */}
          {all ? "Show less" : "Show all"}
        </Button>
        {shown ? (
          <>
            {" "}
            <Link
              to="/entities/$ref/viewers/$viewer/"
              params={{ ref: shown.hrid, viewer: shown.viewer }}
              search={shown.search}
            >
              Open in {shown.label}
            </Link>
          </>
        ) : null}
      </span>
    </span>
  );
}

/** One value as it reads on the page: references link, files download, units show. */
export function Value({
  field,
  value,
  entity,
  shown,
}: {
  field: FieldDef;
  value: unknown;
  /** With its owner project, references to registry entries open that project's records. */
  entity: Pick<Entity, "refs" | "files" | "people"> & { owner_project?: { slug: string } | null };
  /** The viewer tab that shows it in full, for a long value. */
  shown?: ShownIn | undefined;
}) {
  if (value === null || value === undefined) {
    return <span className="muted">Not set</span>;
  }
  if (Array.isArray(value) && field.items === "row") {
    if (value.length === 0) return <span className="muted">None</span>;
    return <Rows field={field} rows={value as Record<string, unknown>[]} entity={entity} />;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="muted">None</span>;
    // A list of objects, such as a plasmid's features: a table of what they hold, never JSON.
    const objects = objectList(value);
    if (objects) return <ObjectTable list={objects} label={field.label} />;
    const item: FieldDef = { ...field, type: field.items ?? "string" };
    // A long list of JSON, such as a sequence's features, is folded, as a long JSON value
    // is: it would otherwise fill the page.
    const lines = item.type === "json" ? JSON.stringify(value, null, 2).split("\n").length : 0;
    if (lines > JSON_LINES_SHOWN) {
      return (
        <details className="disclosure">
          <summary>{`Show its JSON (${value.length.toLocaleString()} items)`}</summary>
          <pre className="code">
            <code>{JSON.stringify(value, null, 2)}</code>
          </pre>
        </details>
      );
    }
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
  if (
    typeof value === "string" &&
    (field.type === "string" || field.type === "text") &&
    value.length > LONG_VALUE
  ) {
    return (
      <LongValue value={value} label={field.label} text={field.type === "text"} shown={shown} />
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
          </span>{" "}
          <PreviewButton id={value as string} name={found.name} mediaType={found.media_type} />
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
    case "json": {
      const text = JSON.stringify(value, null, 2);
      const code = (
        <pre className="code">
          <code>{text}</code>
        </pre>
      );
      // A long value, such as what a viewer or a tab draws, is folded: it would otherwise
      // fill the page.
      const lines = text.split("\n").length;
      if (lines <= JSON_LINES_SHOWN) return code;
      return (
        <details className="disclosure">
          <summary>{`Show its JSON (${lines.toLocaleString()} lines)`}</summary>
          {code}
        </details>
      );
    }
    default:
      return <>{valueText(field, value, entity)}</>;
  }
}

/** The rows of a list of objects shown before it's asked for all of them. */
const OBJECTS_SHOWN = 10;

/** A list of objects as a small table (see object-lists.ts): a column per key they share,
 * the first rows, and the rest on asking. */
export function ObjectTable({ list, label }: { list: ObjectList; label: string }) {
  const [all, setAll] = useState(false);
  const id = useId();
  const rows = all ? list.rows : list.rows.slice(0, OBJECTS_SHOWN);
  const more = list.rows.length - OBJECTS_SHOWN;
  return (
    <div className="object-list">
      {/* A wide one scrolls across within the value, by keyboard too. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable region */}
      <div className="table-scroll" tabIndex={0} role="group" aria-label={label}>
        <table className="value-rows" id={`${id}-table`}>
          <caption className="visually-hidden">{label}</caption>
          <thead>
            <tr>
              {list.columns.map((c) => (
                <th key={c.key} scope="col" className={c.numeric ? "numeric" : undefined}>
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              // Rows can repeat; their position tells them apart.
              <tr key={index}>
                {list.columns.map((c) => {
                  const text = c.text(row);
                  return (
                    <td key={c.key} className={c.numeric ? "numeric" : undefined}>
                      {text || <span className="muted">—</span>}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {more > 0 ? (
        <button
          type="button"
          className="button button-quiet object-list-more"
          aria-controls={`${id}-table`}
          aria-expanded={all}
          onClick={() => {
            setAll(!all);
          }}
        >
          {all ? "Show fewer" : `Show all ${list.rows.length.toLocaleString()}`}
        </button>
      ) : null}
    </div>
  );
}

/** A list of rows (ADR 0137) as a table: a column per row field, a row per row. */
function Rows({
  field,
  rows,
  entity,
}: {
  field: FieldDef;
  rows: Record<string, unknown>[];
  entity: Parameters<typeof Value>[0]["entity"];
}) {
  const cells = field.row?.fields ?? [];
  return (
    <div className="table-scroll">
      <table className="value-rows">
        <caption className="visually-hidden">{field.label}</caption>
        <thead>
          <tr>
            {cells.map((c) => (
              <th key={c.name} scope="col">
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            // Rows can repeat; their position tells them apart.
            <tr key={index}>
              {cells.map((c) => (
                <td key={c.name}>
                  {row[c.name] === null || row[c.name] === undefined ? (
                    <span className="muted">—</span>
                  ) : (
                    <Value field={c} value={row[c.name]} entity={entity} />
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
