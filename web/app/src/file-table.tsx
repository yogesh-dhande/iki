/**
 * A list of files (ADR 0177): its toolbar (search, format, sort, and filters by when its
 * data was acquired, sample and run) and its table, a page at a time. One component for a
 * project's Files tab, the Files list across projects, and the file picker, so each finds
 * files the same way.
 */

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useId, useState, type ReactNode } from "react";

import { problemOf, type Project } from "./api";
import { FieldInput } from "./entity-inputs";
import { formatSize, typesQuery, type FieldDef } from "./entities";
import {
  acquiredText,
  fileCountQuery,
  fileFormatsQuery,
  filesQuery,
  SORTS,
  usedByText,
  type FileFilter,
  type FileRow,
  type FileSort,
  type Takes,
} from "./files";
import { formatsQuery } from "./media";
import { SearchBox, ToolbarSelect } from "./pages/entity-filters";
import { Button, Loading, ProblemAlert, RelativeTime } from "./ui";

/** Choosing files from the list, for a picker. */
export interface Picking {
  /** Several at once, up to `room` more; or one. */
  multiple: boolean;
  room?: number | undefined;
  chosen: ReadonlyMap<string, FileRow>;
  /** Already in the list it's choosing for: shown as chosen, and not chosen again. */
  held: ReadonlySet<string>;
  onToggle: (file: FileRow, chosen: boolean) => void;
}

/** A synthetic reference field: the Sample filter's picker is a reference picker's, held
 * to the sample types. */
function sampleField(types: string[]): FieldDef {
  return {
    name: "sample",
    label: "Sample",
    type: "ref",
    to: types,
    description: null,
    required: false,
    assignment: false,
    assignment_field: null,
    assignment_states: [],
    computed: false,
    detached: false,
    display_offset: null,
    display_scale: null,
    display_unit: null,
    display_unit_symbol: null,
    immutable: false,
    initial: null,
    input_units: [],
    items: null,
    link: null,
    plugin: "iki/core",
    role: null,
    role_label: null,
    states: [],
    unique: false,
    unit: null,
    unit_symbol: null,
    values: [],
  };
}

