/**
 * A project's records, as a grid: one type's fields as columns, or every
 * type's IDs. Rows are drawn only as they scroll into view, and more are
 * fetched before the end is reached, so a project of a million entities
 * scrolls like one of ten. The Entities tab shows all of them; the Requests
 * tab (ADR 0132) the same grid, held to its requests by fixed conditions.
 */

import { useInfiniteQuery, useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useId, useRef, useState } from "react";

import type { Project } from "../api";
import { usePlaceEntities } from "../component-entities";
import { componentsQuery, gridViews, type GridView, type PlaceSearch } from "../components";
import {
  entitiesQuery,
  gridSearch,
  inSentence,
  recordSearch,
  typeLabel,
  typesQuery,
  valueText,
  type Entity,
  type EntityType,
  type FieldDef,
} from "../entities";
import type { EntitySearch, Showing } from "../entities";
import {
  asksWhatRulesCaught,
  filterable,
  HRID,
  searchWords,
  sentToField,
  sortable,
  UPDATED,
  type Filter,
} from "../filters";
import { problemOf } from "../api";
import { usePageTitle } from "../layout";
import { sentToQuery } from "../requests";
import { buttonClass, Loading, ProblemAlert, RelativeTime, SelectField } from "../ui";
import { FilterPanel, SearchBox, SortHeading, type Direction } from "./entity-filters";
import { MAX_BATCH } from "../runs";
import { useProject } from "./project-overview";
import { CollectionBanner, SaveAsCollection } from "./collections";
import { PluginPlace } from "./plugin-place";
import { ReassignDialog } from "./reassign";
import { SavedSearches } from "./saved-searches";

const ROW_HEIGHT = 44;
/** The most records one reassignment changes, as the API takes. */
const MAX_REASSIGN = 500;
/** Fetch the next page when this many rows remain below the view. */
const AHEAD = 30;

interface Column {
  key: string;
  label: string;
  cell: (entity: Entity) => React.ReactNode;
  /** The field it sorts by, if it can. */
  sort?: string | undefined;
  /** Its heading, when it's more than the label. */
  header?: React.ReactNode;
  /** Its width, when it isn't a field's. */
  width?: string;
}

/** Entities picked in the grid, for the list they were picked in. */
interface Selection {
  list: string;
  hrids: ReadonlySet<string>;
}

/** A checkbox that can be part-checked, as "select all" is when some are. */
function Tick({
  label,
  checked,
  mixed = false,
  onChange,
}: {
  label: string;
  checked: boolean;
  mixed?: boolean;
  onChange: (checked: boolean) => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = mixed;
  }, [mixed]);
  return (
    <input
      ref={ref}
      type="checkbox"
      className="grid-tick"
      aria-label={label}
      checked={checked}
      onChange={(event) => {
        onChange(event.currentTarget.checked);
      }}
    />
  );
}

/** Which of a request's sides (ADR 0132) a grid shows as columns. */
export interface SideColumns {
  /** Show them even without a type: a grid of requests of any type. */
  always?: boolean;
  /** Leave out one: "asked_by" or "sent_to", which a grid's fixed conditions already say. */
  omit?: string;
}

