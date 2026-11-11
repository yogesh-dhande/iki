/**
 * A project's entities, as a grid: one type's fields as columns, or every
 * type's IDs. Rows are drawn only as they scroll into view, and more are
 * fetched before the end is reached, so a project of a million entities
 * scrolls like one of ten.
 */

import { useInfiniteQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useId, useRef, useState } from "react";

import {
  entitiesQuery,
  inSentence,
  typeLabel,
  typesQuery,
  valueText,
  type Entity,
  type EntityType,
  type FieldDef,
} from "../entities";
import type { Showing } from "../entities";
import { filterable, HRID, sortable, UPDATED, type Filter } from "../filters";
import { problemOf } from "../api";
import { usePageTitle } from "../layout";
import { buttonClass, Loading, ProblemAlert, RelativeTime, SelectField } from "../ui";
import { FilterPanel, SearchBox, SortHeading, type Direction } from "./entity-filters";
import { useProject } from "./project-overview";
import { CollectionBanner, SaveAsCollection } from "./collections";
import { SavedSearches } from "./saved-searches";

const ROW_HEIGHT = 44;
/** Fetch the next page when this many rows remain below the view. */
const AHEAD = 30;

interface Column {
  key: string;
  label: string;
  cell: (entity: Entity) => React.ReactNode;
  /** The field it sorts by, if it can. */
  sort?: string | undefined;
}

function columnsFor(type: EntityType | undefined, types: EntityType[]): Column[] {
  const id: Column = {
    key: "hrid",
    label: "ID",
    sort: HRID.name,
    cell: (e) => (
      <Link to="/entities/$ref/" params={{ ref: e.hrid }}>
        {e.hrid}
      </Link>
    ),
  };
  const kind: Column = {
    key: "type",
    label: "Type",
    cell: (e) => typeLabel(types, e.type),
  };
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
    return [id, kind, name, updated];
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
      return valueText(f, value, e);
    },
  }));
  return [id, ...(type.subtypes.length ? [kind] : []), ...fields, updated];
}

function unitOf(field: FieldDef): string {
  if (field.display_scale != null && field.display_unit) {
    return field.display_unit_symbol ?? field.display_unit;
  }
  return field.unit_symbol ?? field.unit ?? "";
}

export function ProjectEntities() {
  const project = useProject();
  const search = useSearch({ from: "/projects/$slug/entities" });
  const navigate = useNavigate({ from: "/projects/$slug/entities" });
  const types = useSuspenseQuery(typesQuery).data;
  const type = types.find((t) => t.name === search.type);
  const show = search.show ?? "current";
  usePageTitle(`${type ? type.label_plural : "Entities"} · ${project.name}`);

  const fields = filterable(type);
  const filters = search.filters ?? [];
  const sort = fields.some((f) => f.name === search.sort && sortable(f)) ? search.sort : undefined;
  const narrowed = Boolean(search.q) || filters.length > 0;
  const [filtersOpen, setFiltersOpen] = useState(filters.length > 0);
  const panelId = useId();
  const list = useInfiniteQuery(
    entitiesQuery({
      project: project.id,
      type: type?.name,
      archived: show === "all" ? undefined : show === "archived",
      q: search.q,
      filters,
      sort,
      descending: sort ? search.desc : undefined,
      collection: search.collection,
    }),
  );
  const applyFilters = (next: Filter[]) => {
    void navigate({
      search: (prev) => ({ ...prev, filters: next.length ? next : undefined }),
      replace: true,
    });
  };
  const sortBy = (field: string, direction: Direction) => {
    void navigate({
      search: (prev) => ({
        ...prev,
        sort: direction === "none" ? undefined : field,
        desc: direction === "descending" ? true : undefined,
      }),
    });
  };
  const rows = list.data?.pages.flatMap((p) => p.items) ?? [];
  const columns = columnsFor(type, types);

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
  const template = `repeat(${String(columns.length)}, minmax(9rem, 1fr))`;
  const minWidth = `${String(columns.length * 9)}rem`;
  const typeOptions = [
    { value: "", label: "All types" },
    ...types
      .filter((t) => !t.abstract || t.subtypes.length)
      .map((t) => ({ value: t.name, label: t.abstract ? `${t.label} (any kind)` : t.label }))
      .sort((a, b) => a.label.localeCompare(b.label)),
  ];
  const count = list.hasNextPage ? `${String(rows.length)}+` : String(rows.length);
  const one = rows.length === 1 && !list.hasNextPage;
  const noun = type
    ? inSentence(one ? type.label : type.label_plural)
    : one
      ? "entity"
      : "entities";

  return (
    <>
      <div className="toolbar">
        <SelectField
          label="Type"
          value={type?.name ?? ""}
          options={typeOptions}
          onChange={(value) => {
            // Other types have other fields: their filters and sort don't carry over.
            void navigate({
              search: (prev) => ({
                type: value || undefined,
                show: prev.show,
                q: prev.q,
                collection: prev.collection,
              }),
            });
          }}
        />
        <SelectField<Showing>
          label="Show"
          value={show}
          options={[
            { value: "current", label: "Current" },
            { value: "archived", label: "Archived" },
            { value: "all", label: "Current and archived" },
          ]}
          onChange={(value) => {
            void navigate({
              search: (prev) => ({ ...prev, show: value === "current" ? undefined : value }),
            });
          }}
        />
        <SearchBox
          key={search.q ?? ""}
          value={search.q ?? ""}
          placeholder={type ? `ID or ${inSentence(type.label)} name` : "ID or name"}
          onSearch={(q) => {
            void navigate({ search: (prev) => ({ ...prev, q: q || undefined }), replace: true });
          }}
        />
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
            <Link to="/types/$name/" params={{ name: type.name }}>
              About {type.label_plural}
            </Link>
          </p>
        ) : null}
        {project.can.edit && !project.archived && !search.collection ? (
          <SaveAsCollection project={project} search={search} />
        ) : null}
        {project.can.edit && !project.archived ? (
          <Link
            to="/projects/$slug/entities/new/"
            params={{ slug: project.slug }}
            search={type && !type.abstract ? { type: type.name } : {}}
            className={`${buttonClass("primary")} toolbar-end`}
          >
            New {type && !type.abstract ? inSentence(type.label) : "entity"}
          </Link>
        ) : null}
      </div>
      {search.collection ? (
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
      {list.isError ? <ProblemAlert problem={problemOf(list.error)} /> : null}
      {list.isPending ? <Loading label="Loading entities…" /> : null}
      {list.isSuccess && rows.length === 0 ? (
        <p className="empty">
          {search.collection
            ? `No members you can read${narrowed || type ? " match" : ""}.`
            : narrowed
              ? `No ${type ? inSentence(type.label_plural) : "entities"} in this project match.`
              : type
                ? `No ${inSentence(type.label_plural)} in this project${show === "current" ? "" : " match"}.`
                : "No entities in this project yet."}
        </p>
      ) : null}
      {rows.length ? (
        <>
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
                        {c.sort ? (
                          <SortHeading
                            label={c.label}
                            direction={direction}
                            onSort={(next) => {
                              if (c.sort) sortBy(c.sort, next);
                            }}
                          />
                        ) : (
                          c.label
                        )}
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
