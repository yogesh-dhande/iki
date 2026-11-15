/**
 * The alignment viewer (ADR 0088): an alignment's rows, a row per sequence, drawn on a
 * canvas as they scroll into view, so thousands of rows and columns stay quick. iki-wasm
 * reads the file in a Web Worker. Rows headed by the HRID of a sequence the alignment
 * lists link to it and can be picked to run an action on; others, such as reads, show
 * under their own names. The sequence the others were aligned to is pinned at the top,
 * and the others can show only where they differ from it.
 */

import type { Schemas } from "@ikisci/sdk";
import { Link } from "@tanstack/react-router";
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent,
} from "react";

import {
  colourOf,
  idsOf,
  isGap,
  KINDS,
  moved,
  residueNumber,
  shownRows,
  type Colouring,
  type Kind,
  type Shown,
} from "../alignment";
import { formatSize, type EntityType } from "../entities";
import { MAX_READ_BYTES, readInWorker, type Reading } from "../reading";
import { MAX_BATCH } from "../runs";
import { readStoredFile } from "../stored";
import { buttonClass, Loading } from "../ui";
import type { Capture, Position } from "../views";
import type { Source } from "./entity-viewer";

type Viewer = Schemas["AlignmentViewerOut"];
type Aligned = Extract<Reading, { kind: "alignment" }>;

/** A column's width and a row's height, in CSS pixels: rows are tall enough for their
 * labels' links and checkboxes to be targets of 24 pixels (WCAG 2.2's 2.5.8). */
export const CELL = 12;
export const ROW = 24;
/** The labels' column. */
export const LABELS = 240;
/** The bands above the rows: the ruler, the consensus, Clustal's marks, and the agreement. */
const RULER = 18;
const CONSENSUS = 18;
const MARKS = 14;
const AGREEMENT = 22;
export const HEADER = RULER + CONSENSUS + MARKS + AGREEMENT;
/** Past this many columns the page couldn't scroll to the end: browsers cap an element's size. */
export const MAX_COLUMNS = 1_000_000;

const number = new Intl.NumberFormat();
const percent = new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 1 });

export function AlignmentViewer({
  source,
  type,
  viewer,
  position,
  onCapture,
}: {
  source: Source;
  type: EntityType;
  viewer: Viewer;
  /** Where a saved view's cursor was. */
  position?: Position | undefined;
  onCapture?: ((capture: Capture | null) => void) | undefined;
}) {
  const value = source.values[viewer.alignment];
  const fileId = typeof value === "string" ? value : null;
  const file = fileId ? source.files[fileId] : undefined;
  const field = type.fields.find((f) => f.name === viewer.alignment);
  if (!fileId) {
    return <p className="muted">It has no {field?.label.toLowerCase() ?? "alignment"} yet.</p>;
  }
  if (!file)
    return <p className="muted">Its {field?.label.toLowerCase()} is a file you can’t read.</p>;
  return (
    <section className="alignment" aria-labelledby="alignment-file">
      <p id="alignment-file" className="structure-file">
        <span>{file.name}</span> <span className="muted">{formatSize(file.size)}</span> ·{" "}
        <a href={`/api/v1/files/${fileId}/content`} download={file.name}>
          Download {file.name}
        </a>
      </p>
      {file.size > MAX_READ_BYTES ? (
        <p>
          It’s bigger than the alignment viewer opens ({formatSize(MAX_READ_BYTES)}). Download it to
          open it in a desktop viewer, such as Jalview.
        </p>
      ) : (
        <AlignmentFile
          key={fileId}
          fileId={fileId}
          name={file.name}
          source={source}
          viewer={viewer}
          position={position}
          onCapture={onCapture}
        />
      )}
    </section>
  );
}

type State =
  { phase: "reading" } | { phase: "read"; reading: Reading } | { phase: "failed"; message: string };