function columnsFor(
  type: EntityType | undefined,
  types: EntityType[],
  shown: SideColumns = {},
): Column[] {
  const id: Column = {
    key: "hrid",
    label: "ID",
    sort: HRID.name,
    cell: (e) => (
      // A registry entry's record opens as its project's: other projects have its HRID too.
      <Link to="/entities/$ref/" params={{ ref: e.hrid }} search={recordSearch(e)}>
        {e.hrid}
      </Link>
    ),
  };
  const kind: Column = {
    key: "type",
    label: "Type",
    cell: (e) => typeLabel(types, e.type),
  };
  // A request between projects (ADR 0132): who asked, and the service it went to.
  const sides: Column[] = (
    type?.request || shown.always
      ? [
          {
            key: "asked_by",
            label: "Asked by",
            cell: (e: Entity) => e.request?.asking_project?.name ?? "",
          },
          {
            key: "sent_to",
            label: "Sent to",
            cell: (e: Entity) =>
              e.request?.service?.name ?? <span className="muted">Not sent yet</span>,
          },
        ]
      : []
  ).filter((c) => c.key !== shown.omit);
  const updated: Column = {
    key: "updated",
    label: "Last changed",
    cell: (e) => <RelativeTime value={e.updated_at} />,
    sort: UPDATED.name,
  };
  if (!type) {
    const name: Column = {
      key: "title",
      label: "Title",
      cell: (e) => (e.title === e.hrid ? "" : e.title),
    };
    return [id, kind, name, ...sides, updated];
  }
  const fields: Column[] = type.fields.map((f: FieldDef) => ({
    key: `field:${f.name}`,
    label: f.type === "quantity" ? `${f.label} (${unitOf(f)})` : f.label,
    sort: sortable(f) ? f.name : undefined,
    cell: (e) => {
      const value = e.values[f.name];
      if (f.type === "quantity" && typeof value === "number") {
        // The unit is in the column's heading.
        return valueText(f, value, e).replace(/\s\S+$/, "");
      }
      if (f.type === "state") {
        // A state that locks the record says so (ADR 0125).
        return (
          <>
            {valueText(f, value, e)}
            {e.locked_by.includes(f.name) ? <span className="cell-meta">Locked</span> : null}
          </>
        );
      }
      // Who's named and can't be any more (ADR 0122) says so beside them.
      if (f.assignment && (e.needs_reassigning ?? []).some((s) => s.field === f.name)) {
        return (
          <>
            {valueText(f, value, e)}
            <span className="cell-meta">Needs reassigning</span>
          </>
        );
      }
      return valueText(f, value, e);
    },
  }));
  // A record's state fields (ADR 0125), beside its ID: the first thing a lab asks of it.
  const states = fields.filter((_, index) => type.fields[index]?.type === "state");
  const rest = fields.filter((_, index) => type.fields[index]?.type !== "state");
  return [id, ...(type.subtypes.length ? [kind] : []), ...sides, ...states, ...rest, updated];
}

/** Why a record is in a search of what rules caught (ADR 0127): the rules holding it for
 * review, and those it breaks, each with what it was told. */
const CAUGHT: Column = {
  key: "caught",
  label: "Rules",
  width: "minmax(16rem, 2fr)",
  cell: (e) => {
    const holds = e.holds ?? [];
    const breaches = e.breaches ?? [];
    const why = [
      ...holds.map((h) => `Held for review: ${h.label}`),
      ...breaches.map((b) => `Breaks: ${b.label}`),
    ].join("; ");
    const told = holds[0]?.message ?? breaches[0]?.message;
    return why ? (
      <>
        {why}
        {told ? <span className="cell-meta">{told}</span> : null}
      </>
    ) : (
      "None"
    );
  },
};

function unitOf(field: FieldDef): string {
  if (field.display_scale != null && field.display_unit) {
    return field.display_unit_symbol ?? field.display_unit;
  }
  return field.unit_symbol ?? field.unit ?? "";
}

/** The project's entities: every type, its collections and saved searches. */
export function ProjectEntities() {
  const project = useProject();
  const search = useSearch({ from: "/projects/$slug/entities" });
  const navigate = useNavigate({ from: "/projects/$slug/entities" });
  const types = useSuspenseQuery(typesQuery).data;
  const type = types.find((t) => t.name === search.type);
  usePageTitle(`${type ? type.label_plural : "Entities"} · ${project.name}`);
  // Requests (ADR 0132) are found by the service they were sent to: this project, as a
  // service, or one it sent requests to.
  const sentTo = useQuery({ ...sentToQuery(project.id), enabled: project.can.read }).data ?? [];
  const requests = type ? Boolean(type.request) : types.some((t) => t.request);
  return (
    <EntityGrid
      project={project}
      types={types}
      search={search}
      change={(update, replace) => {
        void navigate({ search: update, ...(replace ? { replace } : {}) });
      }}
      extra={requests ? [sentToField([project, ...sentTo.filter((p) => p.id !== project.id)])] : []}
      library
    />
  );
}

