/** Adapt storage records and a scan's focus request to the shared box map. */
import {
  slotKey,
  type BoxFinding,
  type BoxMapData,
  type BoxMapElement,
  type BoxOccupant,
  type MapMode,
  type Slot,
} from "@ikisci/science/box-map";
import { createElement, useCallback, useMemo } from "react";

import { FINDINGS, type Finding, type Shape, type Occupant } from "./storage";

export { slotKey, slotName, type MapMode } from "@ikisci/science/box-map";

function cellOf(o: Occupant): BoxOccupant {
  return {
    row: o.row,
    column: o.column,
    ...(o.ref ? { identifier: o.ref.hrid, ...(o.ref.title ? { name: o.ref.title } : {}) } : {}),
    ...(o.custodian ? { checkedOutBy: o.custodian.name || o.custodian.email } : {}),
    ...(o.container ? { count: o.count } : {}),
  };
}

function markOf(name: string): BoxFinding {
  if (name === "suggested") return { style: name, label: "where one goes" };
  const label = Object.hasOwn(FINDINGS, name) ? FINDINGS[name as Finding] : name;
  switch (name) {
    case "matched":
    case "missing":
    case "unexpected":
    case "wrong_slot":
    case "empty":
    case "checked_out":
      return { style: name, label };
    default:
      return { label };
  }
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
  onActivate?: (slot: Slot<Occupant>) => void;
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
  const cells = useMemo(() => contents.map(cellOf), [contents]);
  const positions = useMemo(
    () =>
      new Map(
        contents.flatMap((o) =>
          o.row && o.column ? [[slotKey(o.row, o.column), o] as const] : [],
        ),
      ),
    [contents],
  );
  const focusRequested = useCallback(
    (element: BoxMapElement | null) => {
      if (!focusKey) return;
      const [row, column] = (focusKey.split("#")[0] ?? "").split(":").map(Number);
      if (row && column) element?.focusPosition(row, column);
    },
    [focusKey],
  );
  const data: BoxMapData = {
    label,
    shape,
    contents: cells,
    mode,
    compact,
    showFree,
    ...(marked && { marked }),
    ...(selected && { selected: [...selected] }),
    ...(findings && {
      findings: Object.fromEntries(Array.from(findings, ([key, name]) => [key, markOf(name)])),
    }),
  };
  return createElement("iki-box-map", {
    ref: focusRequested,
    data,
    "onslot-activate": (event: CustomEvent<Slot>) => {
      const { row, column, label: position } = event.detail;
      onActivate?.({ row, column, label: position, occupant: positions.get(slotKey(row, column)) });
    },
  });
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