function AlignmentFile({
  fileId,
  name,
  source,
  viewer,
  position,
  onCapture,
}: {
  fileId: string;
  name: string;
  source: Source;
  viewer: Viewer;
  position?: Position | undefined;
  onCapture?: ((capture: Capture | null) => void) | undefined;
}) {
  const [state, setState] = useState<State>({ phase: "reading" });
  useEffect(() => {
    const abort = new AbortController();
    readStoredFile(fileId, abort.signal)
      .then((bytes) => readInWorker(bytes))
      .then(
        (reading) => {
          if (!abort.signal.aborted) setState({ phase: "read", reading });
        },
        (error: unknown) => {
          if (abort.signal.aborted) return;
          setState({
            phase: "failed",
            message: `Iki couldn’t read ${name}. ${error instanceof Error ? error.message : ""}`,
          });
        },
      );
    return () => {
      abort.abort();
    };
  }, [fileId, name]);

  if (state.phase === "reading") return <Loading label={`Reading ${name}…`} />;
  if (state.phase === "failed") {
    return (
      <div className="alert alert-error" role="alert">
        <p>{state.message}</p>
      </div>
    );
  }
  const { reading } = state;
  if (reading.kind === "error") {
    return (
      <div className="alert alert-error" role="alert">
        <p>
          {name} isn’t FASTA the viewer can read: {reading.message}
          {reading.line ? ` (line ${number.format(reading.line)})` : ""}.
        </p>
      </div>
    );
  }
  if (reading.kind === "sequences") {
    return (
      <>
        <p>
          {name}’s {number.format(reading.records.length)} sequences aren’t aligned: an alignment’s
          rows, gaps and all, are all one length.
        </p>
        <ul className="alignment-records">
          {reading.records.slice(0, 100).map((record, i) => (
            <li key={i}>
              <span className="mono">{record.id}</span>{" "}
              <span className="muted">{number.format(record.length)} long</span>
            </li>
          ))}
        </ul>
      </>
    );
  }
  if (reading.length > MAX_COLUMNS) {
    return (
      <p>
        It’s {number.format(reading.length)} columns long, more than the alignment viewer draws (
        {number.format(MAX_COLUMNS)}). Download it to open it in a desktop viewer, such as Jalview.
      </p>
    );
  }
  return (
    <AlignmentGrid
      alignment={reading}
      source={source}
      viewer={viewer}
      title={name}
      position={position}
      onCapture={onCapture}
    />
  );
}

/** The page's colours, read from its stylesheet so the canvas follows the theme. */
interface Palette {
  surface: string;
  band: string;
  text: string;
  muted: string;
  border: string;
  focus: string;
  accent: string;
  residueText: string;
  kinds: Record<Kind, string>;
  font: string;
}

function readPalette(element: Element): Palette {
  const style = getComputedStyle(element);
  const v = (name: string, fallback: string) => style.getPropertyValue(name).trim() || fallback;
  const kinds = Object.fromEntries(
    KINDS.map((kind) => [kind, v(`--residue-${kind}`, "#cccccc")]),
  ) as Record<Kind, string>;
  return {
    surface: v("--surface", "#ffffff"),
    band: v("--surface-2", "#f0f2f5"),
    text: v("--text", "#16202c"),
    muted: v("--muted", "#4d5b6a"),
    border: v("--border", "#d5dbe2"),
    focus: v("--focus", "#1f5fbf"),
    accent: v("--accent", "#0b6e66"),
    residueText: v("--residue-text", "#16202c"),
    kinds,
    font: v("--mono", "monospace"),
  };
}

function darkQuery(): MediaQueryList | null {
  return typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: dark)")
    : null;
}

/** The colour scheme, so the canvas is redrawn when it changes. */
function useScheme(): string {
  const [scheme, setScheme] = useState(() => (darkQuery()?.matches ? "dark" : "light"));
  useEffect(() => {
    const query = darkQuery();
    if (!query) return;
    const changed = () => {
      setScheme(query.matches ? "dark" : "light");
    };
    query.addEventListener("change", changed);
    return () => {
      query.removeEventListener("change", changed);
    };
  }, []);
  return scheme;
}

