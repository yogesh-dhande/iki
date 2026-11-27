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
  DEFAULT_SORT,
  entitiesQuery,
  gridSearch,
  inSentence,
  MAX_THEN,
  recordSearch,
  searchCsvUrl,
  sortKeysOf,
  sortKeyText,
  stateFields,
  typeDisplayName,
  typeDisplayText,
  typeLabel,
  valueText,
  type Entity,
  type EntityType,
  type FieldDef,
  type SortKey,
} from "../entities";
import {
  ASKED_BY_SORT,
  asksWhatRulesCaught,
  CREATED,
  filterable,
  HRID,
  PROJECT_FILTERS,
  searchWords,
  SENT_TO_SORT,
  sortable,
  UPDATED,
  type Filter,
} from "../filters";
import {
  countOf,
  countsQuery,
  hiddenColumns,
  keepHiddenColumns,
  useReadableProjects,
  type ListSearch,
} from "../lists";
import { MAX_BATCH } from "../runs";
import { TypeDot, TypeWithDot } from "../type-color";
import { buttonClass, Loading, ProblemAlert, RelativeTime, TypeName } from "../ui";
import { CollectionBanner, SaveAsCollection } from "./collections";
import {
  FilterBar,
  SearchBox,
  SortHeading,
  ToolbarSelect,
  ToolIcon,
  type Direction,
} from "./entity-filters";
import { PluginPlace } from "./plugin-place";
import { ReassignDialog } from "./reassign";
import { RecordSheet } from "./record-sheet";
import { ExportRecords, manyFormats } from "./records-export";
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
  /** Its width, when it's fixed: otherwise it's sized to what it holds. */
  width?: string;
  /** Hidden until someone shows it. */
  hidden?: boolean;
  /** Its column in the grid's download (ADR 0175), if it has one. */
  csv?: string;
  /** A cell's text, by which the column is sized, and which a cell cut short shows whole
   * when pointed at. */
  text?: (entity: Entity) => string;
  /** How it's sized: "id" never cut short; "long" text (a name) takes the room that's
   * left, and may be cut short; anything else is as wide as most of what it holds. */
  fit?: "id" | "long" | undefined;
  /** Its text is drawn in pills, as a record's state is: their padding takes room too. */
  pills?: boolean;
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
    fit: "id",
    text: (e) => e.hrid,
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
    text: (e) => {
      const t = types.find((x) => x.name === e.type);
      return t ? typeDisplayText(t, types) : e.type;
    },
    cell: (e) => {
      const t = types.find((x) => x.name === e.type);
      return (
        <TypeWithDot type={e.type} shown={t ? typeDisplayName(t, types) : { label: e.type }} />
      );
    },
  };
  const projects: Column = {
    key: "project",
    label: "Project",
    csv: "project",
    // A project's own tab lists its records: their other projects are there to show.
    hidden: !across,
    text: (e) => {
      const [first, ...more] = e.projects ?? [];
      return first ? `${first.name}${more.length ? ` +${String(more.length)}` : ""}` : "";
    },
    cell: (e) => <ProjectsCell entity={e} />,
  };
  // A request between projects (ADR 0132): who asked, and the service it went to.
  const sides: Column[] = (
    type?.request || shown.always
      ? [
          {
            key: "asked_by",
            label: "Asked by",
            sort: ASKED_BY_SORT.name,
            csv: "asked_by",
            text: (e: Entity) => e.request?.asking_project?.name ?? "",
            cell: (e: Entity) => e.request?.asking_project?.name ?? "",
          },
          {
            key: "sent_to",
            label: "Sent to",
            sort: SENT_TO_SORT.name,
            csv: "sent_to",
            text: (e: Entity) => e.request?.service?.name ?? "Not sent yet",
            cell: (e: Entity) =>
              e.request?.service?.name ?? <span className="muted">Not sent yet</span>,
          },
        ]
      : []
  ).filter((c) => c.key !== shown.omit);
  const updated: Column = {
    key: "updated",
    label: UPDATED.label,
    cell: (e) => <RelativeTime value={e.updated_at} />,
    sort: UPDATED.name,
    csv: "updated_at",
    // As long as the longest it says: "12 hours ago".
    text: () => "12 hours ago",
  };
  const name: Column = {
    key: "title",
    label: "Name",
    csv: "title",
    fit: "long",
    text: (e) => (e.title === e.hrid ? "" : e.title),
    cell: (e) => (e.title === e.hrid ? "" : e.title),
  };
  const made: Column = {
    key: "created_by",
    label: "Made by",
    text: (e) => e.created_by?.name ?? "",
    cell: (e) => e.created_by?.name ?? "",
  };
  if (!type) {
    const status: Column = {
      key: "status",
      label: "Status",
      pills: true,
      text: (e) => statusText(e, types),
      cell: (e) => <StatusCell entity={e} types={types} />,
    };
    return [id, name, kind, projects, ...sides, status, made, updated];
  }
  const fields: Column[] = type.fields.map((f: FieldDef) => ({
    key: `field:${f.name}`,
    label: f.type === "quantity" ? `${f.label} (${unitOf(f)})` : f.label,
    sort: sortable(f) ? f.name : undefined,
    csv: f.name,
    fit: LONG_FIELDS.has(f.type) && !f.format ? "long" : undefined,
    text: (e) => {
      const value = e.values[f.name];
      const said = valueText(f, value, e);
      if (f.type === "state" && e.locked_by.includes(f.name)) return `${said} Locked`;
      return said;
    },
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
  // Shown at first, as the mockups of ADR 0176 order them: its name, a few of its key
  // fields, its project, its states (ADR 0125), who made it and when it changed. The rest
  // are a click away, under Columns.
  const states = fields.filter((_, index) => type.fields[index]?.type === "state");
  const rest = fields.filter((_, index) => type.fields[index]?.type !== "state");
  const restDefs = type.fields.filter((f) => f.type !== "state");
  // Its name: its own name field, which sorts, or else its title.
  const named = restDefs.find((f) => NAME_FIELDS.has(f.name));
  const nameColumn = (named && rest.find((c) => c.key === `field:${named.name}`)) ?? name;
  // A request's sides (ADR 0132) are among its key facts: who asked, and the service it
  // went to. They say which projects it's in and who it's from, so those wait under
  // Columns. Each state past the first takes a key field's place too.
  const room = KEY_FIELDS - sides.length - Math.max(states.length - 1, 0);
  const keyFields = new Set(
    keyFieldsOf(type, types, restDefs, named)
      .slice(0, Math.max(room, 1))
      .map((f) => `field:${f.name}`),
  );
  const besides = sides.length > 0;
  return [
    id,
    { ...nameColumn, fit: "long" as const },
    ...(type.subtypes.length ? [kind] : []),
    ...sides,
    ...rest
      .filter((c) => c !== nameColumn)
      .map((c) => (keyFields.has(c.key) ? c : { ...c, hidden: true })),
    besides ? { ...projects, hidden: true } : projects,
    ...states,
    besides ? { ...made, hidden: true } : made,
    updated,
  ];
}

/** How many of a type's own fields its table shows at first, besides its name and states. */
const KEY_FIELDS = 4;
/** Fields too long, or too structured, to be one of a table's first columns. */
const NOT_KEY_FIELDS = new Set(["text", "json", "list", "file"]);
/** Fields that are the record's name, which its Name column already shows. */
const NAME_FIELDS = new Set(["name", "title"]);
/** Fields of free text, which take the room a table has left. */
const LONG_FIELDS = new Set(["string", "text"]);

/**
 * A type's fields that say most about a record, best first: those that make it what it is
 * (its identity, ADR 0115); then what's worked out from them (a length, a GC content);
 * then its other facts, by kind; plain text and yes-or-no after; and last what other
 * plugins add to it, or text a plugin writes (which software found something). Long text,
 * lists, JSON and files are never among them, nor its name.
 */
export function keyFieldsOf(
  type: EntityType,
  types: readonly EntityType[],
  fields: readonly FieldDef[],
  name?: FieldDef,
): FieldDef[] {
  // The plugins of the type and its parents: their fields are its own, not extensions'.
  const lineage = new Set<string>(["iki/core"]);
  let parent: string | null = type.extends;
  lineage.add(type.plugin);
  for (let depth = 0; parent && depth < 16; depth += 1) {
    const found = types.find((x) => x.name === parent);
    if (!found) break;
    lineage.add(found.plugin);
    parent = found.extends;
  }
  const FACTS = new Set(["enum", "quantity", "int", "float", "ref", "date", "datetime", "person"]);
  const rank = (f: FieldDef): number => {
    if (!lineage.has(f.plugin)) return 5;
    if (type.identity.includes(f.name)) return 0;
    if (f.computed && FACTS.has(f.type)) return 1;
    if (FACTS.has(f.type)) return 2;
    if (f.type === "string" && !f.computed) return 3;
    if (f.type === "bool") return 4;
    return 5;
  };
  const eligible = fields.filter((f) => !NOT_KEY_FIELDS.has(f.type) && f !== name);
  const order = new Map(eligible.map((f, index) => [f, index]));
  return [...eligible].sort(
    (a, b) => rank(a) - rank(b) || (order.get(a) ?? 0) - (order.get(b) ?? 0),
  );
}

/** A record's state as a row of any type says it: its first state, and whether it's locked. */
function statusText(entity: Entity, types: EntityType[]): string {
  const field = stateFields(types.find((t) => t.name === entity.type))[0];
  const value = field ? entity.values[field.name] : undefined;
  const said =
    field && value !== null && value !== undefined ? valueText(field, value, entity) : "";
  return [said, entity.locked ? "Locked" : ""].filter(Boolean).join(" ");
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
  // An enum, or a state field's states, sort in the order the type declares them (ADR 0232):
  // "routine first", "urgent first".
  if (field.type === "enum" || field.type === "state") {
    const values = field.values.length ? field.values : field.states.map((st) => st.name);
    const end = descending ? values[values.length - 1] : values[0];
    if (end !== undefined && values.length > 1) {
      const said = field.states.find((st) => st.name === end)?.label ?? end.replaceAll("_", " ");
      return `${inSentence(said)} first`;
    }
  }
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
  // A request's sides (ADR 0132) sort by their projects' names (ADR 0232), not by the IDs
  // their filters take.
  const sideSorts =
    type?.request || sides?.always
      ? [ASKED_BY_SORT, SENT_TO_SORT].filter((f) => f.name !== sides?.omit)
      : [];
  const sortFields = [
    UPDATED,
    CREATED,
    ...fields.filter(
      (f) =>
        sortable(f) &&
        f.name !== CREATED.name &&
        f.name !== UPDATED.name &&
        !PROJECT_FILTERS.has(f.name),
    ),
    ...sideSorts,
  ];
  // The address's sort, if it names one; without one, the default: last changed, newest
  // first (ADR 0176), so recent work is at the top. An index serves it across types.
  const chosenSort = sortFields.some((f) => f.name === search.sort) ? search.sort : undefined;
  const sort = chosenSort ?? DEFAULT_SORT.field;
  const descending = chosenSort ? Boolean(search.desc) : DEFAULT_SORT.descending;
  // Then by others, for what it ties (ADR 0232): each a field it can sort by, once.
  const then: SortKey[] = chosenSort
    ? sortKeysOf(search.then).filter(
        (t, i, all) =>
          t.field !== chosenSort &&
          sortFields.some((f) => f.name === t.field) &&
          all.findIndex((o) => o.field === t.field) === i,
      )
    : [];
  const narrowed = Boolean(search.q) || filters.length > 0;
  const filter = {
    project: project?.id,
    type: type?.name,
    archived: show === "all" ? undefined : show === "archived",
    q: search.q,
    filters: [...fixed, ...filters],
    sort,
    descending,
    then,
    collection: library ? search.collection : undefined,
    run: search.run,
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
  // How many there are, when the counts know: of the types it shows, current ones, with no
  // search or filter. The sidebar asks the same, so it's usually there already.
  const countable = !unknownProject && !search.collection && !search.run && show === "current";
  const counts = useQuery({ ...countsQuery(project?.id), enabled: countable });
  const applyFilters = (next: Filter[]) => {
    navigate({
      search: (prev) => ({ ...prev, filters: next.length ? next : undefined }),
      replace: true,
    });
  };
  const sortBy = (field: string, direction: Direction) => {
    // The default order is kept as no sort, so the address stays short. What it's then
    // sorted by stays, but for the field it's now sorted by first.
    const byDefault =
      direction === "none" ||
      (field === DEFAULT_SORT.field && (direction === "descending") === DEFAULT_SORT.descending);
    const rest = then.filter((t) => t.field !== field).map(sortKeyText);
    navigate({
      search: (prev) => ({
        ...prev,
        sort: byDefault ? undefined : field,
        desc: !byDefault && direction === "descending" ? true : undefined,
        then: !byDefault && rest.length ? rest : undefined,
      }),
    });
  };
  const thenBy = (next: SortKey[]) => {
    navigate({
      search: (prev) => ({ ...prev, then: next.length ? next.map(sortKeyText) : undefined }),
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
        run: prev.run,
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
    descending,
    then,
    search.collection,
    search.run,
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
  const acts = across ? readable.some(actsIn) : Boolean(project && actsIn(project));
  // Records of a type that exports a file (ADR 0229) are picked to export, by any reader.
  const shownTypes = type ? [type.name] : [...new Set(rows.map((e) => e.type))];
  const selectable = acts || manyFormats(shownTypes, types).length > 0;
  const [reassigning, setReassigning] = useState(false);
  // The list being edited as a sheet, if one is.
  const [sheet, setSheet] = useState<string | null>(null);
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
  const sized = sizeColumns(columns, rows.slice(0, PAGE));
  const { template, minWidth } = sized;
  // The types it offers and their subtypes, an abstract one as any of its kinds.
  const typeOptions = [
    { value: "", label: `All ${plural.many}` },
    ...types
      .filter((t) => offered.has(t.name) && (!t.abstract || t.subtypes.length))
      .map((t) => {
        const label = typeDisplayText(t, types);
        return { value: t.name, label: t.abstract ? `${label} (any kind)` : label };
      })
      .sort((a, b) => a.label.localeCompare(b.label)),
  ];
  // The types it lists, for their count: its type's, or those its fixed conditions hold it
  // to; null for every type. Any other fixed condition (a request's side, say) leaves it
  // uncounted: the counts know types, not sides.
  const onlyTypes = fixed.every(
    (f) => f.field === "type" && f.op === "in" && Array.isArray(f.value),
  );
  const covered: ReadonlySet<string> | null | undefined = !onlyTypes
    ? undefined
    : type
      ? new Set([type.name, ...type.subtypes])
      : fixed.length === 0
        ? null
        : new Set(fixed.flatMap((f) => (Array.isArray(f.value) ? f.value.map(String) : [])));
  const exact = countable && covered !== undefined ? countOf(counts.data, covered) : null;
  const loaded = list.hasNextPage ? `${String(rows.length)}+` : String(rows.length);
  // Unnarrowed, the count the counts know, if they do; narrowed, how many of them match.
  const shown = !narrowed && exact !== null ? exact : rows.length;
  const one = (narrowed && exact !== null ? exact : shown) === 1 && !list.hasNextPage;
  const noun = type
    ? inSentence(one ? type.label : type.label_plural)
    : one
      ? plural.one
      : plural.many;
  const count =
    exact === null
      ? `${loaded} ${noun}${show === "archived" ? ", archived" : ""}${narrowed ? " match" : ""}`
      : narrowed
        ? `${loaded} of ${exact.toLocaleString()} ${noun}`
        : `${exact.toLocaleString()} ${noun}`;
  const many = type ? inSentence(type.label_plural) : plural.many;
  const makeable = type && !type.abstract && !type.detached ? type : undefined;
  const sortedBy = sortFields.find((f) => f.name === sort) ?? UPDATED;
  const sortKey = `${sortedBy.name}:${descending ? "desc" : "asc"}`;
  // A project's own tab says "this project"; across projects, the one picked, by name.
  const where = fixedProject ? "in this project" : project ? `in ${project.name}` : "you can read";
  // A type's records in a project, edited as a sheet (ADR 0228): those the table loaded,
  // with its search and filters, which stay as they are until it's done. The table reads
  // them again as it goes; the sheet keeps its rows where they were (sheet.ts `Loaded`).
  const sheetType =
    type && !type.detached && fixedProject?.can.edit && !fixedProject.archived && !shownAs
      ? type
      : undefined;
  if (sheetType && fixedProject && sheet === listKey) {
    return list.isPending ? (
      <Loading label="Loading entities…" />
    ) : (
      <RecordSheet
        type={sheetType}
        project={fixedProject}
        records={rows}
        more={list}
        onClose={() => {
          setSheet(null);
        }}
      />
    );
  }

  return (
    <>
      <div className="list-toolbar">
        {/* Keyed apart from the filters beside it: two siblings keyed "" are one to React. */}
        <SearchBox
          key={`q:${search.q ?? ""}`}
          value={search.q ?? ""}
          placeholder={
            type?.fields.some((f) => f.assignment)
              ? `Search by ID, name, or assigned:me`
              : type?.rules?.some((r) => r.hold)
                ? `Search by ID, name, or is:held`
                : type
                  ? `Search ${inSentence(type.label_plural)} by ID or name`
                  : across
                    ? "Search your projects by ID or name"
                    : "Search by ID or name"
          }
          onSearch={(q) => {
            navigate({ search: (prev) => ({ ...prev, q: q || undefined }), replace: true });
          }}
        />
        {across ? (
          <ToolbarSelect
            label="Project"
            value={chosen?.slug ?? search.project ?? ""}
            options={[
              { value: "", label: "Any" },
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
          <ToolbarSelect
            label="Type"
            value={type?.name ?? ""}
            options={typeOptions}
            onChange={pickType}
          />
        )}
        <FilterBar
          key={`filters:${type?.name ?? ""}`}
          type={type}
          types={types}
          fields={fields}
          applied={filters}
          projectId={project?.id}
          noun={many}
          onApply={applyFilters}
          show={{
            value: show,
            onChange: (value) => {
              navigate({
                search: (prev) => ({ ...prev, show: value === "current" ? undefined : value }),
              });
            },
          }}
        />
        <MenuTrigger>
          <AriaButton
            className={buttonClass()}
            aria-label={`Sort: ${sortLabel(sortedBy, sortKey)}`}
          >
            <ToolIcon name="sort" />
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
                sortBy(name, direction === "desc" ? "descending" : "ascending");
              }}
            >
              {sortChoices(sortFields)}
            </Menu>
          </Popover>
        </MenuTrigger>
        {chosenSort ? (
          <ThenBy
            fields={sortFields.filter((f) => f.name !== chosenSort)}
            then={then}
            onChange={thenBy}
          />
        ) : null}
        {views.length ? (
          <ToolbarSelect
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
        <div className="list-toolbar-end" hidden={unknownProject}>
          {!shownAs && list.isSuccess && rows.length ? (
            <p className="muted list-count" id={captionId}>
              {count}
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
            <ToolIcon name="download" />
            Download CSV
          </a>
          {/* As one FASTA, GenBank or SDF file, each record pinned (ADR 0229). */}
          {!shownAs && rows.length ? (
            <ExportRecords filter={filter} typeNames={shownTypes} types={types} />
          ) : null}
          {library && project?.can.edit && !project.archived && !search.collection ? (
            <SaveAsCollection project={project} search={search} />
          ) : null}
          {sheetType ? (
            <button
              type="button"
              className={buttonClass()}
              onClick={() => {
                setSheet(listKey);
              }}
            >
              Edit as sheet
            </button>
          ) : null}
          {making && fixedProject?.can.edit && !fixedProject.archived ? (
            // A spreadsheet's rows, as records of the type shown, or one chosen (ADR 0178).
            <Link
              to="/projects/$slug/entities/import/"
              params={{ slug: fixedProject.slug }}
              search={makeable ? { type: makeable.name } : {}}
              className={buttonClass()}
            >
              <ToolIcon name="upload" />
              Import
            </Link>
          ) : null}
          {/* The page's header makes a record of any type, as its one primary action; a type
              picked here can be made straight away, its form already of that type. */}
          {making && makeable && fixedProject?.can.edit && !fixedProject.archived ? (
            <Link
              to="/projects/$slug/entities/new/"
              params={{ slug: fixedProject.slug }}
              search={{ type: makeable.name }}
              className={buttonClass()}
            >
              New {inSentence(makeable.label)}
            </Link>
          ) : null}
        </div>
      </div>
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
      {search.run ? (
        <RunBanner
          run={search.run}
          onClear={() => {
            navigate({ search: (prev) => ({ ...prev, run: undefined }) });
          }}
        />
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
                {!acts
                  ? ""
                  : picked.size > MAX_BATCH
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
                <ExportRecords
                  filter={filter}
                  typeNames={[...new Set(pickedRows.map((e) => e.type))]}
                  types={types}
                  picked={pickedRows.map((e) => e.id)}
                  label="Export selected"
                />
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
              refresh={async () => {
                const read = await list.refetch({ throwOnError: true });
                return (read.data?.pages.flatMap((p) => p.items) ?? [])
                  .filter((e) => picked.has(e.hrid))
                  .slice(0, MAX_REASSIGN);
              }}
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
                      ? descending
                        ? "descending"
                        : "ascending"
                      : "none";
                    // Sorted by default (newest first), a click turns it round: going on to no
                    // sort would leave it as it is.
                    const byDefault = sorted && !chosenSort;
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
                              next={byDefault ? "ascending" : undefined}
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
                      {columns.map((c, index) => (
                        <div
                          role="cell"
                          key={c.key}
                          className="data-grid-cell"
                          title={cutShort(c, entity, sized.widths[index])}
                        >
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

/** About how wide an ID's monospace characters are, in pixels at the stylesheet's 13px rem
 * (ADR 0176); and a cell's padding, both sides. */
const MONO_CHAR = 7.3;
const PADDING = 22;

/** About how wide `text` is in the grid's 13px Inter, in pixels: capitals and digits are
 * wider than lower case, and spaces narrower. Near enough to size a column by. */
function textWidth(text: string): number {
  let width = 0;
  for (const c of text) {
    if (c === " ") width += 3.6;
    else if (/[A-Z0-9%]/.test(c)) width += 8.4;
    else if (/[a-z]/.test(c)) width += 7;
    else width += 6;
  }
  return width;
}
/** The narrowest a column is, and the widest one sized to what it holds; long text may be
 * cut to this, and takes what room is left. */
const NARROWEST = 56;
const WIDEST = 260;
const LONG_AT_LEAST = 180;
const LONG_AT_MOST = 420;
/** Rows the columns are sized by: the first page, so they don't shift as more load. */
const PAGE = 100;

/**
 * Each column's width, from what it holds (ADR 0176): an ID as wide as the longest; long
 * text as wide as it can be, given the room, and cut short past that; anything else as
 * wide as its heading or most of its values. Columns grow, in proportion, to fill a wide
 * screen. Widths are in pixels at the stylesheet's 13px rem, so they scale with the
 * reader's text size. `widths` is each column's narrowest, in those pixels.
 */
export function sizeColumns(
  columns: readonly Pick<Column, "label" | "sort" | "width" | "text" | "fit" | "pills">[],
  rows: readonly Entity[],
): { template: string; minWidth: string; widths: number[] } {
  const px = (n: number) => `calc(${String(Math.round(n))}rem / 13)`;
  const grows = (min: number, grow: number) => `minmax(${px(min)}, ${(grow / 100).toFixed(2)}fr)`;
  const tracks: string[] = [];
  const widths: number[] = [];
  for (const c of columns) {
    if (c.width) {
      // Only the selection column and the rules caught are fixed: theirs, as they set.
      const fixed = /calc\((\d+)rem/.exec(c.width);
      tracks.push(c.width);
      widths.push(fixed ? Number(fixed[1]) : NARROWEST);
      continue;
    }
    // A heading is 12px, a little narrower than the cells' text.
    const heading = textWidth(c.label) * 0.93 + PADDING + (c.sort ? 16 : 0);
    const texts = c.text ? rows.map((e) => c.text?.(e) ?? "") : [];
    if (c.fit === "id") {
      const longest = Math.max(10, ...texts.map((t) => t.length));
      const width = Math.max(heading, longest * MONO_CHAR + PADDING);
      tracks.push(px(width));
      widths.push(width);
      continue;
    }
    // Most of its values: one long outlier doesn't widen a column for every row.
    const sorted = texts.map(textWidth).sort((x, y) => x - y);
    const most = sorted.length ? (sorted[Math.ceil((sorted.length - 1) * 0.9)] ?? 0) : 0;
    // A pill's padding, and the gap between two (a state, and "Locked").
    const natural = Math.max(heading, most + PADDING + (c.pills ? 34 : 0));
    if (c.fit === "long") {
      const min = Math.min(natural, Math.max(heading, LONG_AT_LEAST));
      tracks.push(grows(min, Math.min(Math.max(natural, LONG_AT_LEAST), LONG_AT_MOST)));
      widths.push(min);
      continue;
    }
    const width = Math.min(Math.max(natural, NARROWEST), WIDEST);
    tracks.push(grows(width, width));
    widths.push(width);
  }
  return {
    template: tracks.join(" "),
    minWidth: px(widths.reduce((sum, w) => sum + w, 0)),
    widths,
  };
}

/** A cell's whole text, to show when pointed at, if it may be cut short: when it's wider
 * than its column's narrowest. A long sequence's is shown in part. */
function cutShort(column: Column, entity: Entity, width: number | undefined): string | undefined {
  if (!column.text || column.fit === "id" || width === undefined) return undefined;
  const text = column.text(entity);
  if (!text || textWidth(text) + PADDING <= width) return undefined;
  return text.length > 500 ? `${text.slice(0, 500)}…` : text;
}

/** The sort button's words: what it's sorted by, and which way. */
function sortLabel(field: FieldDef, key: string): string {
  return `${field.label}, ${directionWords(field, key.endsWith(":desc"))}`;
}

/** A sort menu's items: each field, each way, as `name:asc` and `name:desc`. */
function sortChoices(fields: readonly FieldDef[]) {
  return fields.flatMap((f) =>
    // Times newest first first: that's how people look for recent work.
    (f.type === "datetime" || f.type === "date" ? [true, false] : [false, true]).map((desc) => {
      const key = `${f.name}:${desc ? "desc" : "asc"}`;
      return (
        <MenuItem key={key} id={key} className="menu-item">
          {f.label}, {directionWords(f, desc)}
        </MenuItem>
      );
    }),
  );
}

function sortKeyOf(key: string): SortKey {
  const [field = "", direction] = key.split(":");
  return { field, descending: direction === "desc" };
}

/**
 * What a grid is sorted by after its first sort, for the rows that tie (ADR 0232): a core
 * sorts its queue by its own ranking of the projects it serves, then priority, then when
 * each is needed. Each is a menu of its own, which can change it or take it away; one more
 * can be added, up to the most the API takes.
 */
function ThenBy({
  fields,
  then,
  onChange,
}: {
  /** What it can sort by, besides the first sort. */
  fields: readonly FieldDef[];
  then: readonly SortKey[];
  onChange: (then: SortKey[]) => void;
}) {
  const named = (key: SortKey) => fields.find((f) => f.name === key.field);
  const free = (except?: string) =>
    fields.filter((f) => f.name === except || !then.some((t) => t.field === f.name));
  const more = free();
  return (
    <>
      {then.map((key, index) => {
        const field = named(key);
        if (!field) return null;
        const words = `${field.label}, ${directionWords(field, key.descending)}`;
        return (
          <MenuTrigger key={key.field}>
            <AriaButton className={buttonClass()}>
              <span className="muted">then</span> {words}
            </AriaButton>
            <Popover className="menu-popover" placement="bottom start" offset={4}>
              <Menu
                className="menu"
                aria-label={`Then by, after ${index === 0 ? "the sort" : "the one before"}`}
                selectionMode="single"
                selectedKeys={[`${key.field}:${key.descending ? "desc" : "asc"}`]}
                onAction={(chosen) => {
                  const next = [...then];
                  if (chosen === "remove") next.splice(index, 1);
                  else next[index] = sortKeyOf(String(chosen));
                  onChange(next);
                }}
              >
                {sortChoices(free(key.field))}
                <MenuItem id="remove" className="menu-item">
                  Don’t sort by {inSentence(field.label)}
                </MenuItem>
              </Menu>
            </Popover>
          </MenuTrigger>
        );
      })}
      {then.length < MAX_THEN && more.length ? (
        <MenuTrigger>
          <AriaButton className={buttonClass()}>Then by…</AriaButton>
          <Popover className="menu-popover" placement="bottom start" offset={4}>
            <Menu
              className="menu"
              aria-label="Then by"
              onAction={(chosen) => {
                onChange([...then, sortKeyOf(String(chosen))]);
              }}
            >
              {sortChoices(more)}
            </Menu>
          </Popover>
        </MenuTrigger>
      ) : null}
    </>
  );
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
      <AriaButton className={buttonClass()}>
        <ToolIcon name="columns" />
        Columns
      </AriaButton>
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
 * counting would be slow (ADR 0176). The one picked is pressed. One row, which scrolls
 * across when there are more than fit, with the pressed one scrolled into view. Two types
 * with the same label each say their plugin. */
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
  const row = useRef<HTMLDivElement>(null);
  const listed = counts ?? [];
  // A type picked that has nothing counted (an abstract one, say) still shows, pressed.
  const all =
    chosen && !listed.some((c) => c.type === chosen.name)
      ? [...listed, { type: chosen.name, label: typeLabel(types, chosen.name), count: null }]
      : listed;
  const picked = chosen?.name ?? "";
  useEffect(() => {
    const pressed = row.current?.querySelector<HTMLElement>("[aria-pressed=true]");
    const box = row.current;
    if (!pressed || !box) return;
    // Scrolled within the row alone: the page doesn't move.
    const left = pressed.offsetLeft - box.offsetLeft;
    if (left < box.scrollLeft || left + pressed.offsetWidth > box.scrollLeft + box.clientWidth) {
      box.scrollLeft = left - box.clientWidth / 2 + pressed.offsetWidth / 2;
    }
  }, [picked, all.length]);
  const pill = (value: string, label: ReactNode, n: number | null | undefined) => (
    <button
      key={value || "all"}
      type="button"
      className="type-pill"
      aria-pressed={picked === value}
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
    <div className="type-pills" role="group" aria-label="Type" ref={row}>
      {pill("", "All types", total)}
      {all.map((c) => {
        const t = types.find((x) => x.name === c.type);
        const shown = t ? typeDisplayName(t, types) : { label: c.label };
        return pill(c.type, <TypeName shown={shown} />, c.count);
      })}
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

/** Only the records a run wrote, such as a spreadsheet's import (ADR 0178): which run, and
 * a way back to every record. */
function RunBanner({ run, onClear }: { run: string; onClear: () => void }) {
  return (
    <div className="alert alert-info collection-banner" role="note">
      <p>
        Records written by run{" "}
        <Link to="/runs/$ref/" params={{ ref: run }}>
          {run}
        </Link>
        .
      </p>
      <button type="button" className={buttonClass("quiet")} onClick={onClear}>
        Show every record
      </button>
    </div>
  );
}
