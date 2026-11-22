/**
 * The one table of records (ADR 0176): every record its reader can read, across their
 * projects, or a project's alone. The All entities page, a type's table, the Requests
 * list and a project's Entities and Requests tabs are each this grid, held by fixed
 * conditions, a type or a project.
 *
 * Rows are drawn only as they scroll into view, and more are fetched before the end is
 * reached, so a million records scroll like ten. A record in several projects shows each
 * one the reader can read; one only in a project they aren't in never appears.
 */

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import {
  Button as AriaButton,
  Dialog,
  DialogTrigger,
  Menu,
  MenuItem,
  MenuTrigger,
  Popover,
} from "react-aria-components";

import { problemOf, type Project } from "../api";
import { usePlaceEntities } from "../component-entities";
import { componentsQuery, gridViews, type GridView, type PlaceSearch } from "../components";
import {
  entitiesQuery,
  gridSearch,
  inSentence,
  recordSearch,
  searchCsvUrl,
  stateFields,
  typeLabel,
  valueText,
  type Entity,
  type EntityType,
  type FieldDef,
  type Showing,
} from "../entities";
import {
  asksWhatRulesCaught,
  CREATED,
  filterable,
  HRID,
  searchWords,
  sortable,
  UPDATED,
  type Filter,
} from "../filters";
import {
  countsQuery,
  hiddenColumns,
  keepHiddenColumns,
  useReadableProjects,
  type ListSearch,
} from "../lists";
import { MAX_BATCH } from "../runs";
import { TypeDot, TypeName } from "../type-color";
import { buttonClass, Loading, ProblemAlert, RelativeTime, SelectField } from "../ui";
import { CollectionBanner, SaveAsCollection } from "./collections";
import { FilterBar, SearchBox, SortHeading, type Direction } from "./entity-filters";
import { PluginPlace } from "./plugin-place";
import { ReassignDialog } from "./reassign";
import { SavedSearches } from "./saved-searches";

/** A row's height, as the stylesheet draws it (`.data-grid-row`). */
const ROW_HEIGHT = 34;
/** The most records one reassignment changes, as the API takes. */
const MAX_REASSIGN = 500;
/** Fetch the next page when this many rows remain below the view. */
const AHEAD = 30;