export function FileTable({
  projectId,
  across,
  filter,
  onFilter,
  takes,
  pick,
  actions,
  caption,
}: {
  /** The project whose files these are; across projects if none. */
  projectId: string | undefined;
  /** Across projects: those the reader can read, the one picked, and picking another. */
  across?: { projects: Project[]; slug: string | undefined; onProject: (slug: string) => void };
  filter: FileFilter;
  onFilter: (filter: FileFilter) => void;
  /** What a field or param takes: only those files are listed. */
  takes?: Takes | undefined;
  pick?: Picking | undefined;
  actions?: ReactNode;
  caption: string;
}) {
  const catalog = useQuery(formatsQuery);
  const media = catalog.data;
  const unknown = Boolean(across?.slug && !projectId);
  const requestTakes = takes && media ? { ...takes, accepts: media.choices(takes.accepts) } : takes;
  const enabled = !unknown && (!takes?.accepts.length || Boolean(media));
  const files = useInfiniteQuery({ ...filesQuery(projectId, filter, requestTakes), enabled });
  const count = useQuery({ ...fileCountQuery(projectId, filter, requestTakes), enabled });
  const formats = useQuery({ ...fileFormatsQuery(projectId, filter, requestTakes), enabled });
  const types = useQuery(typesQuery).data ?? [];
  const samples = types.filter((t) => t.sample && !t.abstract).map((t) => t.name);
  const items = files.data?.pages.flatMap((p) => p.items) ?? [];
  const names = new Map((across?.projects ?? []).map((p) => [p.id, p]));
  // How many of the filters beneath the toolbar are set: the acquired range counts once.
  const narrowing = [
    Boolean(filter.acquiredFrom ?? filter.acquiredTo),
    Boolean(filter.sample),
    Boolean(filter.run),
  ].filter(Boolean).length;
  const narrowed = narrowing > 0;
  const [open, setOpen] = useState(narrowed);
  const panelId = useId();
  const set = (change: Partial<FileFilter>) => {
    onFilter({ ...filter, ...change });
  };
  // Existing links use media types. IDs and aliases select the same facet.
  const chosenFormat = filter.format.map((type) => media?.get(type)?.canonical ?? type).join(",");
  const formatOptions = [
    { value: "", label: "Any" },
    ...(formats.data ?? []).map((f) => ({
      value: f.media_type,
      label: `${f.label} (${f.count.toLocaleString()})`,
    })),
    ...(chosenFormat && !(formats.data ?? []).some((f) => f.media_type === chosenFormat)
      ? [{ value: chosenFormat, label: filter.format.map((f) => media?.named(f) ?? f).join(", ") }]
      : []),
  ];
  const total = count.data
    ? `${count.data.total.toLocaleString()}${count.data.more_than_total ? "+" : ""} ${
        count.data.total === 1 && !count.data.more_than_total ? "file" : "files"
      }`
    : "";
  const searched = filter.q || filter.format.length || narrowed;

  return (
    <div className="file-table">
      <div className="list-toolbar">
        <SearchBox
          value={filter.q}
          placeholder="Search files by name"
          onSearch={(q) => {
            set({ q });
          }}
        />
        {across ? (
          <ToolbarSelect
            label="Project"
            value={across.slug ?? ""}
            options={[
              { value: "", label: "Any" },
              ...(across.slug && !projectId ? [{ value: across.slug, label: across.slug }] : []),
              ...across.projects.map((p) => ({ value: p.slug, label: p.name })),
            ]}
            onChange={across.onProject}
          />
        ) : null}
        <ToolbarSelect
          label="Format"
          value={chosenFormat}
          options={formatOptions}
          onChange={(value) => {
            set({ format: value ? value.split(",") : [] });
          }}
        />
        <ToolbarSelect<FileSort>
          label="Sort"
          value={filter.sort}
          options={SORTS}
          onChange={(sort) => {
            set({ sort });
          }}
        />
        <button
          type="button"
          className="button"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => {
            setOpen(!open);
          }}
        >
          Filters
          {narrowed ? <span className="file-filter-count">{narrowing}</span> : null}
        </button>
        <div className="list-toolbar-end">
          {total ? (
            <p className="list-count muted" aria-live="polite">
              {total}
            </p>
          ) : null}
          {actions}
        </div>
      </div>
      {open ? (
        <div className="file-filters" id={panelId} role="group" aria-label="Filters">
          <div className="file-filter-dates">
            <label>
              Acquired from
              <input
                type="date"
                value={filter.acquiredFrom ?? ""}
                max={filter.acquiredTo}
                onChange={(event) => {
                  set({ acquiredFrom: event.target.value || undefined });
                }}
              />
            </label>
            <label>
              to
              <input
                type="date"
                value={filter.acquiredTo ?? ""}
                min={filter.acquiredFrom}
                onChange={(event) => {
                  set({ acquiredTo: event.target.value || undefined });
                }}
              />
            </label>
          </div>
          <div className="file-filter-sample">
            {samples.length ? (
              <FieldInput
                field={sampleField(samples)}
                type={{ values_schema: {} }}
                types={types}
                projectId={projectId ?? ""}
                draft={
                  filter.sample
                    ? { id: filter.sample, hrid: filter.sample, title: filter.sample }
                    : null
                }
                note="Its files, and those a run that read it read or made."
                onChange={(draft) => {
                  const picked = draft as { id?: string } | null;
                  set({ sample: picked?.id });
                }}
              />
            ) : (
              <p className="muted file-filter-none">
                No type here holds samples, so there are none to filter by.
              </p>
            )}
          </div>
          <RunFilter
            value={filter.run}
            onChange={(run) => {
              set({ run });
            }}
          />
          {narrowed ? (
            <Button
              variant="quiet"
              onClick={() => {
                set({
                  acquiredFrom: undefined,
                  acquiredTo: undefined,
                  sample: undefined,
                  run: undefined,
                });
              }}
            >
              Clear filters
            </Button>
          ) : null}
        </div>
      ) : null}
      {unknown ? <p className="empty">There’s no project {across?.slug} you can read.</p> : null}
      {catalog.error ? <ProblemAlert problem={problemOf(catalog.error)} /> : null}
      {files.error ? <ProblemAlert problem={problemOf(files.error)} /> : null}
      {files.isPending && !unknown && !catalog.error ? <Loading label="Loading files…" /> : null}
      {files.isSuccess && items.length === 0 ? (
        <p className="muted empty">
          {searched
            ? "No files match. Try another part of the name, or fewer filters."
            : takes?.accepts.length
              ? `No ${media?.either(takes.accepts) ?? takes.accepts.join(", ")} files here yet. Upload one instead.`
              : projectId
                ? "No files yet. Files uploaded to this project, or held by its records, are listed here."
                : "No files you can read yet."}
        </p>
      ) : null}
      {items.length ? (
        <div className="list-table-box">
          {/* Named by its caption, not aria-labelledby: Firefox crashes working out names
              through aria-labelledby after axe has run. */}
          <table className="list-table file-rows">
            <caption className="visually-hidden">{caption}</caption>
            <thead>
              <tr>
                {pick ? (
                  <th scope="col">
                    <span className="visually-hidden">Choose</span>
                  </th>
                ) : null}
                <th scope="col" className="list-fill">
                  Name
                </th>
                {across && !projectId ? <th scope="col">Project</th> : null}
                <th scope="col">Format</th>
                <th scope="col" className="list-table-end">
                  Size
                </th>
                <th scope="col">Acquired</th>
                <th scope="col">Added</th>
                <th scope="col" className="list-table-end">
                  Used by
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((file) => (
                <FileRowView
                  key={file.id}
                  file={file}
                  formatLabel={media?.named(file.media_type) ?? file.media_type}
                  pick={pick}
                  projectName={
                    across && !projectId
                      ? (names.get(file.project?.id ?? "")?.name ?? file.project?.name ?? "")
                      : undefined
                  }
                />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {files.hasNextPage ? (
        <div className="actions">
          <Button
            busy={files.isFetchingNextPage}
            busyText="Loading…"
            onClick={() => void files.fetchNextPage()}
          >
            Show more
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function FileRowView({
  file,
  formatLabel,
  pick,
  projectName,
}: {
  file: FileRow;
  formatLabel: string;
  pick: Picking | undefined;
  projectName: string | undefined;
}) {
  const id = useId();
  const held = pick?.held.has(file.id) ?? false;
  const chosen = held || (pick?.chosen.has(file.id) ?? false);
  const full = Boolean(pick?.multiple && pick.room !== undefined && pick.chosen.size >= pick.room);
  const locked = held || (!chosen && full);
  return (
    <tr
      className={chosen ? "file-row-chosen" : undefined}
      onClick={
        pick
          ? (event) => {
              // The control itself, and links, do their own.
              if ((event.target as HTMLElement).closest("input, a, button, label")) return;
              if (locked) return;
              pick.onToggle(file, !chosen);
            }
          : undefined
      }
    >
      {pick ? (
        <td className="file-choose">
          <input
            id={id}
            type={pick.multiple ? "checkbox" : "radio"}
            name={pick.multiple ? undefined : "file-choice"}
            checked={chosen}
            disabled={locked}
            onChange={(event) => {
              pick.onToggle(file, event.target.checked);
            }}
          />
        </td>
      ) : null}
      <td className="list-fill" title={file.name}>
        <span className="list-fill-text">
          {pick ? (
            <label htmlFor={id} className="list-name file-name">
              {file.name}
              {held ? <span className="muted"> (chosen)</span> : null}
            </label>
          ) : (
            <Link className="list-name file-name" to="/files/$id/" params={{ id: file.id }}>
              {file.name}
            </Link>
          )}
        </span>
      </td>
      {projectName !== undefined ? <td className="muted">{projectName}</td> : null}
      <td className="muted" title={file.media_type}>
        {formatLabel}
      </td>
      <td className="list-table-end muted">{formatSize(file.size)}</td>
      <td className={file.acquired ? undefined : "muted"}>
        {file.acquired ? (
          <span title={`${acquiredText(file.acquired.at)}, ${file.acquired.source_label}`}>
            {file.acquired.on}
          </span>
        ) : (
          "Unknown"
        )}
      </td>
      <td className="muted">
        <RelativeTime value={file.created_at} />
        {file.created_by ? <span className="cell-meta">by {file.created_by.name}</span> : null}
      </td>
      <td className="list-table-end">
        {file.used_by.records || file.used_by.runs ? (
          pick ? (
            usedByText(file.used_by)
          ) : (
            <Link to="/files/$id/" params={{ id: file.id }} hash="used-by">
              {usedByText(file.used_by)}
            </Link>
          )
        ) : (
          <span className="muted">{usedByText(file.used_by)}</span>
        )}
      </td>
    </tr>
  );
}

/** The Run filter: a run's ID, as a run's page links here with it. */
function RunFilter({
  value,
  onChange,
}: {
  value: string | undefined;
  onChange: (run: string | undefined) => void;
}) {
  const id = useId();
  const [text, setText] = useState(value ?? "");
  const apply = () => {
    const run = text.trim().toUpperCase();
    if (run !== (value ?? "")) onChange(run || undefined);
  };
  return (
    <div className="field file-filter-run">
      <label htmlFor={id}>Run</label>
      <input
        id={id}
        type="text"
        value={text}
        placeholder="RUN-000123"
        spellCheck={false}
        autoComplete="off"
        aria-describedby={`${id}-help`}
        onChange={(event) => {
          setText(event.target.value);
        }}
        onBlur={apply}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            apply();
          }
        }}
      />
      <p className="field-help" id={`${id}-help`}>
        The files it read or made.
      </p>
    </div>
  );
}
