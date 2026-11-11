/**
 * An entity's lineage: what runs made it from, what that was made from, and
 * what was made from it, as a graph or, read in order, as a list of steps.
 */

import { useQuery } from "@tanstack/react-query";
import { Link, useSearch } from "@tanstack/react-router";
import { useId, useMemo, useState } from "react";

import { problemOf } from "../api";
import type { EntityType } from "../entities";
import { usePageTitle } from "../layout";
import {
  connected,
  GENERATIONS,
  layout,
  lineageQuery,
  MAX_GENERATIONS,
  NODE_HEIGHT,
  NODE_WIDTH,
  type Layout,
  type LineageEntity,
  type LineageGraph,
  type LineageRun,
} from "../lineage";
import { actionsQuery } from "../runs";
import { buttonClass, Loading, ProblemAlert } from "../ui";
import { EntityTabs, useEntity } from "./entity";

/** How the page names things: types and actions by their labels. */
interface Names {
  root: string;
  type: (name: string) => string;
  action: (name: string) => string;
}

export function EntityLineage() {
  const { entity, type, types } = useEntity();
  usePageTitle(`Lineage of ${entity.title} (${entity.hrid})`);
  const search = useSearch({ from: "/entities/$ref/lineage" });
  const up = search.up ?? GENERATIONS;
  const down = search.down ?? GENERATIONS;
  const graph = useQuery(lineageQuery(entity.hrid, up, down));
  const actions = useQuery(actionsQuery).data;
  const names: Names = {
    root: entity.id,
    type: (name) => types.find((t: EntityType) => t.name === name)?.label ?? name,
    action: (name) => actions?.find((a) => a.name === name)?.label ?? name,
  };
  const data = graph.data;
  const further = (by: "up" | "down") => ({
    ...search,
    [by]: Math.min(MAX_GENERATIONS, (by === "up" ? up : down) + GENERATIONS),
  });
  return (
    <>
      <EntityTabs entity={entity} type={type} />
      <p className="muted">
        What runs made it from, and what they made from it, earliest first. Entities made or changed
        by hand have no lineage.
      </p>
      {graph.error ? <ProblemAlert problem={problemOf(graph.error)} /> : null}
      {graph.isPending ? <Loading label="Loading the lineage…" /> : null}
      {data && !data.links.length ? (
        <p className="empty">
          No lineage you can see: no run made {entity.title}, and nothing you can read has been made
          from it.
        </p>
      ) : null}
      {data?.links.length ? (
        <>
          <div className="toolbar">
            <nav className="segmented" aria-label="Show lineage as">
              <Link
                to="/entities/$ref/lineage/"
                params={{ ref: entity.hrid }}
                search={{ ...search, view: undefined }}
                aria-current={search.view ? undefined : "page"}
              >
                Graph
              </Link>
              <Link
                to="/entities/$ref/lineage/"
                params={{ ref: entity.hrid }}
                search={{ ...search, view: "list" }}
                aria-current={search.view ? "page" : undefined}
              >
                List
              </Link>
            </nav>
            <div className="toolbar-end lineage-more">
              {data.earlier && up < MAX_GENERATIONS ? (
                <Link
                  to="/entities/$ref/lineage/"
                  params={{ ref: entity.hrid }}
                  search={further("up")}
                  className={buttonClass()}
                >
                  Show earlier
                </Link>
              ) : null}
              {data.later && down < MAX_GENERATIONS ? (
                <Link
                  to="/entities/$ref/lineage/"
                  params={{ ref: entity.hrid }}
                  search={further("down")}
                  className={buttonClass()}
                >
                  Show later
                </Link>
              ) : null}
            </div>
          </div>
          <Notes graph={data} up={up} down={down} />
          {search.view === "list" ? (
            <Steps graph={data} names={names} />
          ) : (
            <Graph graph={data} names={names} />
          )}
        </>
      ) : null}
    </>
  );
}

