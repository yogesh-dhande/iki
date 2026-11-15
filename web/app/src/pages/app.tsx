/**
 * An app's page (core.App, ADR 0090): its panels on a twelve-column grid, each one of
 * Iki's components, reading what the panels before it offer. It shows what the person
 * viewing it may read: each grid is their search, each viewer an entity they can open.
 * What expressions make is shown as text, never as HTML.
 */

import { unwrap, type Problem, type Schemas } from "@ikisci/sdk";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";

import { iki, problemOf } from "../api";
import {
  conditionTemplates,
  entityValue,
  fillConditions,
  readSpec,
  renderTemplates,
  type EntitiesProps,
  type EntityValue,
  type Offers,
  type Panel,
  type Rendered,
} from "../apps";
import { ENTITIES, entityQuery, valueText, type Entity, type EntityType } from "../entities";
import { Loading, ProblemAlert, RelativeTime } from "../ui";
import { ViewerBody } from "./entity-viewer";

/** An app's panels. An app about an entity shows only on that entity's page, `about`
 * it; on its own, it says where it's shown. */
export function AppPanel({
  app,
  types,
  about,
}: {
  app: Entity;
  types: EntityType[];
  about?: Entity;
}) {
  const spec = readSpec(app.values["spec"]);
  const [offers, setOffers] = useState<Offers>({});
  if (!spec) {
    return (
      <section className="card">
        <p className="muted">This version of Iki can’t show this app.</p>
      </section>
    );
  }
  if (spec.entity && !about) {
    const type = types.find((t) => t.name === spec.entity?.type);
    return (
      <section className="card">
        <p className="muted">
          This app is about one {type?.label ?? spec.entity.type}. It’s a tab on each one’s page
          {app.owner_project ? ` in ${app.owner_project.name}` : ""}.
        </p>
      </section>
    );
  }
  // An app about an entity reads it as `entity`, as the server checked.
  const base: Offers = about
    ? { entity: entityValue(about) as unknown as Record<string, unknown> }
    : {};
  return (
    <div className="app-grid">
      {spec.panels.map((panel, index) => {
        // A panel reads only the panels before it, as the server checked, and waits for
        // each of them to offer what it has.
        const before: Offers = { ...base };
        let ready = true;
        for (const p of spec.panels.slice(0, index)) {
          const offered = offers[p.id];
          if (offered) before[p.id] = offered;
          else ready = false;
        }
        const offer = (values: Record<string, unknown>) => {
          setOffers((current) => ({ ...current, [panel.id]: values }));
        };
        return (
          <AppCard key={panel.id} panel={panel}>
            {ready ? (
              <PanelBody panel={panel} before={before} offer={offer} app={app} types={types} />
            ) : (
              <Loading label="Waiting for the panels before it…" />
            )}
          </AppCard>
        );
      })}
    </div>
  );
}

/** How the app is built: its spec, as saved. */
export function AppSpec({ app }: { app: Entity }) {
  return (
    <section className="card" aria-labelledby="app-spec-title">
      <h2 className="section-title" id="app-spec-title">
        How it’s built
      </h2>
      <details>
        <summary>Its spec, as saved</summary>
        <pre className="app-spec">{JSON.stringify(app.values["spec"], null, 2)}</pre>
      </details>
    </section>
  );
}

function AppCard({ panel, children }: { panel: Panel; children: React.ReactNode }) {
  const headingId = `app-panel-${panel.id}`;
  const style = { gridColumn: `span ${String(panel.width ?? 12)}` };
  // A panel with a title is a region of the page, named by it.
  return panel.title ? (
    <section className="card app-panel" style={style} aria-labelledby={headingId}>
      <h2 className="section-title" id={headingId}>
        {panel.title}
      </h2>
      {children}
    </section>
  ) : (
    <div className="card app-panel" style={style}>
      {children}
    </div>
  );
}

interface BodyProps {
  panel: Panel;
  before: Offers;
  offer: (values: Record<string, unknown>) => void;
  app: Entity;
  types: EntityType[];
}

function PanelBody(props: BodyProps) {
  const { panel } = props;
  switch (panel.component) {
    case "iki.entities":
      return <EntitiesPanel {...props} entities={panel.props} />;
    case "iki.viewer":
      return <ViewerPanel {...props} entity={panel.props.entity} viewer={panel.props.viewer} />;
    case "iki.text":
      return <TextPanel {...props} text={panel.props.text} />;
  }
}

/** Templates' values, rendered again whenever what they read changes. Until the new ones
 * come, the last ones stay, so the page doesn't flicker as people pick. */
