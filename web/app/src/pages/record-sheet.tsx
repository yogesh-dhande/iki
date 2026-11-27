/**
 * A type's records in a project, edited as a sheet (ADR 0228): typed into, pasted from
 * Excel or Sheets, filled down, with new rows added at the top, and nothing written until
 * Save. Each changed row is checked by the server as it would be saved, and its problems
 * shown at their cells; Save shows every change, takes a reason, and writes all of them or
 * none. The logic of cells, pastes and fills is in sheet.ts.
 *
 * It's an ARIA grid: one tab stop, the active cell focused and moved by the arrow keys,
 * Home, End and the page keys; Shift extends the selection. Enter or F2 edits a cell, as
 * does typing; Escape puts it back.
 */

import type { Problem } from "@ikisci/sdk";
import { ApiError } from "@ikisci/sdk";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type ClipboardEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { iki, problemOf, signInAgain, type Project } from "../api";
import {
  ENTITIES,
  inSentence,
  typesQuery,
  valueText,
  type Entity,
  type EntityType,
} from "../entities";
import { listOfItems } from "../lab-lists";
import {
  AMOUNT,
  baseText,
  changed,
  checkRow,
  checkRows,
  clipboardBlock,
  did,
  EMPTY,
  fillDown,
  ID,
  inRange,
  isNew,
  LOCATION,
  moved,
  placePaste,
  problemsAt,
  rangeOf,
  redo,
  SAVE_ROWS,
  saveRow,
  saveRows,
  sheetColumns,
  templateUrl,
  textAt,
  toTsv,
  undo,
  version,
  whyLocked,
  withNewRows,
  withoutNewRows,
  written,
  type Checked,
  type CheckRow,
  type History,
  type Point,
  type Saved,
  type SheetColumn,
  type SheetLocale,
  type SheetRow,
  type Write,
} from "../sheet";
import { DATE_ORDERS, DECIMALS, plural } from "../spreadsheets";
import { ListCell } from "./list-cell";
import {
  Button,
  buttonClass,
  CheckboxField,
  ConfirmDialog,
  SelectField,
  TextField,
  useMessages,
} from "../ui";

/** What's become of the lab list options a record's cell names, if no dropdown offers
 * them any more (ADR 0270): "archived", "superseded", or for a list, how many. */
function retiredIn(column: SheetColumn, entity: Entity): string {
  const field = column.field;
  if (!field) return "";
  const value = entity.values[field.name];
  const ids = Array.isArray(value) ? value : value ? [value] : [];
  const marks = ids.flatMap((id) => {
    const ref = typeof id === "string" ? entity.refs[id] : undefined;
    return ref?.code && (ref.superseded || ref.archived)
      ? [ref.superseded ? "superseded" : "archived"]
      : [];
  });
  if (!marks.length) return "";
  if (!Array.isArray(value)) return marks[0] ?? "";
  return `${String(marks.length)} archived or superseded`;
}

/** A row's height, as the stylesheet draws it (`.record-sheet-row`). */
const ROW_HEIGHT = 34;
/** Fetch more records when this many rows remain below the view. */
const AHEAD = 30;
/** How long after the last change a check is sent. */
const SETTLE_MS = 350;

/** A check's answer for a row, and what it answered. */
interface Answer {
  version: string;
  sent: CheckRow;
  checked: Checked;
}

/** The browser's time zone: a date and time a cell gives without one is in it. */
function browserZone(): string | null {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    return null;
  }
}

/** About how wide `text` is in the sheet's 13px Inter, in pixels. */
function textWidth(text: string): number {
  let width = 0;
  for (const c of text) width += c === " " ? 3.6 : /[A-Z0-9%]/.test(c) ? 8.4 : 7;
  return width;
}

/** Each column's width in pixels at the stylesheet's 13px rem: as wide as its heading or
 * most of its first rows' text, within bounds. */
function widthsOf(columns: readonly SheetColumn[], rows: readonly SheetRow[]): number[] {
  return columns.map((c) => {
    const heading = textWidth(c.label) * 0.93 + 22;
    const texts = rows
      .slice(0, 100)
      .map((r) => textWidth(baseText(r, c)))
      .sort((a, b) => a - b);
    const most = texts.length ? (texts[Math.ceil((texts.length - 1) * 0.9)] ?? 0) : 0;
    const least = c.key === ID ? 104 : c.key === LOCATION ? 150 : 80;
    return Math.round(Math.min(Math.max(heading, most + 22, least), 280));
  });
}

const px = (n: number) => `calc(${String(n)}rem / 13)`;

