/**
 * A container's map (ADR 0180): its grid, each position with what's in it, as a grid of
 * cells to move through with the arrow keys. Opening a position opens what's in it;
 * choosing several selects them, to move, check out or label together; picking one says
 * where something goes. A position can be marked, as a record's own is on its page, and
 * the free ones shown.
 */

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
} from "react";

import { FINDINGS, rowLabel, slotsOf, type Occupant, type Shape, type Slot } from "./storage";

/** What each mark on a position says: a reconciliation's findings, and where one goes. */
const FOUND: Record<string, string> = { ...FINDINGS, suggested: "where one goes" };

export type MapMode = "open" | "select" | "pick";

/** A position's key, as selections hold it. */
export function slotKey(row: number, column: number): string {
  return `${String(row)}:${String(column)}`;
}

/** What's in a position, as screen readers hear it and its tooltip says. */
export function slotName(slot: Slot): string {
  const o = slot.occupant;
  if (!o) return `${slot.label}, empty`;
  if (!o.ref) return `${slot.label}, taken by a record you can’t read`;
  const named =
    o.ref.title && o.ref.title !== o.ref.hrid ? `${o.ref.hrid} ${o.ref.title}` : o.ref.hrid;
  const out = o.custodian ? `, checked out by ${o.custodian.name || o.custodian.email}` : "";
  const holds = o.container ? `, holding ${String(o.count)}` : "";
  return `${slot.label}, ${named}${holds}${out}`;
}

/** What a position shows, in the room it has: a tube's short name, or its HRID's number. */
function shortText(o: Occupant): string {
  if (!o.ref) return "";
  if (o.ref.title && o.ref.title !== o.ref.hrid) return o.ref.title;
  return /0*(\d+)$/.exec(o.ref.hrid)?.[1] ?? o.ref.hrid;
}