interface Scroll {
  left: number;
  top: number;
  width: number;
  height: number;
}

function AlignmentGrid({
  alignment,
  source,
  viewer,
  title,
  position,
  onCapture,
}: {
  alignment: Aligned;
  source: Source;
  viewer: Viewer;
  title: string;
  position?: Position | undefined;
  onCapture?: ((capture: Capture | null) => void) | undefined;
}) {
  const referenceId = viewer.reference ? idsOf(source.values[viewer.reference])[0] : undefined;
  const memberIds = viewer.rows ? idsOf(source.values[viewer.rows]) : [];
  const listed = useMemo(
    () => [...(referenceId ? [referenceId] : []), ...memberIds],
    // The IDs as text, so a new array of the same IDs doesn't recompute the rows.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [referenceId, memberIds.join(",")],
  );
  const all = useMemo(
    () => shownRows(alignment.rows, listed, source.refs),
    [alignment.rows, listed, source.refs],
  );
  const referenceHrid = referenceId ? source.refs[referenceId]?.hrid : undefined;
  const reference = referenceHrid
    ? (all.find((r) => r.entity?.hrid === referenceHrid) ?? null)
    : null;
  // The reference first, pinned; the others in the file's order.
  const rows = useMemo(
    () => (reference ? [reference, ...all.filter((r) => r !== reference)] : all),
    [all, reference],
  );
  const pinned = reference ? 1 : 0;
  const top = HEADER + pinned * ROW;
  const length = alignment.length;

  const [cursor, setCursor] = useState(() => {
    const row = position ? rows.findIndex((r) => r.index === position.row - 1) : 0;
    const column = position ? Math.min(Math.max(position.column - 1, 0), length - 1) : 0;
    return { row: Math.max(row, 0), column };
  });
  const [colouring, setColouring] = useState<Colouring>("agreeing");
  const [differences, setDifferences] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [scroll, setScroll] = useState<Scroll>({ left: 0, top: 0, width: 0, height: 0 });
  const region = useRef<HTMLDivElement | null>(null);
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const scheme = useScheme();
  const ids = useId();

  const cursorRow = rows[cursor.row];
  useEffect(() => {
    if (!onCapture || !cursorRow) return;
    onCapture(() =>
      Promise.resolve({
        version: 1,
        position: { column: cursor.column + 1, row: cursorRow.index + 1 },
      }),
    );
    return () => {
      onCapture(null);
    };
  }, [onCapture, cursor.column, cursorRow]);

  // What's in view, and so the canvas's size: never past the alignment's end, so the canvas
  // never makes the region scroll further than the alignment goes.
  const spaceWidth = LABELS + length * CELL + CELL;
  const spaceHeight = top + (rows.length - pinned) * ROW;
  const measure = useCallback(() => {
    const el = region.current;
    if (!el) return;
    setScroll({
      left: el.scrollLeft,
      top: el.scrollTop,
      width: Math.min(el.clientWidth, spaceWidth),
      height: Math.min(el.clientHeight, spaceHeight),
    });
  }, [spaceWidth, spaceHeight]);
  useLayoutEffect(() => {
    measure();
    const el = region.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => {
      observer.disconnect();
    };
  }, [measure]);

  // A saved view's cursor, scrolled into view when the viewer opens.
  const opened = useRef(false);
  useLayoutEffect(() => {
    if (opened.current || !position || !scroll.width) return;
    opened.current = true;
    reveal(cursor, true);
    // Only once, when the viewer has its size.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scroll.width]);

  useEffect(() => {
    const el = canvas.current;
    const context = el?.getContext("2d");
    if (!el || !context || !region.current || !scroll.width) return;
    const ratio = window.devicePixelRatio || 1;
    const width = Math.round(scroll.width * ratio);
    const height = Math.round(scroll.height * ratio);
    // Resized only when its size changes: each resize allocates the canvas afresh.
    if (el.width !== width || el.height !== height) {
      el.width = width;
      el.height = height;
      el.style.width = `${String(scroll.width)}px`;
      el.style.height = `${String(scroll.height)}px`;
    }
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    draw(context, {
      palette: readPalette(region.current),
      alignment,
      rows,
      pinned,
      scroll,
      cursor,
      colouring,
      differences: differences && reference ? reference.row.aligned : null,
    });
  }, [alignment, rows, pinned, scroll, cursor, colouring, differences, reference, scheme]);

  /** Scrolls `at` into view: just into it as the cursor moves, or to the middle on a jump. */
  function reveal(at: { row: number; column: number }, centre = false) {
    const el = region.current;
    if (!el) return;
    const x = at.column * CELL;
    const across = el.clientWidth - LABELS;
    if (centre) el.scrollLeft = Math.max(x - across / 2, 0);
    else if (x < el.scrollLeft) el.scrollLeft = x;
    else if (x + CELL > el.scrollLeft + across) el.scrollLeft = x + CELL - across;
    if (at.row < pinned) return;
    const y = (at.row - pinned) * ROW;
    const down = el.clientHeight - top;
    if (y < el.scrollTop) el.scrollTop = y;
    else if (y + ROW > el.scrollTop + down) el.scrollTop = y + ROW - down;
  }

  function moveTo(at: { row: number; column: number }, centre = false) {
    setCursor(at);
    reveal(at, centre);
  }

  function keyed(event: KeyboardEvent<HTMLDivElement>) {
    if (event.target !== event.currentTarget) return;
    const page = Math.max(Math.floor((scroll.height - top) / ROW) - 1, 1);
    const next = moved(event.key, cursor, rows.length, length, page);
    if (!next) return;
    event.preventDefault();
    moveTo(next);
  }

  function clicked(event: MouseEvent<HTMLDivElement>) {
    const el = region.current;
    if (!el) return;
    const box = el.getBoundingClientRect();
    const x = event.clientX - box.left - LABELS;
    const y = event.clientY - box.top;
    if (x < 0 || y < HEADER) return;
    const column = Math.floor((x + el.scrollLeft) / CELL);
    const row = y < top ? 0 : Math.floor((y - top + el.scrollTop) / ROW) + pinned;
    if (column >= length || row >= rows.length) return;
    setCursor({ row, column });
    el.focus();
  }

  // The labels in view, as links and checkboxes: the canvas draws everything else.
  const first = Math.max(Math.floor(scroll.top / ROW) - 2, 0);
  const shown = Math.ceil((scroll.height || 600) / ROW) + 4;
  const body = rows.slice(pinned + first, pinned + first + shown);
  const linked = rows.filter((r) => r.entity).map((r) => r.entity?.hrid ?? "");
  const pickedSet = new Set(picked);
  const toggle = (hrid: string) => {
    setPicked((now) => (now.includes(hrid) ? now.filter((h) => h !== hrid) : [...now, hrid]));
  };

  const identity = length ? alignment.identical / length : 0;
  const atRow = rows[cursor.row];
  const residue = atRow?.row.aligned.charAt(cursor.column) ?? "";
  const residueAt = atRow ? residueNumber(atRow.row.aligned, cursor.column) : null;
  const labelOf = (r: Shown) => r.entity?.hrid ?? r.row.id;

  return (
    <>
      <p className="muted">
        {number.format(alignment.rows.length)} rows, {number.format(length)} columns ·{" "}
        {number.format(alignment.identical)} identical ({percent.format(identity)}) ·{" "}
        {number.format(alignment.gapped)} with gaps
      </p>
      {referenceHrid && !reference ? (
        <p className="muted">
          Its reference, {referenceHrid}, isn’t one of its rows: no row is headed by its HRID.
        </p>
      ) : null}
      <div className="alignment-controls">
        <label htmlFor={`${ids}-colour`}>
          Colour{" "}
          <select
            id={`${ids}-colour`}
            value={colouring}
            onChange={(event) => {
              setColouring(event.target.value as Colouring);
            }}
          >
            <option value="agreeing">Where columns agree</option>
            <option value="all">Every residue</option>
            <option value="none">None</option>
          </select>
        </label>
        {reference ? (
          <label className="choice" htmlFor={`${ids}-differences`}>
            <input
              id={`${ids}-differences`}
              type="checkbox"
              role="switch"
              checked={differences}
              onChange={(event) => {
                setDifferences(event.target.checked);
              }}
            />
            <span className="choice-text">Only where they differ from {labelOf(reference)}</span>
          </label>
        ) : null}
        <form
          className="alignment-go"
          onSubmit={(event) => {
            event.preventDefault();
            const data = new FormData(event.currentTarget);
            const column = Number(
              (data.get("column") as string | null) ?? "".replaceAll(/[\s,]/g, ""),
            );
            if (!Number.isInteger(column) || column < 1 || column > length) return;
            moveTo({ row: cursor.row, column: column - 1 }, true);
            region.current?.focus();
          }}
        >
          <label htmlFor={`${ids}-column`}>Go to column</label>
          <input
            id={`${ids}-column`}
            name="column"
            type="text"
            inputMode="numeric"
            autoComplete="off"
            spellCheck={false}
          />
          <button type="submit" className={buttonClass("quiet")}>
            Go
          </button>
        </form>
      </div>
      <SelectionBar
        picked={picked}
        linked={linked}
        project={source.owner_project?.slug}
        onPick={setPicked}
      />
      {/* A widget of its own: its keys move a cursor across the canvas, whose place is read
          out below it, and it scrolls, so it takes focus (as scrolling regions must). */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <div
        ref={region}
        className="alignment-scroll"
        role="application"
        aria-roledescription="alignment"
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        tabIndex={0}
        aria-label={`Alignment of ${title}`}
        aria-describedby={`${ids}-keys ${ids}-at`}
        style={{ height: `min(70vh, ${String(top + (rows.length - pinned) * ROW + 16)}px)` }}
        onScroll={measure}
        onKeyDown={keyed}
        onClick={clicked}
      >
        <div
          className="alignment-space"
          style={{ width: `${String(spaceWidth)}px`, height: `${String(spaceHeight)}px` }}
        >
          <div className="alignment-canvas">
            <canvas ref={canvas} aria-hidden="true" />
          </div>
          <div className="alignment-labels" style={{ width: `${String(LABELS)}px` }}>
            <div className="alignment-corner" style={{ height: `${String(top)}px` }}>
              <span style={{ height: `${String(RULER)}px` }}>Column</span>
              <span style={{ height: `${String(CONSENSUS)}px` }}>Consensus</span>
              <span style={{ height: `${String(MARKS)}px` }}>Conserved</span>
              <span style={{ height: `${String(AGREEMENT)}px` }}>Agreement</span>
              {reference ? (
                <RowLabel
                  shown={reference}
                  top={HEADER}
                  pinned
                  picked={pickedSet}
                  onToggle={toggle}
                />
              ) : null}
            </div>
            {body.map((r, i) => (
              <RowLabel
                key={r.index}
                shown={r}
                top={top + (first + i) * ROW}
                picked={pickedSet}
                onToggle={toggle}
              />
            ))}
          </div>
        </div>
      </div>
      <p id={`${ids}-keys`} className="visually-hidden">
        The arrow keys move along the alignment; Home and End go to a row’s first and last column.
      </p>
      <p id={`${ids}-at`} className="alignment-at" aria-live="polite">
        {atRow ? (
          <>
            <strong>{labelOf(atRow)}</strong>, column {number.format(cursor.column + 1)}:{" "}
            {residueAt === null
              ? "a gap"
              : `${residue}, residue ${number.format(residueAt)} of ${number.format(atRow.row.residues)}`}
            . Consensus {alignment.consensus.charAt(cursor.column)}, where{" "}
            {String(alignment.agreement[cursor.column] ?? 0)}% of rows agree.
          </>
        ) : null}
      </p>
    </>
  );
}

function RowLabel({
  shown,
  top,
  pinned = false,
  picked,
  onToggle,
}: {
  shown: Shown;
  top: number;
  pinned?: boolean;
  picked: Set<string>;
  onToggle: (hrid: string) => void;
}) {
  const { entity, row } = shown;
  return (
    <div
      className={pinned ? "alignment-label alignment-label-pinned" : "alignment-label"}
      style={{ top: `${String(top)}px`, height: `${String(ROW)}px` }}
    >
      {entity ? (
        <>
          <input
            type="checkbox"
            checked={picked.has(entity.hrid)}
            aria-label={`Select ${entity.hrid}`}
            onChange={() => {
              onToggle(entity.hrid);
            }}
          />
          <Link to="/entities/$ref/" params={{ ref: entity.hrid }} title={entity.title}>
            {entity.hrid}
          </Link>
          <span className="muted">{entity.title}</span>
        </>
      ) : (
        <span title={row.description || row.id}>{row.id}</span>
      )}
      {pinned ? <span className="alignment-pin">Reference</span> : null}
    </div>
  );
}

/** What a person picked: sequences in Iki the alignment lists, ready to run an action on. */
function SelectionBar({
  picked,
  linked,
  project,
  onPick,
}: {
  picked: string[];
  linked: string[];
  project: string | undefined;
  onPick: (hrids: string[]) => void;
}) {
  if (!linked.length) return null;
  const tooMany = picked.length > MAX_BATCH;
  let status = "";
  if (tooMany) {
    status = `${String(picked.length)} selected: an action runs on at most ${String(MAX_BATCH)} at once`;
  } else if (picked.length) {
    status = `${String(picked.length)} ${picked.length === 1 ? "sequence" : "sequences"} selected`;
  }
  return (
    <div className={picked.length ? "selection-bar" : "alignment-select"}>
      <p role="status" className={picked.length ? undefined : "visually-hidden"}>
        {status}
      </p>
      {picked.length && !tooMany && project ? (
        <Link
          to="/projects/$slug/runs/new/"
          params={{ slug: project }}
          search={{ entities: picked.join(",") }}
          className={buttonClass("primary")}
        >
          Run action on {picked.length === 1 ? "it" : "them"}
        </Link>
      ) : null}
      {picked.length < linked.length ? (
        <button
          type="button"
          className={buttonClass("quiet")}
          onClick={() => {
            onPick(linked);
          }}
        >
          Select every sequence in Iki ({number.format(linked.length)})
        </button>
      ) : null}
      {picked.length ? (
        <button
          type="button"
          className={buttonClass("quiet")}
          onClick={() => {
            onPick([]);
          }}
        >
          Clear selection
        </button>
      ) : null}
    </div>
  );
}

/** Draws what's in view: the bands, the pinned reference, the rows, and the cursor. */
function draw(
  context: CanvasRenderingContext2D,
  {
    palette,
    alignment,
    rows,
    pinned,
    scroll,
    cursor,
    colouring,
    differences,
  }: {
    palette: Palette;
    alignment: Aligned;
    rows: Shown[];
    pinned: number;
    scroll: Scroll;
    cursor: { row: number; column: number };
    colouring: Colouring;
    /** The reference's aligned residues, to draw the others only where they differ. */
    differences: string | null;
  },
) {
  const { width, height } = scroll;
  const top = HEADER + pinned * ROW;
  const first = Math.floor(scroll.left / CELL);
  const last = Math.min(Math.ceil((scroll.left + width - LABELS) / CELL), alignment.length);
  const xOf = (column: number) => LABELS + column * CELL - scroll.left;
  context.fillStyle = palette.surface;
  context.fillRect(0, 0, width, height);
  context.textAlign = "center";
  context.textBaseline = "middle";

  context.save();
  context.beginPath();
  context.rect(LABELS, 0, width - LABELS, height);
  context.clip();

  // The bands.
  context.fillStyle = palette.band;
  context.fillRect(LABELS, 0, width - LABELS, HEADER);
  context.font = `10px ${palette.font}`;
  context.fillStyle = palette.muted;
  context.strokeStyle = palette.border;
  context.lineWidth = 1;
  for (let c = first; c < last; c++) {
    const n = c + 1;
    if (n % 10 === 0 || n === 1) {
      const x = xOf(c) + CELL / 2;
      context.fillText(number.format(n), x, RULER / 2 - 1);
      context.beginPath();
      context.moveTo(Math.round(x) + 0.5, RULER - 4);
      context.lineTo(Math.round(x) + 0.5, RULER);
      context.stroke();
    }
  }
  context.font = `12px ${palette.font}`;
  for (let c = first; c < last; c++) {
    const x = xOf(c) + CELL / 2;
    context.fillStyle = palette.text;
    context.fillText(alignment.consensus.charAt(c), x, RULER + CONSENSUS / 2);
    context.fillStyle = palette.muted;
    context.fillText(alignment.conservation.charAt(c), x, RULER + CONSENSUS + MARKS / 2);
    const share = alignment.agreement[c] ?? 0;
    const bar = Math.round(((AGREEMENT - 4) * share) / 100);
    context.fillStyle = palette.accent;
    context.fillRect(xOf(c) + 1, HEADER - 2 - bar, CELL - 2, bar);
  }

  const drawRow = (shown: Shown, y: number, against: string | null) => {
    const aligned = shown.row.aligned;
    for (let c = first; c < last; c++) {
      const residue = aligned.charAt(c);
      const x = xOf(c);
      if (isGap(residue)) {
        context.fillStyle = palette.muted;
        context.fillText("-", x + CELL / 2, y + ROW / 2);
        continue;
      }
      if (against !== null && against.charAt(c).toUpperCase() === residue.toUpperCase()) {
        context.fillStyle = palette.muted;
        context.fillText("·", x + CELL / 2, y + ROW / 2);
        continue;
      }
      const kind = colourOf(
        residue,
        alignment.consensus.charAt(c),
        alignment.agreement[c] ?? 0,
        alignment.alphabet,
        colouring,
      );
      if (kind) {
        context.fillStyle = palette.kinds[kind];
        context.fillRect(x, y + 1, CELL, ROW - 2);
        context.fillStyle = palette.residueText;
      } else {
        context.fillStyle = palette.text;
      }
      context.fillText(residue, x + CELL / 2, y + ROW / 2);
    }
  };

  // The rows, under the bands and the pinned reference.
  context.save();
  context.beginPath();
  context.rect(LABELS, top, width - LABELS, height - top);
  context.clip();
  const firstRow = Math.floor(scroll.top / ROW);
  const lastRow = Math.min(Math.ceil((scroll.top + height - top) / ROW), rows.length - pinned);
  for (let r = firstRow; r < lastRow; r++) {
    const shown = rows[r + pinned];
    if (shown) drawRow(shown, top + r * ROW - scroll.top, differences);
  }
  context.restore();

  const reference = pinned ? rows[0] : undefined;
  if (reference) {
    context.fillStyle = palette.surface;
    context.fillRect(LABELS, HEADER, width - LABELS, ROW);
    drawRow(reference, HEADER, null);
    context.strokeStyle = palette.border;
    context.beginPath();
    context.moveTo(LABELS, top - 0.5);
    context.lineTo(width, top - 0.5);
    context.stroke();
  }

  // The cursor, and its column in the ruler.
  const cx = xOf(cursor.column);
  const cy = cursor.row < pinned ? HEADER : top + (cursor.row - pinned) * ROW - scroll.top;
  context.strokeStyle = palette.focus;
  context.lineWidth = 2;
  context.strokeRect(cx + 1, 1, CELL - 2, RULER - 2);
  if (cursor.row < pinned || cy >= top) context.strokeRect(cx + 1, cy + 1, CELL - 2, ROW - 2);
  context.restore();
}