export function RecordSheet({
  type,
  project,
  records,
  more,
  onClose,
}: {
  type: EntityType;
  project: Project;
  /** The records the table has loaded, in its order. */
  records: readonly Entity[];
  more: { hasNextPage: boolean; isFetchingNextPage: boolean; fetchNextPage: () => unknown };
  /** Done editing: with what was saved, if it was. */
  onClose: (saved?: Saved) => void;
}) {
  const client = useQueryClient();
  const { say } = useMessages();
  const captionId = useId();
  const statusId = useId();
  const activeNoteId = useId();
  const columns = sheetColumns(type);
  const [history, setHistory] = useState<History>({ past: [], present: EMPTY, future: [] });
  const state = history.present;
  // Each record as it was when its first cell changed: what's saved is from its revision.
  const [bases, setBases] = useState<Record<string, Entity>>({});
  const rows: SheetRow[] = [
    ...state.added.map((key) => ({ key, number: Number(key.slice(4)) })),
    ...records.map((e) => ({ key: e.id, entity: bases[e.id] ?? e })),
  ];
  const [anchor, setAnchor] = useState<Point>({ row: 0, col: 1 });
  const [active, setActive] = useState<Point>({ row: 0, col: 1 });
  const selection = rangeOf(anchor, active);
  const [editing, setEditing] = useState<{ text: string; at: Point; typed?: boolean } | null>(null);
  const types = useQuery(typesQuery).data ?? [];
  /** The lab list (ADR 0270) a column's cells name options of, if they do. */
  const listAt = (column: SheetColumn | undefined) =>
    column?.field && !column.readOnly ? listOfItems(column.field, types) : undefined;
  const [note, setNote] = useState("");
  const [locale, setLocale] = useState<SheetLocale>(() => ({
    decimal: null,
    dates: null,
    serial_dates: false,
    time_zone: browserZone(),
  }));
  const [answers, setAnswers] = useState<Record<string, Answer>>({});
  const [checkProblem, setCheckProblem] = useState<Problem>();
  // Asking again for a check that couldn't be made: each ask, one more.
  const [asks, setAsks] = useState(0);
  const [reviewing, setReviewing] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const [reason, setReason] = useState("");
  const [holdReason, setHoldReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveProblem, setSaveProblem] = useState<Problem>();
  const [stale, setStale] = useState<{ key: string; hrid: string }[]>([]);
  const grid = useRef<HTMLDivElement>(null);
  const committed = useRef<typeof editing>(null);
  const shownAt = useRef("");
  const scroller = useRef<HTMLDivElement>(null);

  const label = inSentence(type.label);
  const many = inSentence(type.label_plural);
  const rowName = (row: SheetRow | undefined) =>
    row?.entity?.hrid ?? `New ${label} ${String(row?.number ?? "")}`;
  const locked = (row: number, col: number): string | null => {
    const r = rows[row];
    const c = columns[col];
    return r && c ? whyLocked(r, c, type, project.name) : "";
  };

  // What's changed, and what each changed row sends to be checked.
  const sending = new Map<string, CheckRow>();
  for (const row of rows) {
    if (!state.edits[row.key]) continue;
    const sent = checkRow(state, row, columns);
    if (sent) sending.set(row.key, sent);
  }
  const current = (key: string): Answer | undefined => {
    const sent = sending.get(key);
    const found = answers[key];
    return sent && found?.version === version(sent, locale) ? found : undefined;
  };
  const waiting = [...sending.entries()].filter(([key]) => !current(key));
  const waitingKey = JSON.stringify(waiting.map(([, sent]) => version(sent, locale)));

  // Checks what's changed, once typing settles: only rows whose cells or locale changed
  // since their last check. An answer to something since changed is set aside.
  useEffect(() => {
    if (!waiting.length) return;
    const sent = waiting.map(([, s]) => s);
    const said = locale;
    const timer = window.setTimeout(() => {
      checkRows(project.id, type.name, sent, said)
        .then((found) => {
          setCheckProblem(undefined);
          setAnswers((prev) => {
            const next = { ...prev };
            found.forEach((checked, i) => {
              const s = sent[i];
              if (s) next[s.key] = { version: version(s, said), sent: s, checked };
            });
            return next;
          });
        })
        .catch((error: unknown) => {
          if (error instanceof ApiError && error.status === 401) signInAgain();
          setCheckProblem(problemOf(error));
        });
    }, SETTLE_MS);
    return () => {
      window.clearTimeout(timer);
    };
    // The rows waiting are what `waitingKey` says.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [waitingKey, asks, project.id, type.name]);

  // Leaving the page with changes unsaved asks first, as the browser does.
  const dirty = sending.size > 0;
  useEffect(() => {
    if (!dirty) return;
    const ask = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", ask);
    return () => {
      window.removeEventListener("beforeunload", ask);
    };
  }, [dirty]);

  // The rows checked, their problems, and what saving them does.
  const checkedRows = [...sending.keys()].flatMap((key) => {
    const found = current(key);
    return found ? [found] : [];
  });
  const problems = rows.flatMap((row, index) => {
    const found = current(row.key);
    if (!found) return [];
    return found.checked.problems.map((p) => ({ row: index, key: row.key, problem: p }));
  });
  const ambiguous = problems.filter((p) => p.problem.kind === "ambiguous").length;
  const toSave = checkedRows.flatMap((a) => {
    const row = saveRow(a.sent, a.checked);
    return row ? [{ answer: a, row }] : [];
  });
  const held = checkedRows.filter((a) => a.checked.held.length > 0);
  const checking = waiting.length > 0;
  const edited = toSave.filter((s) => s.answer.sent.entity).length;
  const created = toSave.length - edited;
  const canSave = !checking && !problems.length && toSave.length > 0 && !checkProblem;

  // --- Changing the sheet ------------------------------------------------------------

  const change = (next: typeof state) => {
    // Each record's revision is the one it had when it first changed.
    const newBases = { ...bases };
    let grew = false;
    for (const key of Object.keys(next.edits)) {
      const record = records.find((e) => e.id === key);
      if (record && !newBases[key]) {
        newBases[key] = record;
        grew = true;
      }
    }
    if (grew) setBases(newBases);
    setHistory((h) => did(h, next));
  };

  /** Makes `writes`, but to cells that can't change; says how many were left as they are. */
  const write = (writes: readonly Write[], into = state, among = rows, verb = "changed") => {
    const allowed = writes.filter((w) => {
      const r = among[w.row];
      const c = columns[w.col];
      return r && c && !whyLocked(r, c, type, project.name);
    });
    const skipped = writes.flatMap((w) => {
      const r = among[w.row];
      const c = columns[w.col];
      if (!r || !c || allowed.includes(w) || w.text === textAt(into, r, c)) return [];
      return [whyLocked(r, c, type, project.name) ?? ""];
    });
    change(written(into, allowed, among, columns));
    if (skipped.length) {
      const why = skipped[0] ?? "";
      setNote(`${plural(skipped.length, "cell wasn’t", "cells weren’t")} ${verb}: ${why}`);
    } else {
      setNote("");
    }
  };

  const focusGrid = () => {
    grid.current?.focus({ preventScroll: true });
  };

  const go = (to: Point, extend = false) => {
    setActive(to);
    if (!extend) setAnchor(to);
    virtualizer.scrollToIndex(to.row, { align: "auto" });
  };

  const startEditing = (text?: string) => {
    const why = locked(active.row, active.col);
    if (why !== null) {
      setNote(
        `${rowName(rows[active.row])}, ${columns[active.col]?.label ?? ""}: ${why || "it can’t change."}`,
      );
      return;
    }
    const r = rows[active.row];
    const c = columns[active.col];
    if (!r || !c) return;
    setEditing({ at: active, text: text ?? textAt(state, r, c), typed: text !== undefined });
  };

  /** Done editing, with what was typed, or `text`: a lab list's option chosen. */
  const commit = (move?: Point, text?: string) => {
    // Once: the input's blur, as it goes, doesn't commit what Enter or Tab did.
    if (!editing || committed.current === editing) return;
    committed.current = editing;
    write([{ row: editing.at.row, col: editing.at.col, text: text ?? editing.text }]);
    setEditing(null);
    if (move) go(move);
    focusGrid();
  };

  const addRows = (count = 1) => {
    const next = withNewRows(state, count);
    change(next);
    go({ row: next.added.length - 1, col: 1 });
    focusGrid();
  };

  const removeNewRows = () => {
    const keys = new Set<string>();
    for (let row = selection.top; row <= selection.bottom; row++) {
      const key = rows[row]?.key;
      if (key && isNew(key)) keys.add(key);
    }
    if (!keys.size) return;
    change(withoutNewRows(state, keys));
    go({ row: Math.max(0, selection.top - 1), col: active.col });
  };

  const paste = (event: ClipboardEvent<HTMLDivElement>) => {
    if (editing) return;
    event.preventDefault();
    const block = clipboardBlock(
      event.clipboardData.getData("text/plain"),
      event.clipboardData.getData("text/html"),
    );
    if (!block.length) return;
    let into = state;
    let among = rows;
    // A paste into the new rows stays in them, adding as many as it needs.
    const inNew = selection.top < state.added.length;
    if (inNew) {
      const short = selection.top + block.length - state.added.length;
      if (short > 0) {
        into = withNewRows(state, short);
        among = [
          ...into.added.map((key) => ({ key, number: Number(key.slice(4)) })),
          ...rows.slice(state.added.length),
        ];
      }
    }
    const size = { rows: inNew ? into.added.length : among.length, cols: columns.length };
    const placed = placePaste(block, selection, size);
    if (placed.colsPast) {
      setNote(
        `Nothing was pasted: it’s ${plural(placed.colsPast, "column", "columns")} wider than the sheet from here.`,
      );
      return;
    }
    if (placed.rowsPast) {
      setNote(
        `Nothing was pasted: it has ${plural(placed.rowsPast, "row", "rows")} more than the ${many} below${more.hasNextPage ? " loaded so far: scroll down to load more" : ""}. To add ${many}, add a row and paste into it.`,
      );
      return;
    }
    write(placed.writes, into, among, "pasted");
    const height = Math.max(block.length, selection.bottom - selection.top + 1);
    const width = Math.max(block[0]?.length ?? 1, selection.right - selection.left + 1);
    setAnchor({ row: selection.top, col: selection.left });
    setActive({
      row: Math.min(selection.top + height - 1, size.rows - 1),
      col: Math.min(selection.left + width - 1, columns.length - 1),
    });
  };

  const copy = (event: ClipboardEvent<HTMLDivElement>, cut = false) => {
    if (editing) return;
    event.preventDefault();
    const block: string[][] = [];
    for (let row = selection.top; row <= selection.bottom; row++) {
      const r = rows[row];
      if (!r) continue;
      const line: string[] = [];
      for (let col = selection.left; col <= selection.right; col++) {
        const c = columns[col];
        line.push(c ? textAt(state, r, c) : "");
      }
      block.push(line);
    }
    event.clipboardData.setData("text/plain", toTsv(block));
    if (cut) clear();
  };

  const clear = () => {
    const writes: Write[] = [];
    for (let row = selection.top; row <= selection.bottom; row++) {
      for (let col = selection.left; col <= selection.right; col++)
        writes.push({ row, col, text: "" });
    }
    write(writes, state, rows, "emptied");
  };

  const keyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (editing) return;
    const ctrl = event.ctrlKey || event.metaKey;
    const key = event.key;
    if (ctrl && key.toLowerCase() === "z") {
      event.preventDefault();
      setHistory((h) => (event.shiftKey ? redo(h) : undo(h)));
      return;
    }
    if (ctrl && key.toLowerCase() === "y") {
      event.preventDefault();
      setHistory(redo);
      return;
    }
    if (ctrl && key.toLowerCase() === "d") {
      event.preventDefault();
      write(
        fillDown(selection, (row, col) => {
          const r = rows[row];
          const c = columns[col];
          return r && c ? textAt(state, r, c) : "";
        }),
        state,
        rows,
        "filled",
      );
      return;
    }
    if (ctrl && key.toLowerCase() === "a") {
      event.preventDefault();
      setAnchor({ row: 0, col: 0 });
      setActive({ row: rows.length - 1, col: columns.length - 1 });
      return;
    }
    if (event.altKey && key === "ArrowDown" && listAt(columns[active.col])) {
      // A lab list's cell opens its options, as a spreadsheet's dropdown cell does.
      event.preventDefault();
      startEditing();
      return;
    }
    const page = Math.max(1, Math.floor((scroller.current?.clientHeight ?? 340) / ROW_HEIGHT) - 1);
    const to = moved(active, key, { ctrl, rows: rows.length, cols: columns.length, page });
    if (to) {
      event.preventDefault();
      go(to, event.shiftKey);
      return;
    }
    if (key === "Tab") {
      // Tab moves along the row, as a spreadsheet's does; at its end, out of the sheet.
      const col = active.col + (event.shiftKey ? -1 : 1);
      if (col >= 0 && col < columns.length) {
        event.preventDefault();
        go({ row: active.row, col });
      }
      return;
    }
    if (key === "Enter" || key === "F2") {
      event.preventDefault();
      startEditing();
      return;
    }
    if (key === "Delete" || key === "Backspace") {
      event.preventDefault();
      clear();
      return;
    }
    if (key.length === 1 && !ctrl && !event.altKey) {
      event.preventDefault();
      startEditing(key);
    }
  };

  // --- Rows as they scroll -------------------------------------------------------------

  // TanStack Virtual hands back functions that read its live state, so the React Compiler
  // leaves this component unmemoized; it re-renders as the sheet scrolls, as it must.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 10,
  });
  const visible = virtualizer.getVirtualItems();
  const last = visible.at(-1)?.index ?? 0;
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = more;
  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage && last >= rows.length - AHEAD) void fetchNextPage();
  }, [last, rows.length, hasNextPage, isFetchingNextPage, fetchNextPage]);
  // The active row is always drawn, so the cell with focus is never taken away.
  const drawn =
    visible.some((v) => v.index === active.row) || !rows[active.row]
      ? visible.map((v) => ({ index: v.index, start: v.start }))
      : [
          ...visible.map((v) => ({ index: v.index, start: v.start })),
          { index: active.row, start: active.row * ROW_HEIGHT },
        ];

  // Focus follows the active cell, while focus is in the sheet.
  useEffect(() => {
    if (editing || !grid.current?.contains(document.activeElement)) return;
    const cell = grid.current.querySelector<HTMLElement>(
      `[data-cell="${String(active.row)}:${String(active.col)}"]`,
    );
    if (cell && document.activeElement !== cell) cell.focus({ preventScroll: true });
    // Into view as it becomes the active cell, clear of the sticky ID column and headings.
    const at = `${String(active.row)}:${String(active.col)}`;
    if (cell && shownAt.current !== at) {
      shownAt.current = at;
      cell.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  });

  const widths = widthsOf(columns, rows);
  const template = widths.map((w) => px(w)).join(" ");
  const minWidth = px(widths.reduce((a, b) => a + b, 0));

  // --- Saving --------------------------------------------------------------------------

  const save = async () => {
    setSaving(true);
    setSaveProblem(undefined);
    try {
      const saved = await saveRows(project.id, {
        type: type.name,
        reason: reason.trim(),
        hold_reason: held.length ? holdReason.trim() : null,
        locale,
        rows: toSave.map((s) => s.row),
      });
      await client.invalidateQueries({ queryKey: ENTITIES });
      say(
        [
          saved.edited ? plural(saved.edited, `${label} changed`, `${many} changed`) : "",
          saved.created ? plural(saved.created, `${label} added`, `${many} added`) : "",
        ]
          .filter(Boolean)
          .join(", ") + ".",
      );
      setReviewing(false);
      onClose(saved);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) signInAgain();
      const problem = problemOf(error);
      setSaveProblem(problem);
      if (error instanceof ApiError && error.status === 412) {
        const found = (problem as Problem & { rows?: { key: string; hrid: string }[] }).rows ?? [];
        setStale(found);
      } else if (error instanceof ApiError && error.status === 422) {
        // Something changed since the check: check every row again.
        setAnswers({});
      }
    } finally {
      setSaving(false);
    }
  };

  /** Brings in the latest of records changed since they were read, keeping what was typed
   * in them: it's checked again against what they are now. */
  const bringInLatest = async () => {
    setSaving(true);
    try {
      const latest = await Promise.all(
        stale.map(async (s) => {
          const { data } = await iki.GET("/api/v1/entities/{ref}", {
            params: { path: { ref: s.key }, query: { project: project.id } },
          });
          return data;
        }),
      );
      setBases((prev) => {
        const next = { ...prev };
        for (const e of latest) if (e) next[e.id] = e;
        return next;
      });
      setStale([]);
      setSaveProblem(undefined);
      setReviewing(false);
      setNote(
        `Brought in the latest of ${plural(latest.length, "record", "records")}: your changes to them are checked again.`,
      );
    } catch (error) {
      setSaveProblem(problemOf(error));
    } finally {
      setSaving(false);
    }
  };

  // --- Drawing it ----------------------------------------------------------------------

  const activeRow = rows[active.row];
  const activeColumn = columns[active.col];
  const activeProblems =
    activeRow && activeColumn ? problemsAt(current(activeRow.key)?.checked, activeColumn) : [];
  const activeWhy = locked(active.row, active.col);
  const activeNote =
    activeRow && activeColumn
      ? `${rowName(activeRow)}, ${activeColumn.label}${
          activeProblems.length
            ? `: ${activeProblems.map((p) => p.detail).join(" ")}`
            : activeWhy
              ? `: can’t change. ${activeWhy}`
              : ""
        }`
      : "";

  const cell = (row: SheetRow, index: number, column: SheetColumn, col: number): ReactNode => {
    const at = `${String(index)}:${String(col)}`;
    const isActive = active.row === index && active.col === col;
    const why = whyLocked(row, column, type, project.name);
    const found = problemsAt(current(row.key)?.checked, column);
    const ambiguousHere = found.length > 0 && found.every((p) => p.kind === "ambiguous");
    // A field a sheet can't change reads as the table shows it: a state's label, say.
    const text =
      column.readOnly && column.field && row.entity
        ? valueText(column.field, row.entity.values[column.field.name], row.entity)
        : textAt(state, row, column);
    const isChanged = isNew(row.key)
      ? Boolean(state.edits[row.key]?.[column.key])
      : changed(state, row, column);
    const classes = [
      "record-sheet-cell",
      column.key === ID ? "is-id" : "",
      why !== null ? "is-readonly" : "",
      isChanged ? "is-changed" : "",
      found.length ? (ambiguousHere ? "is-ambiguous" : "is-invalid") : "",
      inRange(selection, index, col) ? "is-selected" : "",
      isActive ? "is-active" : "",
    ]
      .filter(Boolean)
      .join(" ");
    const isEditing = editing?.at.row === index && editing.at.col === col;
    const list = listAt(column);
    // An option no dropdown offers any more, which the record keeps (ADR 0270): marked.
    const retired = list && row.entity ? retiredIn(column, row.entity) : "";
    return (
      <div
        role="gridcell"
        key={column.key}
        data-cell={at}
        aria-colindex={col + 1}
        aria-selected={inRange(selection, index, col)}
        aria-readonly={why !== null || undefined}
        aria-invalid={found.length ? true : undefined}
        aria-describedby={isActive ? activeNoteId : undefined}
        tabIndex={isActive && !isEditing ? 0 : -1}
        className={classes}
        title={
          found.length
            ? found.map((p) => p.detail).join(" ")
            : (why ?? (text.length > 30 ? text : undefined))
        }
        onMouseDown={(event) => {
          if (isEditing) return;
          if (editing) commit();
          event.preventDefault();
          const to = { row: index, col };
          setActive(to);
          if (!event.shiftKey) setAnchor(to);
          (event.currentTarget as HTMLElement).focus({ preventScroll: true });
        }}
        onMouseEnter={(event) => {
          if (event.buttons === 1 && !editing) setActive({ row: index, col });
        }}
        onDoubleClick={() => {
          startEditing();
        }}
      >
        {isEditing && list ? (
          <ListCell
            label={`${column.label}, ${rowName(row)}`}
            list={list}
            projectId={project.id}
            multiple={column.field?.type === "list"}
            text={editing.text}
            typedFirst={editing.typed ?? false}
            onText={(next) => {
              setEditing({ ...editing, text: next });
            }}
            onCommit={(next, leave) => {
              const r = editing.at.row;
              const c = editing.at.col;
              const to =
                leave === "down" || leave === "up"
                  ? {
                      row: Math.max(0, Math.min(rows.length - 1, r + (leave === "up" ? -1 : 1))),
                      col: c,
                    }
                  : leave === "right" || leave === "left"
                    ? {
                        row: r,
                        col: Math.max(
                          0,
                          Math.min(columns.length - 1, c + (leave === "left" ? -1 : 1)),
                        ),
                      }
                    : undefined;
              commit(to, next);
            }}
            onCancel={() => {
              setEditing(null);
              focusGrid();
            }}
          />
        ) : isEditing ? (
          <input
            className="record-sheet-input"
            aria-label={`${column.label}, ${rowName(row)}`}
            ref={(node) => {
              // The cell being typed in has focus, its text's end the caret.
              if (node && document.activeElement !== node) {
                node.focus({ preventScroll: true });
                node.setSelectionRange(node.value.length, node.value.length);
              }
            }}
            value={editing.text}
            onChange={(event) => {
              setEditing({ at: editing.at, text: event.target.value });
            }}
            onBlur={() => {
              commit();
            }}
            onKeyDown={(event) => {
              const r = editing.at.row;
              const c = editing.at.col;
              if (event.key === "Enter") {
                event.preventDefault();
                commit({ row: Math.min(rows.length - 1, r + (event.shiftKey ? -1 : 1)), col: c });
              } else if (event.key === "Tab") {
                event.preventDefault();
                commit({
                  row: r,
                  col: Math.max(0, Math.min(columns.length - 1, c + (event.shiftKey ? -1 : 1))),
                });
              } else if (event.key === "Escape") {
                event.preventDefault();
                setEditing(null);
                focusGrid();
              }
            }}
          />
        ) : (
          <span className="record-sheet-text">
            {text}
            {retired && !isChanged ? <span className="muted"> ({retired})</span> : null}
          </span>
        )}
      </div>
    );
  };

  const changes = toSave.map(({ answer: a }) => {
    const row = rows.find((r) => r.key === a.sent.key);
    const shown = a.checked.shown;
    const fields = Object.keys(a.checked.values).map((name) => {
      const column = columns.find((c) => c.key === name);
      return {
        name,
        label: column?.label ?? name,
        was: row && column && row.entity ? baseText(row, column) : "",
        now: shown[name] ?? "",
      };
    });
    return { row, checked: a.checked, fields };
  });

  return (
    <section className="record-sheet" aria-labelledby={captionId}>
      <div className="record-sheet-bar">
        <h2 className="record-sheet-title" id={captionId}>
          Editing {many} as a sheet
        </h2>
        <div className="record-sheet-actions">
          {type.abstract ? null : (
            <Button
              onClick={() => {
                addRows();
              }}
            >
              Add row
            </Button>
          )}
          <Button
            variant="quiet"
            disabled={!history.past.length}
            onClick={() => {
              setHistory(undo);
            }}
          >
            Undo
          </Button>
          <Button
            variant="quiet"
            disabled={!history.future.length}
            onClick={() => {
              setHistory(redo);
            }}
          >
            Redo
          </Button>
          {state.added.length ? (
            <Button
              variant="quiet"
              disabled={!rows.slice(selection.top, selection.bottom + 1).some((r) => isNew(r.key))}
              onClick={removeNewRows}
            >
              Remove new rows
            </Button>
          ) : null}
          <a className={buttonClass("quiet")} href={templateUrl(type.name)} download>
            Import template
          </a>
          <Button
            variant="quiet"
            onClick={() => {
              if (dirty) setLeaving(true);
              else onClose();
            }}
          >
            {dirty ? "Discard changes" : "Done"}
          </Button>
          <Button
            variant="primary"
            disabled={!canSave}
            onClick={() => {
              setSaveProblem(undefined);
              setReviewing(true);
            }}
          >
            Review and save
          </Button>
        </div>
      </div>
      <div
        className="record-sheet-locale"
        role="group"
        aria-label="How the sheet writes numbers and dates"
      >
        <SelectField
          label="Decimal mark"
          value={locale.decimal ?? ""}
          options={DECIMALS}
          onChange={(value) => {
            setLocale({ ...locale, decimal: value || null });
          }}
        />
        <SelectField
          label="Dates written as numbers"
          value={locale.dates ?? ""}
          options={DATE_ORDERS}
          onChange={(value) => {
            setLocale({ ...locale, dates: value || null });
          }}
        />
        <CheckboxField
          label="Plain numbers in date columns are Excel serial dates"
          checked={locale.serial_dates}
          onChange={(checked) => {
            setLocale({ ...locale, serial_dates: checked });
          }}
        />
      </div>
      <p className="muted record-sheet-help">
        Type, paste from a spreadsheet or fill down (Ctrl+D): a paste fills right and down from the
        selected cell, in the order it was copied. Each changed row is checked as it would be saved;
        nothing is saved until you review it. Iki doesn’t guess how numbers and dates are written:
        until you say, a cell such as 1,234 or 03/04/2027 is flagged.
        {locale.time_zone ? ` Times without a zone are in ${locale.time_zone}.` : ""}
      </p>
      <p className="record-sheet-status" id={statusId} role="status">
        {[
          sending.size ? plural(sending.size, "row changed", "rows changed") : "No changes yet",
          checking ? "checking…" : "",
          problems.length
            ? `${plural(problems.length, "problem", "problems")}${ambiguous ? ` (${String(ambiguous)} reading two ways)` : ""}`
            : "",
          note,
        ]
          .filter(Boolean)
          .join(" · ")}
      </p>
      {checkProblem ? (
        <div className="alert alert-error" role="alert">
          <p>The changes couldn’t be checked: {checkProblem.detail}</p>
          <Button
            onClick={() => {
              setAsks((n) => n + 1);
            }}
          >
            Check again
          </Button>
        </div>
      ) : null}
      <div
        className="record-sheet-scroll"
        ref={scroller}
        style={{ scrollPaddingLeft: px(widths[0] ?? 0), scrollPaddingTop: px(32) }}
      >
        <div
          ref={grid}
          role="grid"
          tabIndex={rows.length ? -1 : 0}
          className="record-sheet-grid"
          aria-labelledby={captionId}
          aria-describedby={statusId}
          aria-multiselectable="true"
          aria-rowcount={hasNextPage ? -1 : rows.length + 1}
          aria-colcount={columns.length}
          onKeyDown={keyDown}
          onPaste={paste}
          onCopy={(event) => {
            copy(event);
          }}
          onCut={(event) => {
            copy(event, true);
          }}
          style={{ minWidth }}
        >
          <div role="rowgroup" className="record-sheet-head">
            <div
              role="row"
              aria-rowindex={1}
              className="record-sheet-row"
              style={{ gridTemplateColumns: template }}
            >
              {columns.map((c, col) => (
                <div
                  role="columnheader"
                  key={c.key}
                  aria-colindex={col + 1}
                  className={[
                    "record-sheet-cell",
                    c.key === ID ? "is-id" : "",
                    c.readOnly || c.newOnly ? "is-readonly" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  title={c.readOnly ?? (c.newOnly ? `New ${many} only` : undefined)}
                >
                  {c.label}
                </div>
              ))}
            </div>
          </div>
          <div
            role="rowgroup"
            className="record-sheet-body"
            style={{ height: px(virtualizer.getTotalSize()) }}
          >
            {drawn.map(({ index, start }) => {
              const row = rows[index];
              if (!row) return null;
              const isNewRow = isNew(row.key);
              return (
                <div
                  role="row"
                  key={row.key}
                  aria-rowindex={index + 2}
                  className={isNewRow ? "record-sheet-row is-new" : "record-sheet-row"}
                  style={{ gridTemplateColumns: template, transform: `translateY(${px(start)})` }}
                >
                  {columns.map((c, col) => cell(row, index, c, col))}
                </div>
              );
            })}
          </div>
        </div>
      </div>
      <p className="visually-hidden" id={activeNoteId} aria-live="polite">
        {activeNote}
      </p>
      {rows.length === 0 ? (
        <p className="empty">No {many} here yet. Add a row to start, or paste into a new row.</p>
      ) : null}
      {problems.length ? (
        <section className="record-sheet-problems" aria-labelledby={`${captionId}-problems`}>
          <h3 className="section-title" id={`${captionId}-problems`}>
            {plural(problems.length, "problem", "problems")} to put right
          </h3>
          <ul>
            {problems.slice(0, 200).map(({ row, key, problem }) => {
              const column = problem.at.startsWith("values/")
                ? columns.findIndex((c) => c.key === problem.at.slice(7))
                : problem.at
                  ? columns.findIndex((c) => c.key === problem.at)
                  : 0;
              const where = columns[column]?.label ?? "";
              return (
                <li key={`${key}:${problem.at}:${problem.detail}`}>
                  <button
                    type="button"
                    className="record-sheet-jump"
                    onClick={() => {
                      go({ row, col: Math.max(0, column) });
                      window.requestAnimationFrame(() => {
                        grid.current
                          ?.querySelector<HTMLElement>(
                            `[data-cell="${String(row)}:${String(Math.max(0, column))}"]`,
                          )
                          ?.focus();
                      });
                    }}
                  >
                    {rowName(rows[row])}
                    {where && problem.at ? `, ${where}` : ""}
                  </button>
                  {problem.kind === "ambiguous" ? " (reads two ways)" : ""}: {problem.detail}
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}
      {toSave.length > SAVE_ROWS ? (
        <div className="alert alert-warning" role="status">
          <p>
            A save takes at most {SAVE_ROWS.toLocaleString()} rows: import larger changes from a
            spreadsheet.
          </p>
        </div>
      ) : null}
      <ConfirmDialog
        isOpen={reviewing}
        title={`Save ${plural(toSave.length, "row", "rows")}?`}
        confirmLabel={stale.length ? "Save" : `Save ${plural(toSave.length, "row", "rows")}`}
        confirmVariant="primary"
        confirmDisabled={
          !reason.trim() ||
          (held.length > 0 && !holdReason.trim()) ||
          stale.length > 0 ||
          toSave.length > SAVE_ROWS
        }
        busy={saving}
        problem={saveProblem}
        onCancel={() => {
          setReviewing(false);
        }}
        onConfirm={() => {
          void save();
        }}
      >
        <p>
          {[
            edited ? plural(edited, `${label} changes`, `${many} change`) : "",
            created ? plural(created, `new ${label} is added`, `new ${many} are added`) : "",
          ]
            .filter(Boolean)
            .join(", and ")}
          , all at once: if any can’t be saved, none is.
        </p>
        {stale.length ? (
          <div className="alert alert-warning">
            <p>
              {stale.map((s) => s.hrid).join(", ")} changed since you opened the sheet.{" "}
              {stale.length === 1
                ? "Bring in its latest values: what you typed in it stays, and is checked again."
                : "Bring in their latest values: what you typed in them stays, and is checked again."}
            </p>
            <Button
              onClick={() => {
                void bringInLatest();
              }}
            >
              Bring in the latest
            </Button>
          </div>
        ) : null}
        <div className="record-sheet-review">
          <table className="table">
            <caption className="visually-hidden">What saving changes</caption>
            <thead>
              <tr>
                <th scope="col">Record</th>
                <th scope="col">Field</th>
                <th scope="col">Was</th>
                <th scope="col">Now</th>
              </tr>
            </thead>
            <tbody>
              {changes.map(({ row, checked, fields }) => {
                const extra = [
                  ...(checked.location
                    ? [{ name: LOCATION, label: "Location", was: "", now: checked.location.shown }]
                    : []),
                  ...(checked.amount
                    ? [
                        {
                          name: AMOUNT,
                          label: columns.find((c) => c.key === AMOUNT)?.label ?? "Amount",
                          was: "",
                          now: checked.amount_shown ?? "",
                        },
                      ]
                    : []),
                ];
                return [...fields, ...extra].map((f, i) => (
                  <tr key={`${checked.key}:${f.name}`}>
                    {i === 0 ? (
                      <th scope="row" rowSpan={fields.length + extra.length}>
                        {rowName(row)}
                        {checked.held.length ? (
                          <span className="muted"> (held for review)</span>
                        ) : null}
                      </th>
                    ) : null}
                    <td>{f.label}</td>
                    <td>
                      {!row?.entity ? (
                        <span className="muted">New</span>
                      ) : f.was ? (
                        <del>{f.was}</del>
                      ) : (
                        <span className="muted">Empty</span>
                      )}
                    </td>
                    <td>{f.now ? <ins>{f.now}</ins> : <span className="muted">Empty</span>}</td>
                  </tr>
                ));
              })}
            </tbody>
          </table>
        </div>
        <TextField
          label="Reason"
          help="Why, for each record’s history: people will read it."
          value={reason}
          onChange={setReason}
          maxLength={2000}
          multiline
        />
        {held.length ? (
          <TextField
            label="Why save values a rule holds for review"
            help={`${plural(held.length, "row breaks", "rows break")} a rule that holds a record for review: ${held
              .flatMap((a) => a.checked.held)
              .join(" ")}`}
            value={holdReason}
            onChange={setHoldReason}
            maxLength={2000}
            multiline
          />
        ) : null}
      </ConfirmDialog>
      <ConfirmDialog
        isOpen={leaving}
        title="Discard your changes?"
        confirmLabel="Discard changes"
        cancelLabel="Keep editing"
        busy={false}
        onCancel={() => {
          setLeaving(false);
        }}
        onConfirm={() => {
          setLeaving(false);
          onClose();
        }}
      >
        <p>
          {plural(sending.size, "row has", "rows have")} changes that aren’t saved. Discarding them
          leaves every {label} as it is.
        </p>
      </ConfirmDialog>
    </section>
  );
}