/** What isn't shown, and how to see it. */
function Notes({ graph, up, down }: { graph: LineageGraph; up: number; down: number }) {
  const notes = [];
  if (graph.capped) {
    notes.push(
      "There’s more than fits on one page, so some is left out. Open an entity in it to follow its lineage from there.",
    );
  }
  if ((graph.earlier && up >= MAX_GENERATIONS) || (graph.later && down >= MAX_GENERATIONS)) {
    notes.push(
      `It goes further than ${String(MAX_GENERATIONS)} generations. Open the furthest entity to follow it on.`,
    );
  }
  return notes.length ? (
    <div className="alert alert-warning" role="note">
      {notes.map((n) => (
        <p key={n}>{n}</p>
      ))}
    </div>
  ) : null;
}

function EntityLabel({ entity, names }: { entity: LineageEntity; names: Names }) {
  const meta = [
    entity.title === entity.hrid ? null : entity.hrid,
    names.type(entity.type),
    entity.archived ? "Archived" : null,
  ].filter(Boolean);
  return (
    <>
      <span className="lineage-title">{entity.title}</span>{" "}
      <span className="lineage-meta">{meta.join(" · ")}</span>
      {entity.id === names.root ? <span className="visually-hidden"> (this entity)</span> : null}
    </>
  );
}

/** An entity, linked to its own lineage, so people can follow it on. */
function EntityNode({
  entity,
  names,
  className,
  ...point
}: {
  entity: LineageEntity;
  names: Names;
  className?: string;
} & Pointing) {
  const classes = `lineage-node lineage-entity ${className ?? ""}`;
  return entity.id === names.root ? (
    <div className={`${classes} is-root`} {...point}>
      <EntityLabel entity={entity} names={names} />
    </div>
  ) : (
    <Link to="/entities/$ref/lineage/" params={{ ref: entity.hrid }} className={classes} {...point}>
      <EntityLabel entity={entity} names={names} />
    </Link>
  );
}

function RunNode({
  run,
  names,
  className,
  ...point
}: {
  run: LineageRun;
  names: Names;
  className?: string;
} & Pointing) {
  const classes = `lineage-node lineage-run ${className ?? ""}`;
  return run.run ? (
    <Link to="/runs/$ref/" params={{ ref: run.run.hrid }} className={classes} {...point}>
      <span className="lineage-title">{names.action(run.run.action)}</span>{" "}
      <span className="lineage-meta">{run.run.hrid}</span>
    </Link>
  ) : (
    <div className={`${classes} is-hidden`} {...point}>
      <span className="lineage-title">A run you can’t see</span>{" "}
      <span className="lineage-meta">In a project you can’t read</span>
    </div>
  );
}

interface Pointing {
  onMouseEnter?: () => void;
  onMouseLeave?: () => void;
  onFocus?: () => void;
  onBlur?: () => void;
}