interface Column {
  key: string;
  label: string;
  cell: (entity: Entity) => ReactNode;
  /** The field it sorts by, if it can. */
  sort?: string | undefined;
  /** Its heading, when it's more than the label. */
  header?: ReactNode;
  /** Its width, when it isn't a field's. */
  width?: string;
  /** Hidden until someone shows it. */
  hidden?: boolean;
  /** Its column in the grid's download (ADR 0175), if it has one. */
  csv?: string;
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

/** The projects a record is in that its reader can read: the first, and how many more. */
function ProjectsCell({ entity }: { entity: Entity }) {
  const [first, ...more] = entity.projects ?? [];
  if (!first) return <span className="muted">—</span>;
  const others = more.map((p) => p.name).join(", ");
  return (
    <>
      {first.name}
      {more.length ? (
        <span className="cell-more" title={`Also in ${others}`}>
          <span aria-hidden="true">+{more.length}</span>
          <span className="visually-hidden">, also in {others}</span>
        </span>
      ) : null}
    </>
  );
}

/** A record's state (ADR 0125), as a row of any type shows it: its type's first state. */
function StatusCell({ entity, types }: { entity: Entity; types: EntityType[] }) {
  const field = stateFields(types.find((t) => t.name === entity.type))[0];
  const value = field ? entity.values[field.name] : undefined;
  return (
    <>
      {field && value !== null && value !== undefined ? (
        <span className="state-pill">{valueText(field, value, entity)}</span>
      ) : null}
      {entity.locked ? <span className="state-pill state-pill-muted">Locked</span> : null}
    </>
  );
}

function columnsFor(
  type: EntityType | undefined,
  types: EntityType[],
  shown: SideColumns = {},
  across: boolean,
): Column[] {
  const id: Column = {
    key: "hrid",
    label: "ID",
    sort: HRID.name,
    csv: "hrid",
    cell: (e) => (
      // A registry entry's record opens as its project's: other projects have its HRID too.
      <Link
        className="grid-id"
        to="/entities/$ref/"
        params={{ ref: e.hrid }}
        search={recordSearch(e)}
      >
        {e.hrid}
      </Link>
    ),
  };
  const kind: Column = {
    key: "type",
    label: "Type",
    csv: "type",
    cell: (e) => <TypeName type={e.type} label={typeLabel(types, e.type)} />,
  };
  const projects: Column = {
    key: "project",
    label: "Project",
    csv: "project",
    // A project's own tab lists its records: their other projects are there to show.
    hidden: !across,
    cell: (e) => <ProjectsCell entity={e} />,
  };
  // A request between projects (ADR 0132): who asked, and the service it went to.
  const sides: Column[] = (
    type?.request || shown.always
      ? [
          {
            key: "asked_by",
            label: "Asked by",
            csv: "asked_by",
            cell: (e: Entity) => e.request?.asking_project?.name ?? "",
          },
          {
            key: "sent_to",
            label: "Sent to",
            csv: "sent_to",
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
    csv: "updated_at",
  };
  if (!type) {
    const name: Column = {
      key: "title",
      label: "Name",
      csv: "title",
      cell: (e) => (e.title === e.hrid ? "" : e.title),
    };
    const status: Column = {
      key: "status",
      label: "Status",
      cell: (e) => <StatusCell entity={e} types={types} />,
    };
    const made: Column = {
      key: "created_by",
      label: "Made by",
      cell: (e) => e.created_by?.name ?? "",
    };
    return [id, name, kind, projects, ...sides, status, made, updated];
  }
  const fields: Column[] = type.fields.map((f: FieldDef) => ({
    key: `field:${f.name}`,
    label: f.type === "quantity" ? `${f.label} (${unitOf(f)})` : f.label,
    sort: sortable(f) ? f.name : undefined,
    csv: f.name,
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
  return [
    id,
    ...(type.subtypes.length ? [kind] : []),
    ...sides,
    ...states,
    ...rest,
    projects,
    updated,
  ];
}

/** Why a record is in a search of what rules caught (ADR 0127): the rules holding it for
 * review, and those it breaks, each with what it was told. */
const CAUGHT: Column = {
  key: "caught",
  label: "Rules",
  width: "minmax(calc(256rem / 13), 2fr)",
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

/** What a grid is of, besides its project: its fixed conditions and the types it offers. */
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
  /** Its types as pills, each with how many records of it its reader can read. */
  pills?: boolean;
}

/** How the sort menu says a direction of a field: newest first, A to Z. */
function directionWords(field: FieldDef, descending: boolean): string {
  if (field.type === "date" || field.type === "datetime") {
    return descending ? "newest first" : "oldest first";
  }
  if (["int", "float", "quantity"].includes(field.type)) {
    return descending ? "highest first" : "lowest first";
  }
  if (field.type === "bool") return descending ? "yes first" : "no first";
  return descending ? "Z to A" : "A to Z";
}

/**
 * A grid of records, from the search in its page's address (`search`), which it changes
 * with `change`. With a `project`, it's that project's; without one, every project its
 * reader can read, with a Project filter (`?project=`, a slug) and a Project column.
 */
export function EntityGrid({
  project: fixedProject,
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
  pills = false,
}: GridScope & {
  project?: Project | undefined;
  types: EntityType[];
  search: ListSearch;
  change: (update: (prev: ListSearch) => ListSearch, replace?: boolean) => void;
}) {
  const navigate = ({
    search: update,
    replace,
  }: {
    search: (prev: ListSearch) => ListSearch;
    replace?: boolean;
  }) => {
    change(update, replace);
  };
  const across = !fixedProject;
  // Across projects, the project the reader picked, if any: it's then held to it.
  const { projects: readable, ready: projectsLoaded } = useReadableProjects();
  const chosen = across ? readable.find((p) => p.slug === search.project) : undefined;
  const project = fixedProject ?? chosen;
  // With one type to offer, it's that type's grid.
  const only = choices.length === 1 ? choices[0] : undefined;
  const offered = new Set(choices.flatMap((t) => [t.name, ...t.subtypes]));
  const type = types.find((t) => t.name === search.type && offered.has(t.name)) ?? only;
  const show = search.show ?? "current";

  const fields = filterable(type, extra);
  const filters = search.filters ?? [];
  const sortFields = [CREATED, ...fields.filter((f) => sortable(f) && f.name !== CREATED.name)];
  const sort = fields.some((f) => f.name === search.sort && sortable(f)) ? search.sort : undefined;
  const narrowed = Boolean(search.q) || filters.length > 0;
  const filter = {
    project: project?.id,
    type: type?.name,
    archived: show === "all" ? undefined : show === "archived",
    q: search.q,
    filters: [...fixed, ...filters],
    sort,
    descending: sort ? search.desc : undefined,
    collection: library ? search.collection : undefined,
  };
  // A project named in the address that isn't one the reader can read finds nothing, as
  // the API would answer for it: it isn't searched across every project instead.
  const unknownProject = across && Boolean(search.project) && !chosen;
  // Plugins' components that show this type's results another way (ADR 0117), in a project.
  const components = useQuery(componentsQuery).data ?? [];
  const views = project ? gridViews(type?.name, types, components) : [];
  const shownAs = views.find((v) => v.component.name === search.view);
  const list = useInfiniteQuery({
    ...entitiesQuery(filter),
    enabled: !shownAs && !unknownProject,
  });
  const counts = useQuery({ ...countsQuery(project?.id), enabled: pills && !unknownProject });
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
  const pickType = (value: string) => {
    // Other types have other fields: their filters and sort don't carry over.
    navigate({
      search: (prev) => ({
        type: value || undefined,
        show: prev.show,
        q: prev.q,
        collection: prev.collection,
        project: prev.project,
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
    project?.id,
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
  const pickedRows = rows.filter((e) => picked.has(e.hrid));
  // What's picked runs, or is reassigned (ADR 0122), in a project: the grid's, or across
  // projects, one every picked record is in.
  const shared = across
    ? readable.filter((p) => pickedRows.every((e) => (e.projects ?? []).some((q) => q.id === p.id)))
    : [];
  const runIn = project?.can.run.length
    ? project
    : across && !project
      ? shared.find((p) => p.can.run.length && !p.archived)
      : undefined;
  const owners = new Set(pickedRows.map((e) => e.owner_project?.id));
  const owner = owners.size === 1 ? readable.find((p) => owners.has(p.id)) : undefined;
  const editIn = project ?? owner;
  const assignable = Boolean(type?.fields.some((f) => f.assignment));
  const reassignable = Boolean(assignable && editIn?.can.edit && !editIn.archived);
  const actsIn = (p: Project) =>
    (p.can.run.length > 0 || (assignable && p.can.edit)) && !p.archived;
  const selectable = across ? readable.some(actsIn) : Boolean(project && actsIn(project));
  const [reassigning, setReassigning] = useState(false);
  const allPicked = rows.length > 0 && rows.every((e) => picked.has(e.hrid));
  const select: Column = {
    key: "select",
    label: "Select",
    width: "calc(40rem / 13)",
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
  const [id, ...others] = columnsFor(type, types, sides, across);
  const columnsList = `${across ? "all" : "project"}.${type?.name ?? "*"}`;
  const [hidden, setHidden] = useState<{ list: string; keys: Set<string> | null }>(() => ({
    list: columnsList,
    keys: null,
  }));
  const stored = hidden.list === columnsList ? hidden.keys : null;
  const hiddenKeys = stored ?? keptOr(columnsList, others);
  const shownOthers = others.filter((c) => !hiddenKeys.has(c.key));
  const columns = [
    ...(selectable ? [select] : []),
    ...(id ? [id] : []),
    ...(caught ? [CAUGHT] : []),
    ...shownOthers,
  ];
  const csvColumns = [
    ...new Set([...columns.flatMap((c) => (c.csv ? [c.csv] : [])), "updated_at", "revision", "id"]),
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
  // In pixels at the stylesheet's 13px rem (ADR 0176), so they scale with the reader's size.
  const template = columns.map((c) => c.width ?? "minmax(calc(128rem / 13), 1fr)").join(" ");
  const minWidth = `calc(${String(columns.filter((c) => !c.width).length * 128 + (selectable ? 40 : 0) + (caught ? 256 : 0))}rem / 13)`;
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
  const many = type ? inSentence(type.label_plural) : plural.many;
  const makeable = type && !type.abstract && !type.detached ? type : undefined;
  const sortedBy = sortFields.find((f) => f.name === (sort ?? CREATED.name)) ?? CREATED;
  const sortKey = `${sortedBy.name}:${sort && search.desc ? "desc" : "asc"}`;
  // A project's own tab says "this project"; across projects, the one picked, by name.
  const where = fixedProject ? "in this project" : project ? `in ${project.name}` : "you can read";

  return (
    <>
      <div className="list-toolbar">
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
                  : across
                    ? "Search your projects by ID or name"
                    : "ID or name"
          }
          onSearch={(q) => {
            navigate({ search: (prev) => ({ ...prev, q: q || undefined }), replace: true });
          }}
        />
        {across ? (
          <SelectField
            label="Project"
            compact
            value={chosen?.slug ?? search.project ?? ""}
            options={[
              { value: "", label: "Any project" },
              ...(search.project && !chosen
                ? [{ value: search.project, label: search.project }]
                : []),
              ...readable.map((p) => ({ value: p.slug, label: p.name })),
            ]}
            onChange={(slug) => {
              navigate({ search: (prev) => ({ ...prev, project: slug || undefined }) });
            }}
          />
        ) : null}
        {only || pills ? null : (
          <SelectField
            label="Type"
            compact
            value={type?.name ?? ""}
            options={typeOptions}
            onChange={pickType}
          />
        )}
        <SelectField<Showing>
          label="Show"
          compact
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
        <MenuTrigger>
          <AriaButton
            className={buttonClass()}
            aria-label={`Sort: ${sortLabel(sortedBy, sortKey)}`}
          >
            <span aria-hidden="true">⇅ </span>
            {sortLabel(sortedBy, sortKey)}
          </AriaButton>
          <Popover className="menu-popover" placement="bottom start" offset={4}>
            <Menu
              className="menu"
              aria-label="Sort by"
              selectionMode="single"
              selectedKeys={[sortKey]}
              onAction={(key) => {
                const [name = "", direction] = String(key).split(":");
                // Oldest first is how a search reads without a sort: by when it was made.
                if (name === CREATED.name && direction === "asc") {
                  sortBy(name, "none");
                } else {
                  sortBy(name, direction === "desc" ? "descending" : "ascending");
                }
              }}
            >
              {sortFields.flatMap((f) =>
                [false, true].map((descending) => {
                  const key = `${f.name}:${descending ? "desc" : "asc"}`;
                  return (
                    <MenuItem key={key} id={key} className="menu-item">
                      {f.label}, {directionWords(f, descending)}
                    </MenuItem>
                  );
                }),
              )}
            </Menu>
          </Popover>
        </MenuTrigger>
        {views.length ? (
          <SelectField
            label="Show as"
            compact
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
        <div className="list-toolbar-end" hidden={unknownProject}>
          {!shownAs && list.isSuccess && rows.length ? (
            <p className="muted list-count" id={captionId}>
              {count} {noun}
              {show === "archived" ? ", archived" : ""}
              {narrowed ? " match" : ""}
            </p>
          ) : null}
          <ColumnsMenu
            columns={others}
            hidden={hiddenKeys}
            onChange={(keys) => {
              keepHiddenColumns(columnsList, keys);
              setHidden({ list: columnsList, keys });
            }}
          />
          {/* Every one the search finds, not only those loaded: written by the server as
              it reads them, and in the audit log (ADR 0175). */}
          <a
            className={buttonClass()}
            href={searchCsvUrl(filter, csvColumns)}
            download
            aria-describedby={rows.length ? captionId : undefined}
          >
            Download CSV
          </a>
          {library && project?.can.edit && !project.archived && !search.collection ? (
            <SaveAsCollection project={project} search={search} />
          ) : null}
          {making && fixedProject?.can.edit && !fixedProject.archived && (library || makeable) ? (
            <Link
              to="/projects/$slug/entities/new/"
              params={{ slug: fixedProject.slug }}
              search={makeable ? { type: makeable.name } : {}}
              className={buttonClass("primary")}
            >
              New {makeable ? inSentence(makeable.label) : "entity"}
            </Link>
          ) : null}
        </div>
      </div>
      <FilterBar
        key={type?.name ?? ""}
        type={type}
        types={types}
        fields={fields}
        applied={filters}
        projectId={project?.id}
        noun={many}
        onApply={applyFilters}
      />
      {pills ? (
        <TypePills
          counts={counts.data?.types}
          total={counts.data?.total}
          types={types}
          chosen={type}
          onPick={pickType}
        />
      ) : null}
      {type && !only ? (
        <p className="toolbar-note muted">
          <Link to="/types/$/" params={{ _splat: type.name }}>
            About {type.label_plural}
          </Link>
        </p>
      ) : null}
      {!library || !project ? null : search.collection ? (
        <CollectionBanner reference={search.collection} project={project} />
      ) : null}
      {!library || !project || search.collection ? null : (
        <SavedSearches project={project} search={search} />
      )}
      {shownAs && project ? (
        <ShownAs
          key={shownAs.component.name}
          view={shownAs}
          search={gridSearch(filter) as PlaceSearch}
          project={project}
          types={types}
        />
      ) : null}
      {unknownProject && projectsLoaded ? (
        <p className="empty">
          There’s no project {search.project} you can read.{" "}
          <button
            type="button"
            className={buttonClass("quiet")}
            onClick={() => {
              navigate({ search: (prev) => ({ ...prev, project: undefined }) });
            }}
          >
            Show every project’s
          </button>
        </p>
      ) : null}
      {!shownAs && list.isError ? <ProblemAlert problem={problemOf(list.error)} /> : null}
      {!shownAs && (list.isPending || (unknownProject && !projectsLoaded)) ? (
        <Loading label="Loading entities…" />
      ) : null}
      {!shownAs && list.isSuccess && rows.length === 0 ? (
        <p className="empty">
          {empty
            ? empty(narrowed || show !== "current")
            : filter.collection
              ? `No members you can read${narrowed || type ? " match" : ""}.`
              : narrowed || (type && show !== "current")
                ? `No ${many} ${where} match.`
                : across && !project
                  ? `No ${many} you can read yet.`
                  : type
                    ? `No ${many} ${where}.`
                    : `No ${many} ${where} yet.`}
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
                  : picked.size && !runIn && across
                    ? ": an action runs in one project, and these aren’t all in one you run actions in"
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
              {picked.size && picked.size <= MAX_BATCH && runIn ? (
                <Link
                  to="/projects/$slug/runs/new/"
                  params={{ slug: runIn.slug }}
                  search={{ entities: [...picked].join(",") }}
                  className={buttonClass("primary")}
                >
                  Run action{across && !project ? ` in ${runIn.name}` : ""}
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
          {reassigning && type && editIn ? (
            <ReassignDialog
              type={type}
              projectId={editIn.id}
              picked={pickedRows.slice(0, MAX_REASSIGN)}
              onClose={(done) => {
                setReassigning(false);
                if (done) setSelection({ list: listKey, hrids: new Set() });
              }}
            />
          ) : null}
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
                      aria-selected={selectable ? picked.has(entity.hrid) : undefined}
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
          {across && !project ? (
            <p className="muted list-note">
              Records in projects you aren’t in never appear here, nor in any count.
            </p>
          ) : null}
        </>
      ) : null}
    </>
  );
}

/** The sort button's words: what it's sorted by, and which way. */
function sortLabel(field: FieldDef, key: string): string {
  return `${field.label}, ${directionWords(field, key.endsWith(":desc"))}`;
}

/** The columns a reader hid in this list, or those hidden until shown. */
function keptOr(list: string, columns: Column[]): Set<string> {
  return hiddenColumns(list) ?? new Set(columns.filter((c) => c.hidden).map((c) => c.key));
}

/** Which columns the grid shows: each but the ID, which every row needs. */
function ColumnsMenu({
  columns,
  hidden,
  onChange,
}: {
  columns: Column[];
  hidden: ReadonlySet<string>;
  onChange: (hidden: Set<string>) => void;
}) {
  const legendId = useId();
  return (
    <DialogTrigger>
      <AriaButton className={buttonClass()}>Columns</AriaButton>
      <Popover className="menu-popover" placement="bottom end" offset={4}>
        <Dialog className="columns-dialog" aria-labelledby={legendId}>
          <fieldset className="columns-choice">
            <legend id={legendId} className="menu-heading">
              Columns to show
            </legend>
            {columns.map((c) => (
              <label key={c.key} className="choice">
                <input
                  type="checkbox"
                  checked={!hidden.has(c.key)}
                  onChange={(event) => {
                    const next = new Set(hidden);
                    if (event.target.checked) next.delete(c.key);
                    else next.add(c.key);
                    onChange(next);
                  }}
                />
                <span className="choice-text">{c.label}</span>
              </label>
            ))}
          </fieldset>
        </Dialog>
      </Popover>
    </DialogTrigger>
  );
}

/** The types with records its reader can read, as pills: each with how many, unless
 * counting would be slow (ADR 0176). The one picked is pressed. */
function TypePills({
  counts,
  total,
  types,
  chosen,
  onPick,
}: {
  counts: { type: string; label: string; count: number | null }[] | undefined;
  total: number | null | undefined;
  types: EntityType[];
  chosen: EntityType | undefined;
  onPick: (type: string) => void;
}) {
  const listed = counts ?? [];
  // A type picked that has nothing counted (an abstract one, say) still shows, pressed.
  const all =
    chosen && !listed.some((c) => c.type === chosen.name)
      ? [...listed, { type: chosen.name, label: typeLabel(types, chosen.name), count: null }]
      : listed;
  const pill = (value: string, label: string, n: number | null | undefined) => (
    <button
      key={value || "all"}
      type="button"
      className="type-pill"
      aria-pressed={(chosen?.name ?? "") === value}
      onClick={() => {
        onPick(value);
      }}
    >
      {value ? <TypeDot type={value} /> : null}
      {label}
      {n === null || n === undefined ? null : (
        // Spaces between, as text, so a screen reader's name for it reads as it's seen.
        <>
          {" "}
          <span className="type-pill-count">{n.toLocaleString()}</span>{" "}
          <span className="visually-hidden">{n === 1 ? "record" : "records"}</span>
        </>
      )}
    </button>
  );
  return (
    <div className="type-pills" role="group" aria-label="Type">
      {pill("", "All types", total)}
      {all.map((c) => pill(c.type, c.label, c.count))}
    </div>
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