export function BoxMap({
  label,
  shape,
  contents,
  mode = "open",
  selected,
  onActivate,
  marked,
  showFree = false,
  findings,
  compact = false,
  focusKey,
}: {
  /** What the grid is, for screen readers: such as "BOX-000014's positions". */
  label: string;
  shape: Shape;
  contents: readonly Occupant[];
  mode?: MapMode;
  /** The positions chosen, by slotKey. */
  selected?: ReadonlySet<string>;
  /** A position opened, chosen or picked: with Enter, Space or a click. */
  onActivate?: (slot: Slot) => void;
  /** A position to mark, as a record's own is on its page. */
  marked?: { row: number; column: number } | undefined;
  /** Shade the free positions, so where things can go shows. */
  showFree?: boolean;
  /** A reconciliation's finding at each position, by slotKey. */
  findings?: ReadonlyMap<string, string>;
  /** Smaller cells, for a record's own page beside its other cards. */
  compact?: boolean;
  /** A position to move focus to, when it changes: one just scanned, as `row:column`,
   * with `#` and anything after it to ask again for the same one. */
  focusKey?: string | undefined;
}) {
  const rows = useMemo(() => slotsOf(shape, contents), [shape, contents]);
  const [active, setActive] = useState<{ row: number; column: number }>(() =>
    marked ? { row: marked.row, column: marked.column } : { row: 1, column: 1 },
  );
  const grid = useRef<HTMLDivElement>(null);
  const moveTo = (row: number, column: number, focus = true) => {
    const r = Math.min(Math.max(row, 1), shape.rows);
    const c = Math.min(Math.max(column, 1), shape.columns);
    setActive({ row: r, column: c });
    if (focus) {
      grid.current
        ?.querySelector<HTMLElement>(`[data-slot="${slotKey(r, c)}"]`)
        ?.focus({ preventScroll: false });
    }
  };
  useEffect(() => {
    if (!focusKey) return;
    // Focusing it makes it the active position, as moving to it does.
    const key = focusKey.split("#")[0] ?? "";
    grid.current?.querySelector<HTMLElement>(`[data-slot="${key}"]`)?.focus();
  }, [focusKey]);
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>, slot: Slot) => {
    const { row, column } = slot;
    const keys: Record<string, () => void> = {
      ArrowRight: () => {
        moveTo(row, column + 1);
      },
      ArrowLeft: () => {
        moveTo(row, column - 1);
      },
      ArrowDown: () => {
        moveTo(row + 1, column);
      },
      ArrowUp: () => {
        moveTo(row - 1, column);
      },
      Home: () => {
        moveTo(event.ctrlKey ? 1 : row, 1);
      },
      End: () => {
        moveTo(event.ctrlKey ? shape.rows : row, shape.columns);
      },
      PageUp: () => {
        moveTo(1, column);
      },
      PageDown: () => {
        moveTo(shape.rows, column);
      },
      Enter: () => onActivate?.(slot),
      " ": () => onActivate?.(slot),
    };
    const run = keys[event.key];
    if (run) {
      event.preventDefault();
      run();
    }
  };
  const numbered = shape.labels === "number";
  const style = { "--map-columns": String(shape.columns) } as CSSProperties;
  return (
    <div className={compact ? "box-map-scroll box-map-compact" : "box-map-scroll"}>
      <div
        className={`box-map box-map-${mode}`}
        role="grid"
        aria-label={label}
        aria-multiselectable={mode === "select" ? "true" : undefined}
        ref={grid}
        style={style}
      >
        {numbered ? null : (
          <div className="box-map-row box-map-head" role="row">
            <span className="box-map-corner" role="columnheader">
              <span className="visually-hidden">Row</span>
            </span>
            {rows[0]?.map((s) => (
              <span key={s.column} className="box-map-colhead" role="columnheader">
                {s.column}
              </span>
            ))}
          </div>
        )}
        {rows.map((cells, r) => (
          <div className="box-map-row" role="row" key={r}>
            {numbered ? null : (
              <span className="box-map-rowhead" role="rowheader">
                {rowLabel(shape, r + 1)}
              </span>
            )}
            {cells.map((slot) => {
              const key = slotKey(slot.row, slot.column);
              const o = slot.occupant;
              const isSelected = selected?.has(key) ?? false;
              const isMarked = marked?.row === slot.row && marked.column === slot.column;
              const finding = findings?.get(key);
              const classes = [
                "box-map-slot",
                o ? (o.ref ? "box-map-taken" : "box-map-hidden") : "box-map-free",
                o?.custodian ? "box-map-out" : "",
                o?.container ? "box-map-container" : "",
                isSelected ? "box-map-selected" : "",
                isMarked ? "box-map-marked" : "",
                showFree && !o ? "box-map-show-free" : "",
                finding ? `box-map-finding box-map-${finding}` : "",
              ]
                .filter(Boolean)
                .join(" ");
              const isActive = active.row === slot.row && active.column === slot.column;
              const name = slotName(slot);
              return (
                <div
                  key={key}
                  role="gridcell"
                  className={classes}
                  data-slot={key}
                  aria-selected={mode === "select" ? isSelected : undefined}
                  aria-current={isMarked ? "location" : undefined}
                  title={name}
                  tabIndex={isActive ? 0 : -1}
                  onFocus={() => {
                    if (!isActive) setActive({ row: slot.row, column: slot.column });
                  }}
                  onKeyDown={(event) => {
                    onKeyDown(event, slot);
                  }}
                  onClick={() => {
                    moveTo(slot.row, slot.column, false);
                    onActivate?.(slot);
                  }}
                >
                  {/* Its name is its text, not a label over what it shows (which is cut short
                      to fit), so nothing visible is left out of what's heard. */}
                  <span className="visually-hidden">
                    {finding ? `${name}: ${FOUND[finding] ?? finding}` : name}
                  </span>
                  <span className="box-map-pos" aria-hidden="true">
                    {slot.label}
                  </span>
                  {o ? (
                    <span className="box-map-what" aria-hidden="true">
                      {o.container ? String(o.count) : shortText(o)}
                    </span>
                  ) : null}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

/** The map's key: what its shading means. */
export function MapLegend({ findings = false }: { findings?: boolean }) {
  return (
    <ul className="box-map-legend" aria-label="Key">
      {findings ? (
        <>
          <li>
            <span className="box-map-swatch box-map-matched" aria-hidden="true" /> Matched
          </li>
          <li>
            <span className="box-map-swatch box-map-wrong_slot" aria-hidden="true" /> Wrong position
          </li>
          <li>
            <span className="box-map-swatch box-map-missing" aria-hidden="true" /> Missing
          </li>
          <li>
            <span className="box-map-swatch box-map-unexpected" aria-hidden="true" /> Unexpected
          </li>
        </>
      ) : (
        <>
          <li>
            <span className="box-map-swatch box-map-taken" aria-hidden="true" /> Taken
          </li>
          <li>
            <span className="box-map-swatch box-map-free" aria-hidden="true" /> Free
          </li>
          <li>
            <span className="box-map-swatch box-map-out" aria-hidden="true" /> Checked out
          </li>
          <li>
            <span className="box-map-swatch box-map-hidden" aria-hidden="true" /> Another project’s
          </li>
        </>
      )}
    </ul>
  );
}
