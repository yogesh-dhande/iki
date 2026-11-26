/**
 * CSV, TSV and XLSX as a table (ADR 0226), read in the previews' worker by iki-previews'
 * readers, which follow the spreadsheet importer's rules (ADR 0178): the same encodings and
 * separators for delimited text, and a workbook's saved values, nothing calculated and no
 * macro run. A large CSV file is read from its start only (ADR 0227); a workbook is read
 * whole, at most 64 MiB. The table draws only the rows in view, so its first 50,000 rows
 * scroll as quickly as ten.
 */

import { useVirtualizer } from "@tanstack/react-virtual";
import { useId, useRef, useState } from "react";

import { formatSize } from "../entities";
import { Loading } from "../ui";
import { openBytes, type ByteSource } from "./bytes";
import type { ViewerProps } from "./file-preview";
import { count, Integrity, plural, PreviewNote } from "./notes";
import type { Table } from "./readings";
import { ReadFailed, useRead } from "./use-read";

/** The most rows and columns shown. */
export const MAX_ROWS = 50_000;
export const MAX_COLUMNS = 500;
/** A CSV file up to this is read whole and checked; a larger one, its first this many bytes. */
export const CSV_BYTES = 16 * 1024 * 1024;
const ROW_HEIGHT = 34;

/** Spreadsheet column letters: 1 → A, 27 → AA. */
export function columnName(n: number): string {
  let name = "";
  for (let i = n; i > 0; i = Math.floor((i - 1) / 26)) {
    name = String.fromCharCode(65 + ((i - 1) % 26)) + name;
  }
  return name;
}

type Kind = "csv" | "tsv" | "xlsx";

/** Which reader a file takes, from the viewer its format names. */
export function kindOf(viewer: string): Kind {
  if (viewer === "table-xlsx") return "xlsx";
  return viewer === "table-tsv" ? "tsv" : "csv";
}

export default function TableViewer({ file, stored, reader, viewer }: ViewerProps) {
  const kind = kindOf(viewer);
  const [sheet, setSheet] = useState(0);
  const source = useRead(
    (signal) => openBytes(stored, kind === "xlsx" ? Number.MAX_SAFE_INTEGER : CSV_BYTES, signal),
    [stored, kind],
  );
  if (source.phase === "failed") return <ReadFailed file={file} failed={source} />;
  if (source.phase === "reading") return <Loading label={`Reading ${file.name}…`} />;
  return (
    <TableRead
      file={file}
      source={source.value}
      kind={kind}
      sheet={sheet}
      onSheet={setSheet}
      read={async (signal) => {
        // A workbook, or a CSV file read whole, is all there; a larger CSV file, its start.
        const length = source.value.verified ? source.value.size : CSV_BYTES;
        const bytes = await source.value.read(0, length, signal);
        const { value } =
          kind === "xlsx"
            ? await reader.read("xlsx", [sheet, MAX_ROWS, MAX_COLUMNS], bytes)
            : await reader.read(
                "csv",
                [kind === "tsv" ? 9 : 0, MAX_ROWS, MAX_COLUMNS, source.value.verified ? 1 : 0],
                bytes,
              );
        return value;
      }}
    />
  );
}

