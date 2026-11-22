/**
 * Search everything (ADR 0176): the ⌘K palette. It finds records, requests, runs,
 * projects, saved views and people in every project the signed-in person can read,
 * grouped by kind, each with its project, and each group with a link to its full list.
 *
 * It's a combobox (WAI-ARIA's pattern): focus stays in the text box, and ↑ and ↓ move
 * the active option, which the box names with `aria-activedescendant`. ↵ opens it, ⌘↵
 * (Ctrl+↵) opens it in a new tab, and Esc closes the palette, putting focus back where
 * it was. The dialog keeps focus inside it while it's open.
 */

import { useQuery } from "@tanstack/react-query";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import { Dialog, Modal, ModalOverlay } from "react-aria-components";

import { swatchOf } from "./project-color";

import {
  bindSearchShortcut,
  DEBOUNCE_MS,
  KIND_LABELS,
  MIN_TEXT,
  RESULT_LABELS,
  resultHref,
  SEARCH_EVENT,
  searchQuery,
  shownTotal,
  type SearchAnswer,
  type SearchGroup,
  type SearchKind,
  type SearchResult,
} from "./search";
import { TypeDot } from "./type-color";

/** One row the arrows move through: a result, or a group's link to its full list. */
interface Row {
  id: string;
  href: string;
  /** What a screen reader hears for a group's link; a result reads its own text. */
  label?: string;
  result?: SearchResult;
}

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebounced(value);
    }, ms);
    return () => {
      window.clearTimeout(timer);
    };
  }, [value, ms]);
  return debounced;
}