function useRendered(
  templates: string[],
  before: Offers,
  rows?: unknown[],
): { values: Rendered[][] | undefined; error: string | undefined } {
  const key = JSON.stringify([templates, before, rows ?? null]);
  const [state, setState] = useState<{ values?: Rendered[][]; error?: string }>({});
  const none = templates.length === 0;
  useEffect(() => {
    if (none) return;
    let live = true;
    renderTemplates(templates, before, rows).then(
      (values) => {
        if (live) setState({ values });
      },
      (error: unknown) => {
        if (live) setState({ error: error instanceof Error ? error.message : String(error) });
      },
    );
    return () => {
      live = false;
    };
    // `key` stands for what it's rendered from.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  if (none) return { values: [], error: undefined };
  return { values: state.values, error: state.error };
}

/** A value as text, as iki-wasm writes values into text. */
function shown(rendered: Rendered | undefined): React.ReactNode {
  if (!rendered) return null;
  if ("error" in rendered) return <span className="app-error">{rendered.error}</span>;
  const { value } = rendered;
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return String(value);
  if (typeof value === "object" && "title" in value && typeof value.title === "string") {
    return value.title;
  }
  return JSON.stringify(value);
}

function TextPanel({ text, before, offer }: BodyProps & { text: string }) {
  const { values, error } = useRendered([text], before);
  useEffect(() => {
    // It offers nothing, but later panels wait for it to say so.
    offer({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  if (error) return <p className="app-error">{error}</p>;
  if (!values) return <Loading label="Working it out…" />;
  return <p className="app-text">{shown(values[0]?.[0])}</p>;
}

type SearchBody = Schemas["SearchIn"];

/** A grid's rows while its conditions can't be worked out: the same each render. */
const NONE: Entity[] = [];

/** Columns every entity has, besides its fields, as the server checks a grid's. */
const COLUMNS: Record<string, { label: string; cell: (e: Entity) => React.ReactNode }> = {
  hrid: { label: "ID", cell: (e) => e.hrid },
  title: { label: "Title", cell: (e) => (e.title === e.hrid ? "" : e.title) },
  type: { label: "Type", cell: (e) => e.type },
  created_at: { label: "Created", cell: (e) => <RelativeTime value={e.created_at} /> },
  updated_at: { label: "Last changed", cell: (e) => <RelativeTime value={e.updated_at} /> },
};

function EntitiesPanel({
  panel,
  entities,
  before,
  offer,
  app,
  types,
}: BodyProps & { entities: EntitiesProps }) {
  const type = types.find((t) => t.name === entities.type);
  const project = (entities.scope ?? "project") === "project" ? app.owner_project : null;
  // Conditions whose values are expressions are worked out first; one that can't be (a
  // grid's pick not made yet, say) leaves the grid with nothing to show.
  const conditions = useRendered(conditionTemplates(entities.where), before);
  const values = conditions.values?.map((v) => v[0]);
  const unworked =
    conditions.error ??
    values?.flatMap((v) => (v && "error" in v ? [v.error] : []))[0] ??
    (values?.some((v) => !v || ("value" in v && (v.value === null || v.value === undefined)))
      ? "a condition’s value is empty."
      : undefined);
  const where = (
    values && unworked === undefined
      ? fillConditions(
          entities.where,
          values.map((v) => (v && "value" in v ? v.value : null)),
        )
      : undefined
  ) as SearchBody["where"] | undefined;
  const body: SearchBody = {
    type: entities.type,
    exact_type: false,
    limit: entities.limit ?? 25,
    archived: false,
    ...(project ? { project: project.id } : {}),
    ...(where ? { where } : {}),
    ...(entities.q ? { q: entities.q } : {}),
    ...(entities.sort
      ? { sort: { field: entities.sort, descending: entities.descending ?? false } }
      : {}),
  };
  const found = useQuery({
    queryKey: [...ENTITIES, "app", app.id, panel.id, body],
    queryFn: async () => unwrap(await iki.POST("/api/v1/entities/search", { body })),
    retry: false,
    enabled: !entities.where || where !== undefined,
  });
  const items = unworked === undefined ? found.data?.items : NONE;
  const rows = useMemo(() => (items ?? []).map(entityValue), [items]);
  const [picked, setPicked] = useState<ReadonlySet<string>>(new Set());
  const selection = useMemo(() => rows.filter((r) => picked.has(r.hrid)), [rows, picked]);
  const settled = items !== undefined || found.isError;
  useEffect(() => {
    // Once it has found them (or couldn't), so later panels don't read an empty grid.
    if (settled) offer({ rows, selection });
    // `offer` is new each render; what's offered is what matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settled, rows, selection]);
  const columns = entities.columns ?? [{ field: "title" }];
  const templates = columns.flatMap((c) => ("value" in c ? [c.value] : []));
  const rendered = useRendered(templates, before, rows);
  if (unworked !== undefined) {
    return <p className="muted">Nothing to show yet: {unworked}</p>;
  }
  if (found.isError) return <SearchProblem problem={problemOf(found.error)} />;
  if (!items) return <Loading label="Finding them…" />;
  if (items.length === 0) return <p className="muted">There are none to show.</p>;
  const select = entities.select ?? "one";
  let next = 0;
  const cells = columns.map((c) => {
    if ("value" in c) {
      const index = next++;
      return {
        label: c.label,
        cell: (_: Entity, row: number) => shown(rendered.values?.[index]?.[row]),
      };
    }
    const field = type?.fields.find((f) => f.name === c.field);
    if (field) {
      return {
        label: c.label ?? field.label,
        cell: (e: Entity) => valueText(field, e.values[field.name], e),
      };
    }
    const column = COLUMNS[c.field] ?? { label: c.field, cell: () => "" };
    return { label: c.label ?? column.label, cell: column.cell };
  });
  return (
    <>
      {rendered.error ? <p className="app-error">{rendered.error}</p> : null}
      <div className="app-table">
        <table className="table app-entities">
          <thead>
            <tr>
              {select === "none" ? null : (
                <th scope="col" className="app-pick">
                  <span className="visually-hidden">Pick</span>
                </th>
              )}
              <th scope="col">ID</th>
              {cells.map((c, i) => (
                <th key={i} scope="col">
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {items.map((e, row) => (
              <tr key={e.id}>
                {select === "none" ? null : (
                  <td className="app-pick">
                    <input
                      type={select === "one" ? "radio" : "checkbox"}
                      className="grid-tick"
                      name={`app-${panel.id}`}
                      aria-label={`Pick ${e.title}`}
                      checked={picked.has(e.hrid)}
                      onChange={(event) => {
                        const on = event.currentTarget.checked;
                        setPicked((current) => {
                          if (select === "one") return new Set(on ? [e.hrid] : []);
                          const out = new Set(current);
                          if (on) out.add(e.hrid);
                          else out.delete(e.hrid);
                          return out;
                        });
                      }}
                    />
                  </td>
                )}
                <td>
                  <Link to="/entities/$ref/" params={{ ref: e.hrid }}>
                    {e.hrid}
                  </Link>
                </td>
                {cells.map((c, i) => (
                  <td key={i}>{c.cell(e, row)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

/** Why a grid's search was refused. A panel has no fields to highlight, so each error
 * says where in its props it is, as the server's checks do. */
function SearchProblem({ problem }: { problem: Problem }) {
  if (!problem.errors?.length) return <ProblemAlert problem={problem} />;
  return (
    <div className="alert alert-error" role="alert">
      <p>{problem.detail}</p>
      <ul>
        {problem.errors.map((e, i) => {
          const where = (e.pointer ?? e.parameter ?? "").replace(/^#\//, "").replaceAll("/", ".");
          return (
            <li key={i}>
              {where ? (
                <>
                  <code>{where}</code>:{" "}
                </>
              ) : null}
              {e.detail}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** Which entity an `entity` prop's value is: an HRID, an entity, or the first of a list. */
function hridOf(value: unknown): string | null {
  const first = Array.isArray(value) ? (value as unknown[])[0] : value;
  if (typeof first === "string" && first) return first;
  if (typeof first === "object" && first !== null && "hrid" in first) {
    const { hrid } = first as EntityValue;
    return typeof hrid === "string" ? hrid : null;
  }
  return null;
}

function ViewerPanel({
  entity: template,
  viewer: wanted,
  before,
  offer,
  types,
}: BodyProps & { entity: string; viewer?: string | undefined }) {
  const literal = !template.includes("{{");
  const rendered = useRendered(literal ? [] : [template], before);
  const value = literal ? { value: template } : rendered.values?.[0]?.[0];
  const hrid = value && "value" in value ? hridOf(value.value) : null;
  const found = useQuery({ ...entityQuery(hrid ?? ""), enabled: Boolean(hrid) });
  const entity = found.data;
  useEffect(() => {
    offer({ entity: entity ? entityValue(entity) : null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entity]);
  if (rendered.error) return <p className="app-error">{rendered.error}</p>;
  if (value && "error" in value) return <p className="app-error">{value.error}</p>;
  if (!literal && !rendered.values) return <Loading label="Working it out…" />;
  if (!hrid) return <p className="muted">Nothing to show yet.</p>;
  if (found.isError) return <ProblemAlert problem={problemOf(found.error)} />;
  if (!entity) return <Loading label={`Opening ${hrid}…`} />;
  const type = types.find((t) => t.name === entity.type);
  const viewer = type?.viewers.find((v) => v.viewer === (wanted ?? type.viewers[0]?.viewer));
  if (!type || !viewer) {
    return (
      <p className="muted">
        {entity.hrid}’s type has no {wanted ?? ""} viewer.
      </p>
    );
  }
  return (
    <>
      <p className="muted">
        {viewer.label} of{" "}
        <Link to="/entities/$ref/" params={{ ref: entity.hrid }}>
          {entity.title}
        </Link>
      </p>
      <ViewerBody key={entity.id} source={entity} type={type} viewer={viewer} />
    </>
  );
}