function TableRead({
  file,
  source,
  kind,
  sheet,
  onSheet,
  read,
}: {
  file: ViewerProps["file"];
  source: ByteSource;
  kind: Kind;
  sheet: number;
  onSheet: (sheet: number) => void;
  read: (signal: AbortSignal) => Promise<Table>;
}) {
  const table = useRead(read, [source, sheet]);
  const id = useId();
  if (table.phase === "failed") return <ReadFailed file={file} failed={table} />;
  if (table.phase === "reading") return <Loading label={`Reading ${file.name}…`} />;
  const value = table.value;
  const sheetName = value.sheets[value.sheet]?.name;
  return (
    <div className="preview-table">
      {value.sheets.length > 1 ? (
        <div className="preview-toolbar" role="toolbar" aria-label={`Sheets of ${file.name}`}>
          <span className="preview-zoom">
            <label htmlFor={`${id}-sheet`}>Sheet</label>
            <select
              id={`${id}-sheet`}
              value={sheet}
              onChange={(event) => {
                onSheet(Number(event.target.value));
              }}
            >
              {value.sheets.map((s, i) => (
                <option key={i} value={i}>
                  {s.name}
                  {s.hidden ? " (hidden)" : ""}
                </option>
              ))}
            </select>
          </span>
        </div>
      ) : null}
      <Grid
        table={value}
        header={kind !== "xlsx"}
        caption={sheetName ? `${file.name}, sheet ${sheetName}` : file.name}
      />
      <PreviewNote>
        <Shown table={value} source={source} />
      </PreviewNote>
      {value.problems.length ? (
        <ul className="preview-problems">
          {value.problems.slice(0, 20).map((p, i) => (
            <li key={i}>{p}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** What of the table is shown, and what's left out. */
function Shown({ table, source }: { table: Table; source: ByteSource }) {
  const rows = table.rows.length;
  const parts = [
    table.more_rows
      ? source.verified || table.sheets.length
        ? `Showing its first ${plural(rows, "row", "rows")}.`
        : `Showing the ${plural(rows, "row", "rows")} in its first ${formatSize(CSV_BYTES)}, of ${formatSize(source.size)}.`
      : `${plural(rows, "row", "rows")}.`,
    table.more_columns
      ? `Showing ${count(Math.min(table.columns, MAX_COLUMNS))} of its ${count(table.columns)} columns.`
      : "",
    table.cut_cells
      ? `${plural(table.cut_cells, "cell is", "cells are")} cut short at 10,000 characters.`
      : "",
    table.sheets.length ? "" : `Read as ${table.encoding}.`,
  ];
  return (
    <>
      {parts.filter(Boolean).join(" ")} <Integrity source={source} />
    </>
  );
}

function Grid({ table, header, caption }: { table: Table; header: boolean; caption: string }) {
  const scroller = useRef<HTMLDivElement>(null);
  const captionId = useId();
  const width = Math.max(1, Math.min(table.columns, MAX_COLUMNS));
  const head = header ? table.rows[0] : undefined;
  const body = header ? table.rows.slice(1) : table.rows;
  const firstRow = table.first_row + (header ? 1 : 0);
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: body.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });
  const columns = Array.from({ length: width }, (_, i) => i);
  // A number column as wide as its largest number, then the cells, each 8 to 24 characters
  // wide. Each row is a grid of its own, so every track has a fixed size, and rows line up.
  const numberWidth = `${String(count(firstRow + body.length).length + 2)}ch`;
  const template = `${numberWidth} ${columns.map(() => "minmax(8ch, 24ch)").join(" ")}`;
  const minWidth = `calc(${numberWidth} + ${String(width * 8)}ch)`;
  return (
    <>
      <p id={captionId} className="visually-hidden">
        {caption}
      </p>
      {/* A scrolling region people reach with Tab, so the keyboard scrolls it. */}
      <div
        className="data-grid preview-grid"
        ref={scroller}
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable region
        tabIndex={0}
        aria-labelledby={captionId}
      >
        <div
          role="table"
          aria-labelledby={captionId}
          aria-rowcount={body.length + 1}
          aria-colcount={width + 1}
        >
          <div role="rowgroup" className="data-grid-head">
            <div
              role="row"
              aria-rowindex={1}
              className="data-grid-row"
              style={{ gridTemplateColumns: template, minWidth }}
            >
              <div role="columnheader" className="data-grid-cell preview-grid-number">
                <span className="visually-hidden">Row</span>
              </div>
              {columns.map((c) => (
                <div role="columnheader" key={c} className="data-grid-cell" title={head?.[c]}>
                  {!head ? (
                    columnName(table.first_column + c)
                  ) : (head[c] ?? "") !== "" ? (
                    head[c]
                  ) : (
                    <span className="muted">{columnName(table.first_column + c)}</span>
                  )}
                </div>
              ))}
            </div>
          </div>
          <div
            role="rowgroup"
            className="data-grid-body"
            style={{ height: `${String(virtualizer.getTotalSize())}px`, minWidth }}
          >
            {virtualizer.getVirtualItems().map((item) => {
              const row = body[item.index];
              if (!row) return null;
              return (
                <div
                  role="row"
                  key={item.index}
                  aria-rowindex={item.index + 2}
                  className="data-grid-row"
                  style={{
                    gridTemplateColumns: template,
                    minWidth,
                    transform: `translateY(${String(item.start)}px)`,
                  }}
                >
                  <div role="rowheader" className="data-grid-cell preview-grid-number muted">
                    {count(firstRow + item.index)}
                  </div>
                  {columns.map((c) => (
                    <div role="cell" key={c} className="data-grid-cell" title={row[c]}>
                      {row[c]}
                    </div>
                  ))}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </>
  );
}