function plural(n: number, one: string, many: string): string {
  return `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
}

/** Where it looked: the projects whose records its reader can read. */
function where(readable: number): string {
  if (readable === 0) return "You can read no project’s records yet";
  if (readable === 1) return "In the 1 project you can read";
  return `In all ${plural(readable, "project", "projects")} you can read`;
}

/** Whether the keys to show are a Mac's: ⌘ rather than Ctrl. */
function onMac(): boolean {
  return /Mac|iPhone|iPad/.test(navigator.userAgent);
}

/** Whether a click should be left to the browser: a new tab or window, or a download. */
function modified(event: MouseEvent): boolean {
  return event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;
}

export function SearchPalette({ navigate }: { navigate: (href: string) => void }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    bindSearchShortcut();
    const show = () => {
      setOpen(true);
    };
    window.addEventListener(SEARCH_EVENT, show);
    return () => {
      window.removeEventListener(SEARCH_EVENT, show);
    };
  }, []);
  return (
    <ModalOverlay
      className="dialog-overlay palette-overlay"
      isOpen={open}
      onOpenChange={setOpen}
      isDismissable
    >
      <Modal className="palette-modal">
        <Dialog className="palette-dialog" aria-label="Search everything">
          <Palette
            onOpen={(href) => {
              setOpen(false);
              navigate(href);
            }}
            onClose={() => {
              setOpen(false);
            }}
          />
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

function Palette({ onOpen, onClose }: { onOpen: (href: string) => void; onClose: () => void }) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const [text, setText] = useState("");
  const [kind, setKind] = useState<SearchKind | null>(null);
  // The active row, of the rows it was chosen in: new results start again at the first.
  const [chosen, setChosen] = useState<{ in: Row[] | null; index: number }>({
    in: null,
    index: 0,
  });
  const q = useDebounced(text.trim().replace(/\s+/g, " "), DEBOUNCE_MS);
  const answer = useQuery(searchQuery(q, kind));
  // The kinds' counts come from searching all of them: a kind chosen keeps them.
  const everyKind = useQuery({ ...searchQuery(q, null), enabled: kind !== null && q !== "" });
  const data: SearchAnswer | undefined = answer.data;
  const current = data?.q === q;

  // It opens to type into; ⌘K again, while it's open, starts over from what's typed.
  useEffect(() => {
    input.current?.focus();
    const again = () => {
      input.current?.focus();
      input.current?.select();
    };
    window.addEventListener(SEARCH_EVENT, again);
    return () => {
      window.removeEventListener(SEARCH_EVENT, again);
    };
  }, []);

  const groups = useMemo(() => (data?.groups ?? []).filter((g) => g.items.length > 0), [data]);
  const recent = useMemo(() => (q === "" ? (data?.recent ?? []) : []), [q, data]);
  const rows = useMemo(() => rowsOf(id, groups, recent), [id, groups, recent]);
  const active = chosen.in === rows ? Math.min(chosen.index, rows.length - 1) : 0;
  const setActive = useCallback(
    (index: number) => {
      setChosen({ in: rows, index });
    },
    [rows],
  );
  const activeRow = rows[active];
  useEffect(() => {
    if (!activeRow) return;
    document.getElementById(activeRow.id)?.scrollIntoView({ block: "nearest" });
  }, [activeRow]);

  const go = useCallback(
    (row: Row | undefined, newTab: boolean) => {
      if (!row) return;
      if (newTab) {
        window.open(row.href, "_blank", "noopener");
        return;
      }
      onOpen(row.href);
    },
    [onOpen],
  );

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!rows.length) return;
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActive((active + step + rows.length) % rows.length);
    } else if (event.key === "Enter") {
      event.preventDefault();
      go(activeRow, event.metaKey || event.ctrlKey);
    }
  }

  const counts = kind && everyKind.data ? everyKind.data.groups : (data?.groups ?? []);
  const all = counts.reduce((sum, g) => sum + (g.total ?? 0), 0);
  const allShown = `${all.toLocaleString("en-US")}${counts.some((g) => g.more_than_total) ? "+" : ""}`;
  const pills: { kind: SearchKind | null; label: string; n: number; shown: string }[] = [
    { kind: null, label: "All", n: all, shown: allShown },
    ...counts.map((g) => ({ kind: g.kind, label: g.label, n: g.total ?? 0, shown: shownTotal(g) })),
  ].filter((p) => p.kind === null || p.n > 0 || p.kind === kind);
  const listbox = `${id}-results`;
  const short = q !== "" && current && !data.searched_records && q.length < MIN_TEXT;
  const nothing = q !== "" && current && !answer.isFetching && rows.length === 0;
  const readable = data?.projects_readable;

  return (
    <>
      <div className="palette-box">
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" className="palette-icon">
          <circle cx="11" cy="11" r="7" />
          <path d="m20 20-3.5-3.5" />
        </svg>
        <label className="visually-hidden" htmlFor={`${id}-q`}>
          Search everything you can read
        </label>
        <input
          ref={input}
          id={`${id}-q`}
          role="combobox"
          aria-expanded={rows.length > 0}
          aria-controls={listbox}
          aria-autocomplete="list"
          aria-activedescendant={activeRow?.id}
          aria-describedby={`${id}-hint`}
          autoComplete="off"
          spellCheck={false}
          enterKeyHint="go"
          type="text"
          value={text}
          maxLength={200}
          placeholder="Search records, requests, runs, projects, people"
          onChange={(event) => {
            setText(event.target.value);
          }}
          onKeyDown={onKeyDown}
        />
        <button
          type="button"
          className="palette-esc kbd"
          onClick={onClose}
          aria-label="Esc: close the search"
        >
          Esc
        </button>
      </div>
      {q !== "" ? (
        <div className="palette-kinds">
          <div className="palette-pills" role="group" aria-label="Kind">
            {pills.map((p) => (
              <button
                key={p.kind ?? "all"}
                type="button"
                className="palette-pill"
                aria-pressed={kind === p.kind}
                onClick={() => {
                  setKind(p.kind);
                  input.current?.focus();
                }}
              >
                {p.label}
                <span className="palette-pill-count">{p.shown}</span>
              </button>
            ))}
          </div>
          {readable !== undefined ? <span className="palette-where">{where(readable)}</span> : null}
        </div>
      ) : null}
      <div
        className="palette-results"
        ref={list}
        role="listbox"
        id={listbox}
        aria-label={q ? `Results for ${q}` : "Recent"}
      >
        {recent.length ? (
          <ResultGroup
            id={`${id}-recent`}
            title="Recently changed or run by you"
            rows={rows}
            active={activeRow}
            setActive={setActive}
            onOpen={go}
          />
        ) : null}
        {groups.map((g) => (
          <ResultGroup
            key={g.kind}
            id={`${id}-${g.kind}`}
            title={KIND_LABELS[g.kind]}
            rows={rows}
            active={activeRow}
            setActive={setActive}
            onOpen={go}
          />
        ))}
      </div>
      <div className="palette-note" id={`${id}-hint`}>
        {q === "" && !recent.length && answer.isSuccess ? (
          <p>Type to search everything you can read.</p>
        ) : null}
        {short ? (
          <p>
            Type {MIN_TEXT} or more characters, or the start of an ID like PRT-0001, to search
            records, requests and runs.
          </p>
        ) : null}
        {nothing && !short ? <p>Nothing you can read matches “{q}”.</p> : null}
        {answer.isError ? <p>Search isn’t answering. Try again in a moment.</p> : null}
      </div>
      <div className="visually-hidden" role="status">
        {current && q !== "" && !answer.isFetching
          ? rows.length
            ? `${plural(rows.filter((r) => r.result).length, "result", "results")} shown`
            : `Nothing matches ${q}`
          : ""}
      </div>
      <div className="palette-footer">
        <span className="palette-keys" aria-hidden="true">
          <span>
            <kbd className="kbd">↑</kbd> <kbd className="kbd">↓</kbd> move
          </span>
          <span>
            <kbd className="kbd">↵</kbd> open
          </span>
          <span>
            <kbd className="kbd">{onMac() ? "⌘↵" : "Ctrl+↵"}</kbd> open in a new tab
          </span>
        </span>
        <span className="palette-promise">
          Only what you can read. Records in other projects never appear, not even as a count.
        </span>
      </div>
    </>
  );
}

/** The rows the arrows move through, in the order they're shown. */
function rowsOf(id: string, groups: SearchGroup[], recent: SearchResult[]): Row[] {
  const rows: Row[] = recent.map((r) => ({
    id: `${id}-recent-${r.kind}-${r.id}`,
    href: resultHref(r),
    result: r,
  }));
  for (const g of groups) {
    for (const r of g.items) {
      rows.push({ id: `${id}-${g.kind}-${r.id}`, href: resultHref(r), result: r });
    }
    const shown = g.items.length;
    if (g.href && g.total !== null && (g.total > shown || g.more_than_total)) {
      rows.push({
        id: `${id}-${g.kind}-all`,
        href: g.href,
        label: `All ${shownTotal(g)} in ${KIND_LABELS[g.kind]}`,
      });
    }
  }
  return rows;
}

function ResultGroup({
  id,
  title,
  rows,
  active,
  setActive,
  onOpen,
}: {
  id: string;
  title: string;
  rows: Row[];
  active: Row | undefined;
  setActive: (index: number) => void;
  onOpen: (row: Row, newTab: boolean) => void;
}) {
  const mine = rows.filter((r) => r.id.startsWith(`${id}-`));
  if (!mine.length) return null;
  return (
    <div role="group" aria-labelledby={`${id}-title`} className="palette-group">
      <div role="presentation" id={`${id}-title`} className="palette-group-title">
        {title}
      </div>
      {mine.map((row) => (
        <a
          key={row.id}
          id={row.id}
          role="option"
          aria-selected={row === active}
          tabIndex={-1}
          href={row.href}
          className={row.result ? "palette-option" : "palette-option palette-all"}
          onMouseMove={() => {
            if (row !== active) setActive(rows.indexOf(row));
          }}
          onClick={(event) => {
            if (modified(event)) return;
            event.preventDefault();
            onOpen(row, false);
          }}
        >
          {row.result ? <ResultRow result={row.result} active={row === active} /> : row.label}
          {!row.result ? <span aria-hidden="true"> →</span> : null}
        </a>
      ))}
    </div>
  );
}

function ResultRow({ result, active }: { result: SearchResult; active: boolean }) {
  const record = result.kind === "entity" || result.kind === "request";
  const [first, ...others] = result.projects;
  return (
    <>
      {record && result.type ? (
        <TypeDot type={result.type} className="palette-dot" />
      ) : (
        <span
          className={record ? "palette-dot" : "palette-dot palette-dot-round"}
          aria-hidden="true"
        />
      )}
      {result.hrid ? <span className="palette-hrid">{result.hrid}</span> : null}
      <span className="palette-title">{result.title}</span>
      <span className="palette-meta">
        <span className="palette-detail">
          {record || result.kind === "run"
            ? result.detail
            : `${RESULT_LABELS[result.kind]} · ${result.detail}`}
        </span>
        {first ? (
          <span className="palette-project">
            <span
              className="swatch palette-project-mark"
              data-swatch={swatchOf(first.id)}
              aria-hidden="true"
            />
            {first.name}
            {others.length ? <span className="palette-project-more"> +{others.length}</span> : null}
            {others.length ? (
              <span className="visually-hidden">
                {`, and ${others.map((p) => p.name).join(", ")}`}
              </span>
            ) : null}
          </span>
        ) : null}
        {active ? (
          <kbd className="kbd palette-enter" aria-hidden="true">
            ↵
          </kbd>
        ) : null}
      </span>
    </>
  );
}
