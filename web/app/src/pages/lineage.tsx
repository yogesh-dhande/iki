/**
 * An entity's lineage (ADR 0123): what it was derived from (upstream) and what was derived
 * from it (downstream), through references, runs and files. As a tree that reads as the
 * walk went, a list that says why each is there and downloads as CSV (the impact list), or
 * a graph of a few steps each way.
 */

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import {
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";

import { problemOf } from "../api";
import { apiRef, type EntityType } from "../entities";
import { usePageTitle } from "../layout";
import {
  connected,
  DEFAULT_DEPTH,
  EDGE_KINDS,
  GRAPH_STEPS,
  layout,
  lineageCsvUrl,
  lineageGraphQuery,
  lineageWalkQuery,
  MAX_DEPTH,
  MAX_GRAPH_STEPS,
  NODE_HEIGHT,
  NODE_WIDTH,
  stepWords,
  walkTree,
  type Direction,
  type EdgeKind,
  type Layout,
  type LineageEntity,
  type LineageGraph,
  type LineageRun,
  type LineageSearch,
  type TreeNode,
  type Walk,
  type WalkEntity,
  type WalkFilter,
  type WalkItem,
} from "../lineage";
import { actionsQuery } from "../runs";
import {
  Badge,
  Button,
  buttonClass,
  CheckboxesField,
  Loading,
  ProblemAlert,
  SelectField,
} from "../ui";
import { useEntity } from "./entity-header";

/** How the page names things: types and actions by their labels. */
interface Names {
  root: string;
  type: (name: string) => string;
  action: (name: string) => string;
}

const ROUTE = "/entities/$ref/lineage/";

export function EntityLineage() {
  const { entity, types } = useEntity();
  usePageTitle(`Lineage of ${entity.title} (${entity.hrid})`);
  const search = useSearch({ from: "/entities/$ref/lineage" });
  const actions = useQuery(actionsQuery).data;
  const names: Names = {
    root: entity.id,
    type: (name) => types.find((t: EntityType) => t.name === name)?.label ?? name,
    action: (name) => actions?.find((a) => a.name === name)?.label ?? name,
  };
  const direction: Direction = search.direction ?? "upstream";
  const view = search.view ?? "tree";
  const at = (change: Partial<LineageSearch>): LineageSearch => ({ ...search, ...change });
  return (
    <>
      <p className="muted">
        What {entity.title} was derived from, and what was derived from it: through what its fields
        name, the runs that made things from it, and the files they read. Only what you can read is
        here.
      </p>
      <div className="toolbar">
        {view === "graph" ? null : (
          <nav className="segmented" aria-label="Which way">
            <Link
              to={ROUTE}
              params={{ ref: entity.hrid }}
              search={at({ direction: undefined })}
              aria-current={direction === "upstream" ? "page" : undefined}
            >
              Derived from
            </Link>
            <Link
              to={ROUTE}
              params={{ ref: entity.hrid }}
              search={at({ direction: "downstream" })}
              aria-current={direction === "downstream" ? "page" : undefined}
            >
              Used by
            </Link>
          </nav>
        )}
        <nav className="segmented" aria-label="Show lineage as">
          {(
            [
              ["tree", "Tree"],
              ["list", "List"],
              ["graph", "Graph"],
            ] as const
          ).map(([value, label]) => (
            <Link
              key={value}
              to={ROUTE}
              params={{ ref: entity.hrid }}
              search={at({ view: value === "tree" ? undefined : value })}
              aria-current={view === value ? "page" : undefined}
            >
              {label}
            </Link>
          ))}
        </nav>
      </div>
      {view === "graph" ? (
        <GraphView entityRef={apiRef(entity)} hrid={entity.hrid} names={names} search={search} />
      ) : (
        <WalkView
          entityRef={apiRef(entity)}
          title={entity.title}
          hrid={entity.hrid}
          direction={direction}
          view={view}
          names={names}
          types={types}
          search={search}
        />
      )}
    </>
  );
}

// --- Tree and list -----------------------------------------------------------------------------

function WalkView({
  entityRef,
  title,
  hrid,
  direction,
  view,
  names,
  types,
  search,
}: {
  entityRef: string;
  title: string;
  hrid: string;
  direction: Direction;
  view: "tree" | "list";
  names: Names;
  types: EntityType[];
  search: LineageSearch;
}) {
  const navigate = useNavigate();
  const filter: WalkFilter = {
    depth: search.depth ?? DEFAULT_DEPTH,
    edges: search.edges ?? [],
    type: search.type ?? "",
  };
  const walk = useInfiniteQuery(lineageWalkQuery(entityRef, direction, filter));
  const pages = walk.data?.pages ?? [];
  const items = pages.flatMap((p) => p.items);
  const first = pages[0];
  const set = (change: Partial<LineageSearch>) => {
    void navigate({ to: ROUTE, params: { ref: hrid }, search: { ...search, ...change } });
  };
  const narrowed = filter.edges.length > 0 || filter.type !== "";
  return (
    <>
      <form
        className="toolbar"
        aria-label="Narrow the lineage"
        onSubmit={(event) => {
          event.preventDefault();
        }}
      >
        <SelectField
          label="How far"
          value={String(filter.depth)}
          onChange={(v) => {
            set({ depth: Number(v) === DEFAULT_DEPTH ? undefined : Number(v) });
          }}
          options={Array.from({ length: MAX_DEPTH }, (_, i) => i + 1).map((n) => ({
            value: String(n),
            label: n === 1 ? "1 step" : `${String(n)} steps`,
          }))}
        />
        <SelectField
          label="Show"
          value={filter.type}
          onChange={(v) => {
            set({ type: v || undefined });
          }}
          options={[
            { value: "", label: "Every type" },
            ...types
              .filter((t) => t.name !== "iki/core.Entity")
              .map((t) => ({ value: t.name, label: t.label })),
          ]}
        />
        <CheckboxesField<EdgeKind>
          label="Through"
          value={filter.edges.length ? filter.edges : EDGE_KINDS.map((k) => k.value)}
          onChange={(v) => {
            set({ edges: v.length && v.length < EDGE_KINDS.length ? v : undefined });
          }}
          options={EDGE_KINDS}
        />
      </form>
      {walk.error ? <ProblemAlert problem={problemOf(walk.error)} /> : null}
      {walk.isPending ? <Loading label="Loading the lineage…" /> : null}
      {first ? (
        <Summary walk={first} title={title} direction={direction} narrowed={narrowed} />
      ) : null}
      {first && items.length ? (
        <>
          <div className="toolbar">
            <a
              className={buttonClass()}
              href={lineageCsvUrl(entityRef, direction, filter)}
              download={`${hrid}-${direction}.csv`}
            >
              Download all as CSV
            </a>
          </div>
          {view === "list" ? (
            <List items={items} root={first.root.hrid} direction={direction} names={names} />
          ) : (
            <Tree
              items={items}
              root={first.root.hrid}
              direction={direction}
              names={names}
              label={summaryLabel(first)}
            />
          )}
          {walk.hasNextPage ? (
            <div className="actions">
              <Button
                busy={walk.isFetchingNextPage}
                busyText="Loading…"
                onClick={() => void walk.fetchNextPage()}
              >
                Show more
              </Button>
            </div>
          ) : null}
        </>
      ) : null}
    </>
  );
}

function summaryLabel(walk: Walk): string {
  return walk.direction === "upstream" ? "What it was derived from" : "What was derived from it";
}

/** How many, how far, and what isn't shown. */
function Summary({
  walk,
  title,
  direction,
  narrowed,
}: {
  walk: Walk;
  title: string;
  direction: Direction;
  narrowed: boolean;
}) {
  const n = walk.total;
  const many = `${n.toLocaleString()} ${n === 1 ? "entity" : "entities"}`;
  const steps = `${String(walk.depth)} ${walk.depth === 1 ? "step" : "steps"}`;
  const notes: ReactNode[] = [];
  if (walk.hidden) {
    notes.push(
      `${walk.hidden.toLocaleString()} ${walk.hidden === 1 ? "trail leads" : "trails lead"} to entities you can’t read, so it stops there. Someone who can read them can follow them on.`,
    );
  }
  if (walk.beyond) {
    notes.push(
      walk.depth < MAX_DEPTH
        ? `There’s more past ${steps}: choose how far to go further.`
        : `It goes on past ${steps}. Open the furthest to follow it from there.`,
    );
  }
  return (
    <>
      <p role="status">
        {n === 0
          ? direction === "upstream"
            ? `Nothing you can read that ${title} was derived from${narrowed ? " matches" : ""}, within ${steps}.`
            : `Nothing you can read was derived from ${title}${narrowed ? " that matches" : ""}, within ${steps}.`
          : direction === "upstream"
            ? `${title} was derived from ${many} you can read, within ${steps}. Nearest first.`
            : `${many} you can read ${n === 1 ? "was" : "were"} derived from ${title}, within ${steps}. Nearest first.`}
      </p>
      {notes.length ? (
        <div className="alert alert-warning" role="note">
          {notes.map((note, i) => (
            <p key={i}>{note}</p>
          ))}
        </div>
      ) : null}
    </>
  );
}

function EntityName({ entity, names }: { entity: WalkEntity; names: Names }) {
  const meta = [
    entity.title === entity.hrid ? null : entity.hrid,
    names.type(entity.type),
    entity.project?.name ?? null,
  ].filter(Boolean);
  return (
    <>
      <span className="lineage-title">{entity.title}</span>{" "}
      <span className="lineage-meta">{meta.join(" · ")}</span>
      {entity.archived ? (
        <>
          {" "}
          <Badge muted>Archived</Badge>
        </>
      ) : null}
      {entity.purged ? (
        <>
          {" "}
          <Badge muted>Purged</Badge>
        </>
      ) : null}
    </>
  );
}

/** Why it's there: each step from the entity, in words. */
function Why({ item, root, direction }: { item: WalkItem; root: string; direction: Direction }) {
  return (
    <ol className="lineage-path">
      {item.path.map((step, i) => (
        <li key={`${String(i)}-${step.entity.id}`}>
          {stepWords(step, item.path[i - 1]?.entity.hrid ?? root, direction)}
        </li>
      ))}
    </ol>
  );
}

/** The impact list: everything, nearest first, with why. */
function List({
  items,
  root,
  direction,
  names,
}: {
  items: WalkItem[];
  root: string;
  direction: Direction;
  names: Names;
}) {
  return (
    <div className="table-scroll">
      <table className="table">
        <caption className="visually-hidden">
          {direction === "upstream" ? "What it was derived from" : "What was derived from it"},
          nearest first
        </caption>
        <thead>
          <tr>
            <th scope="col">Entity</th>
            <th scope="col">Steps away</th>
            <th scope="col">Why</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.entity.id}>
              <th scope="row">
                <Link to="/entities/$ref/" params={{ ref: item.entity.hrid }}>
                  <EntityName entity={item.entity} names={names} />
                </Link>
              </th>
              <td>{item.depth}</td>
              <td>
                <Why item={item} root={root} direction={direction} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface Shown {
  node: TreeNode;
  level: number;
  parent: string | null;
  setSize: number;
  position: number;
}

/**
 * The walk as a tree (the WAI-ARIA tree pattern): each entity under the one it was reached
 * from. Up and Down move, Right opens or goes in, Left closes or goes out, Home and End go
 * to the first and last, and Enter opens the entity.
 */
function Tree({
  items,
  root,
  direction,
  names,
  label,
}: {
  items: WalkItem[];
  root: string;
  direction: Direction;
  names: Names;
  label: string;
}) {
  const navigate = useNavigate();
  const roots = useMemo(() => walkTree(items), [items]);
  // The first two levels start open; the rest open as people go.
  const [open, setOpen] = useState<Set<string>>(
    () => new Set(roots.filter((n) => n.children.length).map((n) => n.item.entity.id)),
  );
  const [focused, setFocused] = useState<string | null>(null);
  const refs = useRef(new Map<string, HTMLAnchorElement>());

  const shown: Shown[] = [];
  const visit = (nodes: TreeNode[], level: number, parent: string | null) => {
    nodes.forEach((node, i) => {
      shown.push({ node, level, parent, setSize: nodes.length, position: i + 1 });
      if (open.has(node.item.entity.id)) visit(node.children, level + 1, node.item.entity.id);
    });
  };
  visit(roots, 1, null);
  const current = shown.find((s) => s.node.item.entity.id === focused) ?? shown[0];

  const move = (id: string | undefined) => {
    if (!id) return;
    setFocused(id);
    refs.current.get(id)?.focus();
  };
  const toggle = (id: string, to?: boolean) => {
    setOpen((was) => {
      const next = new Set(was);
      if (to ?? !next.has(id)) next.add(id);
      else next.delete(id);
      return next;
    });
  };
  const onKey = (event: ReactKeyboardEvent, s: Shown) => {
    const id = s.node.item.entity.id;
    const at = shown.indexOf(s);
    const has = s.node.children.length > 0;
    switch (event.key) {
      case "ArrowDown":
        move(shown[at + 1]?.node.item.entity.id);
        break;
      case "ArrowUp":
        move(shown[at - 1]?.node.item.entity.id);
        break;
      case "ArrowRight":
        if (has && !open.has(id)) toggle(id, true);
        else if (has) move(s.node.children[0]?.item.entity.id);
        break;
      case "ArrowLeft":
        if (has && open.has(id)) toggle(id, false);
        else move(s.parent ?? undefined);
        break;
      case "Home":
        move(shown[0]?.node.item.entity.id);
        break;
      case "End":
        move(shown.at(-1)?.node.item.entity.id);
        break;
      case "Enter":
        void navigate({ to: "/entities/$ref/", params: { ref: s.node.item.entity.hrid } });
        break;
      default:
        return;
    }
    event.preventDefault();
  };

  const render = (nodes: TreeNode[], level: number): ReactNode =>
    nodes.map((node) => {
      const id = node.item.entity.id;
      const s = shown.find((x) => x.node === node);
      if (!s) return null;
      const has = node.children.length > 0;
      const expanded = has ? open.has(id) : undefined;
      return (
        <li role="none" key={id}>
          <div className="lineage-tree-row">
            {has ? (
              // The keyboard opens and closes it on the item itself (Right and Left); this
              // is the pointer's way to.
              <span
                className="lineage-tree-toggle"
                aria-hidden="true"
                onClick={() => {
                  toggle(id);
                }}
              >
                {expanded ? "▾" : "▸"}
              </span>
            ) : (
              <span className="lineage-tree-toggle" aria-hidden="true" />
            )}
            <Link
              to="/entities/$ref/"
              params={{ ref: node.item.entity.hrid }}
              role="treeitem"
              aria-level={level}
              aria-setsize={s.setSize}
              aria-posinset={s.position}
              aria-expanded={expanded}
              tabIndex={current === s ? 0 : -1}
              ref={(el: HTMLAnchorElement | null) => {
                if (el) refs.current.set(id, el);
                else refs.current.delete(id);
              }}
              onFocus={() => {
                setFocused(id);
              }}
              onKeyDown={(event: ReactKeyboardEvent) => {
                onKey(event, s);
              }}
              className="lineage-tree-item"
            >
              <EntityName entity={node.item.entity} names={names} />
              {node.step ? (
                <span className="lineage-why">
                  {stepWords(node.step, node.item.path.at(-2)?.entity.hrid ?? root, direction)}
                </span>
              ) : null}
            </Link>
          </div>
          {has && expanded ? (
            <ul role="group" className="lineage-tree-group">
              {render(node.children, level + 1)}
            </ul>
          ) : null}
        </li>
      );
    });

  return (
    // The WAI-ARIA navigation tree: a list whose items are links, as treeitems.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-to-interactive-role -- the APG pattern
    <ul role="tree" aria-label={label} className="lineage-tree">
      {render(roots, 1)}
    </ul>
  );
}

// --- Graph -------------------------------------------------------------------------------------

function GraphView({
  entityRef,
  hrid,
  names,
  search,
}: {
  entityRef: string;
  hrid: string;
  names: Names;
  search: LineageSearch;
}) {
  const up = search.up ?? GRAPH_STEPS;
  const down = search.down ?? GRAPH_STEPS;
  const graph = useQuery(lineageGraphQuery(entityRef, up, down, search.edges ?? []));
  const data = graph.data;
  const further = (by: "up" | "down") => ({
    ...search,
    [by]: Math.min(MAX_GRAPH_STEPS, (by === "up" ? up : down) + GRAPH_STEPS),
  });
  return (
    <>
      {graph.error ? <ProblemAlert problem={problemOf(graph.error)} /> : null}
      {graph.isPending ? <Loading label="Loading the lineage…" /> : null}
      {data && !data.edges.length ? (
        <p className="empty">
          No lineage you can see: it wasn’t derived from anything you can read, and nothing you can
          read was derived from it.
        </p>
      ) : null}
      {data?.edges.length ? (
        <>
          <div className="toolbar">
            <div className="toolbar-end lineage-more">
              {data.earlier && up < MAX_GRAPH_STEPS ? (
                <Link
                  to={ROUTE}
                  params={{ ref: hrid }}
                  search={further("up")}
                  className={buttonClass()}
                >
                  Show earlier
                </Link>
              ) : null}
              {data.later && down < MAX_GRAPH_STEPS ? (
                <Link
                  to={ROUTE}
                  params={{ ref: hrid }}
                  search={further("down")}
                  className={buttonClass()}
                >
                  Show later
                </Link>
              ) : null}
            </div>
          </div>
          <Notes graph={data} up={up} down={down} />
          <Graph graph={data} names={names} />
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
      "There’s more than fits in one picture, so some is left out. The tree and the list have all of it.",
    );
  }
  if ((graph.earlier && up >= MAX_GRAPH_STEPS) || (graph.later && down >= MAX_GRAPH_STEPS)) {
    notes.push(
      `It goes further than ${String(MAX_GRAPH_STEPS)} steps. The tree and the list go further, or open the furthest entity to follow it on.`,
    );
  }
  if (graph.hidden) {
    notes.push(
      `${String(graph.hidden)} ${graph.hidden === 1 ? "trail leads" : "trails lead"} to entities you can’t read, so they aren’t drawn.`,
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
    entity.entry ? "Registry entry" : names.type(entity.type),
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
  const classes = `lineage-node lineage-entity ${entity.entry ? "is-entry" : ""} ${className ?? ""}`;
  return entity.id === names.root ? (
    <div className={`${classes} is-root`} {...point}>
      <EntityLabel entity={entity} names={names} />
    </div>
  ) : entity.entry ? (
    <Link to="/entries/$ref/" params={{ ref: entity.hrid }} className={classes} {...point}>
      <EntityLabel entity={entity} names={names} />
    </Link>
  ) : (
    <Link
      to={ROUTE}
      params={{ ref: entity.hrid }}
      search={{ view: "graph" }}
      className={classes}
      {...point}
    >
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

/** The graph: what something depends on left of it, with runs between what they read and
 * what they made. */
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
        {graph.runs.length} {graph.runs.length === 1 ? "run" : "runs"}, what each depends on to its
        left. Point at one to see where it came from and what came of it. The tree and the list say
        the same in order.
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