/** What a grid is of, besides the project: its fixed conditions and the types it offers. */
export interface GridScope {
  /** Conditions every search adds, which its filters don't show: a service's queue, say. */
  fixed?: Filter[];
  /** The types it offers, each with its subtypes; all of them if not given. */
  choices?: EntityType[];
  /** What it calls its records without a type: "entity" and "entities" if not given. */
  noun?: { one: string; many: string };
  /** Built-in fields it filters by besides the type's: the service a request went to. */
  extra?: FieldDef[];
  /** A request's sides (ADR 0132), as columns. */
  sides?: SideColumns;
  /** What it says with nothing to show: `narrowed` when a search or filter found none. */
  empty?: (narrowed: boolean) => string;
  /** The project's library: its collections, saved searches and new entities. */
  library?: boolean;
  /** Offer making a new record of its type. */
  making?: boolean;
}

/**
 * A grid of a project's records, from the search in its page's address (`search`), which
 * it changes with `change`. The Entities tab shows everything; the Requests tab (ADR 0132)
 * a service's queue, or the requests a project sent, each through `fixed` conditions.
 */
export function EntityGrid({
  project,
  types,
  search,
  change,
  fixed = [],
  choices = types,
  noun: plural = { one: "entity", many: "entities" },
  extra = [],
  sides,
  empty,
  library = false,
  making = library,
}: GridScope & {
  project: Project;
  types: EntityType[];
  search: EntitySearch;
  change: (update: (prev: EntitySearch) => EntitySearch, replace?: boolean) => void;
}) {
  const navigate = ({
    search: update,
    replace,
  }: {
    search: (prev: EntitySearch) => EntitySearch;
    replace?: boolean;
  }) => {
    change(update, replace);
  };
  // With one type to offer, it's that type's grid.
  const only = choices.length === 1 ? choices[0] : undefined;
  const offered = new Set(choices.flatMap((t) => [t.name, ...t.subtypes]));
  const type = types.find((t) => t.name === search.type && offered.has(t.name)) ?? only;
  const show = search.show ?? "current";

  const fields = filterable(type, extra);
  const filters = search.filters ?? [];
  const sort = fields.some((f) => f.name === search.sort && sortable(f)) ? search.sort : undefined;
  const narrowed = Boolean(search.q) || filters.length > 0;
  const [filtersOpen, setFiltersOpen] = useState(filters.length > 0);
  const panelId = useId();
  const filter = {
    project: project.id,
    type: type?.name,
    archived: show === "all" ? undefined : show === "archived",
    q: search.q,
    filters: [...fixed, ...filters],
    sort,
    descending: sort ? search.desc : undefined,
    collection: library ? search.collection : undefined,
  };
  // Plugins' components that show this type's results another way (ADR 0117).
  const components = useQuery(componentsQuery).data ?? [];
  const views = gridViews(type?.name, types, components);
  const shownAs = views.find((v) => v.component.name === search.view);
  const list = useInfiniteQuery({ ...entitiesQuery(filter), enabled: !shownAs });
  const applyFilters = (next: Filter[]) => {
    navigate({
      search: (prev) => ({ ...prev, filters: next.length ? next : undefined }),
      replace: true,
    });
  };
  const sortBy = (field: string, direction: Direction) => {
    navigate({
      search: (prev) => ({
        ...prev,
        sort: direction === "none" ? undefined : field,
        desc: direction === "descending" ? true : undefined,
      }),
    });
  };
  const rows = list.data?.pages.flatMap((p) => p.items) ?? [];

  // Picking entities to run an action on. A different list starts a new selection.
  const listKey = JSON.stringify([
    type?.name,
    show,
    search.q,
    filters,
    sort,
    search.desc,
    search.collection,
  ]);
  const [selection, setSelection] = useState<Selection>({ list: listKey, hrids: new Set() });
  const picked = selection.list === listKey ? selection.hrids : new Set<string>();
  const pick = (hrids: string[], on: boolean) => {
    const next = new Set(picked);
    for (const hrid of hrids) {
      if (on) next.add(hrid);
      else next.delete(hrid);
    }
    setSelection({ list: listKey, hrids: next });
  };
  // Picked to run an action on, or to reassign (ADR 0122).
  const reassignable = Boolean(project.can.edit && type?.fields.some((f) => f.assignment));
  const selectable = (project.can.run.length > 0 || reassignable) && !project.archived;
  const [reassigning, setReassigning] = useState(false);
  const allPicked = rows.length > 0 && rows.every((e) => picked.has(e.hrid));
  const select: Column = {
    key: "select",
    label: "Select",
    width: "2.75rem",
    header: (
      <Tick
        label={`Select all ${String(rows.length)} shown`}
        checked={allPicked}
        mixed={!allPicked && picked.size > 0}
        onChange={(on) => {
          pick(
            rows.map((e) => e.hrid),
            on,
          );
        }}
      />
    ),
    cell: (e) => (
      <Tick
        label={`Select ${e.hrid}`}
        checked={picked.has(e.hrid)}
        onChange={(on) => {
          pick([e.hrid], on);
        }}
      />
    ),
  };
  // A search of what rules caught says, by each record, which caught it and why.
  const caught = asksWhatRulesCaught([...filters, ...searchWords(search.q ?? "").conditions]);
  const [id, ...others] = columnsFor(type, types, sides);
  const columns = [
    ...(selectable ? [select] : []),
    ...(id ? [id] : []),
    ...(caught ? [CAUGHT] : []),
    ...others,
  ];

  const scroller = useRef<HTMLDivElement>(null);
  // TanStack Virtual hands back functions that read its live state, so the React Compiler
  // leaves this component unmemoized; it re-renders as the grid scrolls, as it must.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 10,
  });
  const visible = virtualizer.getVirtualItems();
  const last = visible.at(-1)?.index ?? 0;
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = list;
  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage && last >= rows.length - AHEAD) {
      void fetchNextPage();
    }
  }, [last, rows.length, hasNextPage, isFetchingNextPage, fetchNextPage]);

  const captionId = useId();
  const template = columns.map((c) => c.width ?? "minmax(9rem, 1fr)").join(" ");
  const minWidth = `${String(columns.filter((c) => !c.width).length * 9 + (selectable ? 2.75 : 0) + (caught ? 16 : 0))}rem`;
  // The types it offers and their subtypes, an abstract one as any of its kinds.
  const typeOptions = [
    { value: "", label: `All ${plural.many}` },
    ...types
      .filter((t) => offered.has(t.name) && (!t.abstract || t.subtypes.length))
      .map((t) => ({ value: t.name, label: t.abstract ? `${t.label} (any kind)` : t.label }))
      .sort((a, b) => a.label.localeCompare(b.label)),
  ];
  const count = list.hasNextPage ? `${String(rows.length)}+` : String(rows.length);
  const one = rows.length === 1 && !list.hasNextPage;
  const noun = type
    ? inSentence(one ? type.label : type.label_plural)
    : one
      ? plural.one
      : plural.many;
  const makeable = type && !type.abstract && !type.detached ? type : undefined;

  return (
    <>
      <div className="toolbar">
        {only ? null : (
          <SelectField
            label="Type"
            value={type?.name ?? ""}
            options={typeOptions}
            onChange={(value) => {
              // Other types have other fields: their filters and sort don't carry over.
              navigate({
                search: (prev) => ({
                  type: value || undefined,
                  show: prev.show,
                  q: prev.q,
                  collection: prev.collection,
                }),
              });
            }}
          />
        )}
        <SelectField<Showing>
          label="Show"
          value={show}
          options={[
            { value: "current", label: "Current" },
            { value: "archived", label: "Archived" },
            { value: "all", label: "Current and archived" },
          ]}
          onChange={(value) => {
            navigate({
              search: (prev) => ({ ...prev, show: value === "current" ? undefined : value }),
            });
          }}
        />
        <SearchBox
          key={search.q ?? ""}
          value={search.q ?? ""}
          placeholder={
            type?.fields.some((f) => f.assignment)
              ? `ID, name, or assigned:me`
              : type?.rules?.some((r) => r.hold)
                ? `ID, name, or is:held`
                : type
                  ? `ID or ${inSentence(type.label)} name`
                  : "ID or name"
          }
          onSearch={(q) => {
            navigate({ search: (prev) => ({ ...prev, q: q || undefined }), replace: true });
          }}
        />
        {views.length ? (
          <SelectField
            label="Show as"
            value={shownAs?.component.name ?? ""}
            options={[
              { value: "", label: "Grid" },
              ...views.map((v) => ({ value: v.component.name, label: v.label })),
            ]}
            onChange={(value) => {
              navigate({ search: (prev) => ({ ...prev, view: value || undefined }) });
            }}
          />
        ) : null}
        <button
          type="button"
          className={buttonClass()}
          aria-expanded={filtersOpen}
          aria-controls={panelId}
          onClick={() => {
            setFiltersOpen((open) => !open);
          }}
        >
          Filters{filters.length ? ` (${String(filters.length)})` : ""}
        </button>
        {type ? (
          <p className="toolbar-note">
            <Link to="/types/$/" params={{ _splat: type.name }}>
              About {type.label_plural}
            </Link>
          </p>
        ) : null}
        {library && project.can.edit && !project.archived && !search.collection ? (
          <SaveAsCollection project={project} search={search} />
        ) : null}
        {making && project.can.edit && !project.archived && (library || makeable) ? (
          <Link
            to="/projects/$slug/entities/new/"
            params={{ slug: project.slug }}
            search={makeable ? { type: makeable.name } : {}}
            className={`${buttonClass("primary")} toolbar-end`}
          >
            New {makeable ? inSentence(makeable.label) : "entity"}
          </Link>
        ) : null}
      </div>
      {!library ? null : search.collection ? (
        <CollectionBanner reference={search.collection} project={project} />
      ) : (
        <SavedSearches project={project} search={search} />
      )}
      {filtersOpen ? (
        <FilterPanel
          key={type?.name ?? ""}
          id={panelId}
          type={type}
          types={types}
          fields={fields}
          applied={filters}
          projectId={project.id}
          onApply={applyFilters}
        />
      ) : null}
      {shownAs ? (
        <ShownAs
          key={shownAs.component.name}
          view={shownAs}
          search={gridSearch(filter) as PlaceSearch}
          project={project}
          types={types}
        />
      ) : null}
      {!shownAs && list.isError ? <ProblemAlert problem={problemOf(list.error)} /> : null}
      {!shownAs && list.isPending ? <Loading label="Loading entities…" /> : null}
      {!shownAs && list.isSuccess && rows.length === 0 ? (
        <p className="empty">
          {empty
            ? empty(narrowed || show !== "current")
            : filter.collection
              ? `No members you can read${narrowed || type ? " match" : ""}.`
              : narrowed
                ? `No ${type ? inSentence(type.label_plural) : "entities"} in this project match.`
                : type
                  ? `No ${inSentence(type.label_plural)} in this project${show === "current" ? "" : " match"}.`
                  : "No entities in this project yet."}
        </p>
      ) : null}
      {!shownAs && rows.length ? (
        <>
          {/* The count stays in the page, so screen readers hear it change as rows are picked. */}
          {selectable ? (
            <div className={picked.size ? "selection-bar" : undefined}>
              <p role="status" className={picked.size ? undefined : "visually-hidden"}>
                {picked.size ? `${String(picked.size)} selected` : ""}
                {picked.size > MAX_BATCH
                  ? `: an action runs on at most ${String(MAX_BATCH)} at once`
                  : ""}
              </p>
              {picked.size && reassignable ? (
                <button
                  type="button"
                  className={buttonClass()}
                  onClick={() => {
                    setReassigning(true);
                  }}
                >
                  Reassign
                </button>
              ) : null}
              {picked.size && picked.size <= MAX_BATCH && project.can.run.length ? (
                <Link
                  to="/projects/$slug/runs/new/"
                  params={{ slug: project.slug }}
                  search={{ entities: [...picked].join(",") }}
                  className={buttonClass("primary")}
                >
                  Run action
                </Link>
              ) : null}
              {picked.size ? (
                <button
                  type="button"
                  className={buttonClass("quiet")}
                  onClick={() => {
                    setSelection({ list: listKey, hrids: new Set() });
                  }}
                >
                  Clear selection
                </button>
              ) : null}
            </div>
          ) : null}
          {reassigning && type ? (
            <ReassignDialog
              type={type}
              projectId={project.id}
              picked={rows.filter((e) => picked.has(e.hrid)).slice(0, MAX_REASSIGN)}
              onClose={(done) => {
                setReassigning(false);
                if (done) setSelection({ list: listKey, hrids: new Set() });
              }}
            />
          ) : null}
          <p className="muted" id={captionId}>
            {count} {noun}
            {show === "archived" ? ", archived" : ""}
            {narrowed ? " match" : ""}
          </p>
          {/* A scrolling region people reach with Tab, so the keyboard scrolls it. */}
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable region */}
          <div className="data-grid" ref={scroller} tabIndex={0} aria-labelledby={captionId}>
            <div
              role="table"
              aria-labelledby={captionId}
              aria-busy={list.isPlaceholderData || undefined}
              aria-rowcount={list.hasNextPage ? -1 : rows.length + 1}
            >
              <div role="rowgroup" className="data-grid-head">
                <div
                  role="row"
                  aria-rowindex={1}
                  className="data-grid-row"
                  style={{ gridTemplateColumns: template, minWidth }}
                >
                  {columns.map((c) => {
                    const sorted = c.sort !== undefined && c.sort === sort;
                    const direction: Direction = sorted
                      ? search.desc
                        ? "descending"
                        : "ascending"
                      : "none";
                    return (
                      <div
                        role="columnheader"
                        key={c.key}
                        className="data-grid-cell"
                        aria-sort={c.sort ? direction : undefined}
                      >
                        {c.header ??
                          (c.sort ? (
                            <SortHeading
                              label={c.label}
                              direction={direction}
                              onSort={(next) => {
                                if (c.sort) sortBy(c.sort, next);
                              }}
                            />
                          ) : (
                            c.label
                          ))}
                      </div>
                    );
                  })}
                </div>
              </div>
              <div
                role="rowgroup"
                className="data-grid-body"
                style={{ height: `${String(virtualizer.getTotalSize())}px`, minWidth }}
              >
                {visible.map((item) => {
                  const entity = rows[item.index];
                  if (!entity) return null;
                  return (
                    <div
                      role="row"
                      key={entity.id}
                      aria-rowindex={item.index + 2}
                      className="data-grid-row"
                      style={{
                        gridTemplateColumns: template,
                        minWidth,
                        transform: `translateY(${String(item.start)}px)`,
                      }}
                    >
                      {columns.map((c) => (
                        <div role="cell" key={c.key} className="data-grid-cell">
                          {c.cell(entity)}
                        </div>
                      ))}
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
          {list.isFetchingNextPage ? <Loading label="Loading more…" /> : null}
        </>
      ) : null}
    </>
  );
}

/** The grid's results in a plugin's component, given the same search as the grid. */
function ShownAs({
  view,
  search,
  project,
  types,
}: {
  view: GridView;
  search: PlaceSearch;
  project: Project;
  types: EntityType[];
}) {
  const data = usePlaceEntities(view.component, { search });
  return (
    <PluginPlace
      component={view.component}
      label={view.label}
      place={{ slot: "search.results", label: view.label }}
      data={data}
      project={project}
      types={types}
    />
  );
}