/** The graph: columns of entities and runs, with lines from each to what came of it. */
function Graph({ graph, names }: { graph: LineageGraph; names: Names }) {
  const shape: Layout = useMemo(() => layout(graph), [graph]);
  const [lit, setLit] = useState<string | null>(null);
  const on = useMemo(() => (lit ? connected(shape.edges, lit) : null), [lit, shape]);
  const arrow = useId();
  const caption = useId();
  const point = (key: string): Pointing => ({
    onMouseEnter: () => {
      setLit(key);
    },
    onMouseLeave: () => {
      setLit(null);
    },
    onFocus: () => {
      setLit(key);
    },
    onBlur: () => {
      setLit(null);
    },
  });
  const state = (key: string) => (on ? (on.has(key) ? "is-lit" : "is-dim") : "");
  return (
    <figure className="lineage">
      <figcaption className="muted" id={caption}>
        {graph.entities.length} {graph.entities.length === 1 ? "entity" : "entities"} and{" "}
        {graph.runs.length} {graph.runs.length === 1 ? "run" : "runs"}, earliest on the left. Point
        at one to see where it came from and what came of it.
      </figcaption>
      {/* A scrolling region people reach with Tab, so the keyboard scrolls it. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable region */}
      <div className="lineage-scroll" tabIndex={0} role="group" aria-labelledby={caption}>
        <div
          className="lineage-canvas"
          style={{ width: `${String(shape.width)}rem`, height: `${String(shape.height)}rem` }}
        >
          <svg
            className="lineage-lines"
            aria-hidden="true"
            focusable="false"
            viewBox={`0 0 ${String(shape.width)} ${String(shape.height)}`}
            preserveAspectRatio="none"
          >
            <defs>
              <marker
                id={arrow}
                viewBox="0 0 10 10"
                refX="10"
                refY="5"
                markerWidth="7"
                markerHeight="7"
                markerUnits="userSpaceOnUse"
                orient="auto-start-reverse"
              >
                <path className="lineage-arrow" d="M 0 0 L 10 5 L 0 10 z" />
              </marker>
            </defs>
            {shape.edges.map((e) => (
              <path
                key={`${e.from}>${e.to}`}
                className={`lineage-line ${on ? (on.has(e.from) && on.has(e.to) ? "is-lit" : "is-dim") : ""}`}
                d={e.path}
                vectorEffect="non-scaling-stroke"
                markerEnd={`url(#${arrow})`}
              />
            ))}
          </svg>
          {shape.nodes.map((n) => (
            <div
              key={n.key}
              className="lineage-place"
              style={{
                left: `${String(n.x)}rem`,
                top: `${String(n.y)}rem`,
                width: `${String(NODE_WIDTH)}rem`,
                height: `${String(NODE_HEIGHT)}rem`,
              }}
            >
              {n.kind === "entity" ? (
                <EntityNode
                  entity={n.entity}
                  names={names}
                  className={state(n.key)}
                  {...point(n.key)}
                />
              ) : (
                <RunNode run={n.run} names={names} className={state(n.key)} {...point(n.key)} />
              )}
            </div>
          ))}
        </div>
      </div>
    </figure>
  );
}

/** The same, as steps in order: each run, what it was given and what it made. */
function Steps({ graph, names }: { graph: LineageGraph; names: Names }) {
  const shape = useMemo(() => layout(graph), [graph]);
  const entities = new Map(graph.entities.map((e) => [e.id, e]));
  const steps = shape.nodes.flatMap((n) => (n.kind === "run" ? [n.run] : []));
  const entity = (id: string) => {
    const found = entities.get(id);
    return found ? (
      found.id === names.root ? (
        <strong>
          <EntityLabel entity={found} names={names} />
        </strong>
      ) : (
        <Link to="/entities/$ref/lineage/" params={{ ref: found.hrid }} search={{ view: "list" }}>
          <EntityLabel entity={found} names={names} />
        </Link>
      )
    ) : null;
  };
  return (
    <ol className="lineage-steps">
      {steps.map((run) => {
        const links = graph.links.filter((l) => l.run === run.key);
        const given = [...new Map(links.map((l) => [l.parent, l])).values()];
        const made = [...new Set(links.map((l) => l.entity))];
        return (
          <li key={run.key} className="card">
            <h2 className="section-title">
              {run.run ? (
                <>
                  <Link to="/runs/$ref/" params={{ ref: run.run.hrid }}>
                    {run.run.hrid}
                  </Link>{" "}
                  · {names.action(run.run.action)}
                </>
              ) : (
                "A run you can’t see, in a project you can’t read"
              )}
            </h2>
            <dl className="lineage-step">
              <dt>Given</dt>
              <dd>
                <ul>
                  {given.map((l) => (
                    <li key={l.parent}>
                      {entity(l.parent)}{" "}
                      <span className="muted">
                        as {l.input}, at revision {l.revision}
                      </span>
                    </li>
                  ))}
                </ul>
              </dd>
              <dt>Made</dt>
              <dd>
                <ul>
                  {made.map((id) => (
                    <li key={id}>{entity(id)}</li>
                  ))}
                </ul>
              </dd>
            </dl>
          </li>
        );
      })}
    </ol>
  );
}
