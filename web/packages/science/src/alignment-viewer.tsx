/** Virtualized canvas and accessible keyboard cursor for an already aligned set of sequences. */
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
import { ReactElement } from "./react-element.js";
import { viewerThemeUrl } from "./viewer-theme.js";
import {
  colourOf,
  isGap,
  KINDS,
  moved,
  residueNumber,
  type Colouring,
  type Kind,
} from "./alignment.js";
import { compared, possibleOf, stepped } from "./alignment-compare.js";
import type { AlignmentRow, Reading } from "./parsers.js";

export { colourOf, moved, residueNumber } from "./alignment.js";
export type Aligned = Extract<Reading, { kind: "alignment" }>;
export interface AlignmentPosition {
  row: number;
  column: number;
}
export interface AlignmentLabel {
  text: string;
  description?: string;
  href?: string;
  selectionKey?: string;
}
export interface AlignmentData {
  alignment: Aligned;
  title: string;
  /** Optional labels in the same order as the file rows. */
  labels?: readonly (AlignmentLabel | null)[];
  /** One-based file row to pin and compare the other rows against. */
  referenceRow?: number;
  /** Initial one-based file row and column. */
  position?: AlignmentPosition;
}
export interface AlignmentOpen {
  row: number;
  href: string;
}
interface Shown {
  index: number;
  row: AlignmentRow;
  label: AlignmentLabel | null;
}
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
    surface: v("--surface", "Canvas"),
    band: v("--surface-2", "ButtonFace"),
    text: v("--text", "CanvasText"),
    muted: v("--muted", "CanvasText"),
    border: v("--border", "GrayText"),
    focus: v("--focus", "Highlight"),
    accent: v("--accent", "LinkText"),
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
  data,
  picked,
  onPick,
  onPosition,
  onOpen,
}: {
  data: AlignmentData;
  picked: readonly string[];
  onPick: (keys: string[]) => void;
  onPosition: (position: AlignmentPosition) => void;
  onOpen: (detail: AlignmentOpen) => boolean;
}) {
  const { alignment, title, position } = data;
  const all = useMemo(
    () =>
      alignment.rows.map((row, index) => ({
        index,
        row,
        label: data.labels?.[index] ?? null,
      })),
    [alignment.rows, data.labels],
  );
  const reference = data.referenceRow ? all[data.referenceRow - 1] : undefined;
  const [styleLoaded, setStyleLoaded] = useState(false);
  // The reference first, pinned; the others in the file's order.
  const rows = useMemo(
    () => (reference ? [reference, ...all.filter((r) => r !== reference)] : all),
    [all, reference],
  );
  const pinned = reference ? 1 : 0;
  // Compared with the reference, when there is one: what the others cover, and where they
  // differ. Clustal's identical columns say little of reads, which cover only part of it.
  const comparison = useMemo(
    () => (reference ? compared(alignment.rows, reference.row, alignment.alphabet) : null),
    [alignment.rows, alignment.alphabet, reference],
  );
  const top = HEADER + pinned * ROW;
  const length = alignment.length;

  const [cursor, setCursor] = useState(() => {
    const row = position ? rows.findIndex((r) => r.index === position.row - 1) : 0;
    const column = position ? Math.min(Math.max(position.column - 1, 0), length - 1) : 0;
    return { row: Math.max(row, 0), column };
  });
  const [colouring, setColouring] = useState<Colouring>("agreeing");
  const [differences, setDifferences] = useState(false);
  /** The difference last gone to, and whether getting there went round past an end. */
  const [found, setFound] = useState<{ index: number; round: "start" | "end" | null } | null>(null);
  const [scroll, setScroll] = useState<Scroll>({ left: 0, top: 0, width: 0, height: 0 });
  const region = useRef<HTMLDivElement | null>(null);
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const scheme = useScheme();
  const ids = useId();

  const cursorRow = rows[cursor.row];
  useEffect(() => {
    if (cursorRow && length > 0)
      onPosition({ column: cursor.column + 1, row: cursorRow.index + 1 });
  }, [onPosition, cursor.column, cursorRow, length]);

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
  }, [
    alignment,
    rows,
    pinned,
    scroll,
    cursor,
    colouring,
    differences,
    reference,
    scheme,
    styleLoaded,
  ]);

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

  // The columns the summary counts as differing, so stepping goes to just those.
  const differing = comparison?.columns ?? [];

  /** Goes to the `index`th differing column, on the cursor's row, and says which it is. */
  function toDifference(index: number, round: "start" | "end" | null = null) {
    const column = differing[index];
    if (column === undefined) return;
    moveTo({ row: cursor.row, column }, true);
    // With only one, going round to it again isn't news.
    setFound({ index, round: differing.length > 1 ? round : null });
  }

  /** Goes to the next differing column after the cursor's, or the last before it, going
   * round past the end or the start as find on a page does. */
  function step(direction: 1 | -1) {
    const next = stepped(differing, cursor.column, direction);
    if (!next) return;
    toDifference(next.index, next.wrapped ? (direction === 1 ? "start" : "end") : null);
  }

  function keyed(event: KeyboardEvent<HTMLDivElement>) {
    if (event.target !== event.currentTarget) return;
    const plain = !event.ctrlKey && !event.metaKey && !event.altKey;
    if (plain && differing.length && event.key.toLowerCase() === "n") {
      event.preventDefault();
      step(event.shiftKey ? -1 : 1);
      return;
    }
    const page = Math.max(Math.floor((scroll.height - top) / ROW) - 1, 1);
    const next = moved(event.key, cursor, rows.length, length, page);
    if (!next) return;
    event.preventDefault();
    setFound(null);
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
    setFound(null);
    setCursor({ row, column });
    el.focus();
  }

  // The labels in view, as links and checkboxes: the canvas draws everything else.
  const first = Math.max(Math.floor(scroll.top / ROW) - 2, 0);
  const shown = Math.ceil((scroll.height || 600) / ROW) + 4;
  const body = rows.slice(pinned + first, pinned + first + shown);
  const pickedSet = new Set(picked);
  const toggle = (key: string) => {
    onPick(pickedSet.has(key) ? picked.filter((value) => value !== key) : [...picked, key]);
  };
  const identity = length ? alignment.identical / length : 0;
  const atRow = rows[cursor.row];
  const residue = atRow?.row.aligned.charAt(cursor.column) ?? "";
  const residueAt = atRow
    ? residueNumber(atRow.row.aligned, cursor.column, atRow.row.start, atRow.row.reverse)
    : null;
  /** What the residue at the cursor could be, where its row's header says (`possible=`). */
  const could = atRow ? possibleOf(atRow.row.description).get(cursor.column) : undefined;
  const couldBe = could
    ? ` (${could
        .split("")
        .map((r) => (r === "*" ? "a stop" : r))
        .join(" or ")})`
    : "";
  /** Which part of its sequence a row is, first residue to last: down, on its other strand. */
  const span = (r: Shown) => {
    const last = r.row.reverse
      ? r.row.start - r.row.residues + 1
      : r.row.start + r.row.residues - 1;
    return `${number.format(r.row.start)}–${number.format(last)}${r.row.reverse ? ", its reverse strand" : ""}`;
  };
  /** A row's residues: their count, or which part of its sequence they are. */
  const extent = (r: Shown) =>
    !r.row.spanned ? `of ${number.format(r.row.residues)}` : `of ${span(r)}`;
  const labelOf = (r: Shown) => r.label?.text ?? r.row.id;

  return (
    <>
      <link rel="stylesheet" href={viewerThemeUrl} />
      <link
        rel="stylesheet"
        href={new URL("./alignment-viewer.css?no-inline", import.meta.url).href}
        onLoad={() => {
          setStyleLoaded(true);
        }}
      />
      {reference && comparison ? (
        <p className="muted">
          {number.format(alignment.rows.length)} rows, {number.format(length)} columns · the others
          cover {number.format(comparison.covered)} of {labelOf(reference)}’s{" "}
          {!reference.row.spanned
            ? `${number.format(comparison.residues)} residues`
            : `residues ${span(reference)}`}{" "}
          ({percent.format(comparison.residues ? comparison.covered / comparison.residues : 0)}) and
          differ from it at{" "}
          {comparison.differing ? (
            <button
              type="button"
              className="alignment-jump"
              aria-label={
                comparison.differing === 1
                  ? "Go to the 1 column where they differ"
                  : `Go to the first of the ${number.format(comparison.differing)} columns where they differ`
              }
              onClick={() => {
                toDifference(0);
                region.current?.focus();
              }}
            >
              {number.format(comparison.differing)}{" "}
              {comparison.differing === 1 ? "column" : "columns"}
            </button>
          ) : (
            `${number.format(0)} columns`
          )}
        </p>
      ) : (
        <p className="muted">
          {number.format(alignment.rows.length)} rows, {number.format(length)} columns ·{" "}
          {number.format(alignment.identical)} identical ({percent.format(identity)}) ·{" "}
          {number.format(alignment.gapped)} with gaps
        </p>
      )}
      {alignment.alphabet_guessed ? (
        <p className="muted">
          Read as {alignment.alphabet === "nucleotide" ? "nucleotides" : "amino acids"} from its
          letters: the file doesn’t say which it holds.
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
        {differing.length ? (
          <div className="alignment-steps">
            <button
              type="button"
              className="button"
              onClick={() => {
                step(-1);
              }}
            >
              Previous difference
            </button>
            <button
              type="button"
              className="button"
              onClick={() => {
                step(1);
              }}
            >
              Next difference
            </button>
            <p role="status">
              {found
                ? `Difference ${number.format(found.index + 1)} of ${number.format(differing.length)}: column ${number.format((differing[found.index] ?? 0) + 1)}${found.round ? `, back at the ${found.round}` : ""}.`
                : null}
            </p>
          </div>
        ) : null}
        <form
          className="alignment-go"
          onSubmit={(event) => {
            event.preventDefault();
            const data = new FormData(event.currentTarget);
            const column = Number(
              ((data.get("column") as string | null) ?? "").replaceAll(/[\s,]/g, ""),
            );
            if (!Number.isInteger(column) || column < 1 || column > length) return;
            setFound(null);
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
          <button type="submit" className="button">
            Go
          </button>
        </form>
      </div>
      {/* A widget of its own: its keys move a cursor across the canvas, whose place is read
          out below it, and it scrolls, so it takes focus (as scrolling regions must). */}
      <div
        ref={region}
        className="alignment-scroll"
        role="application"
        aria-roledescription="alignment"
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
                  onOpen={onOpen}
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
                onOpen={onOpen}
              />
            ))}
          </div>
        </div>
      </div>
      <p id={`${ids}-keys`} className="visually-hidden">
        The arrow keys move along the alignment; Home and End go to a row’s first and last column.
        {reference && differing.length
          ? ` N and Shift+N go to the next and previous column where they differ from ${labelOf(reference)}.`
          : null}
      </p>
      <p id={`${ids}-at`} className="alignment-at" aria-live="polite">
        {atRow ? (
          <>
            <strong>{labelOf(atRow)}</strong>, column {number.format(cursor.column + 1)}:{" "}
            {residueAt === null
              ? "a gap"
              : `${residue}${couldBe}, residue ${number.format(residueAt)} ${extent(atRow)}`}
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
  onOpen,
}: {
  shown: Shown;
  top: number;
  pinned?: boolean;
  picked: Set<string>;
  onToggle: (key: string) => void;
  onOpen: (detail: AlignmentOpen) => boolean;
}) {
  const { label, row } = shown;
  // Only document and web links are meaningful for a row label.
  const url =
    label?.href && URL.canParse(label.href, document.baseURI)
      ? new URL(label.href, document.baseURI)
      : null;
  const href =
    url && (url.protocol === "http:" || url.protocol === "https:") ? label?.href : undefined;
  const key = label?.selectionKey;
  return (
    <div
      className={pinned ? "alignment-label alignment-label-pinned" : "alignment-label"}
      style={{ top: `${String(top)}px`, height: `${String(ROW)}px` }}
    >
      {label ? (
        <>
          {key !== undefined ? (
            <input
              type="checkbox"
              checked={picked.has(key)}
              aria-label={`Select ${label.text}`}
              onChange={() => {
                onToggle(key);
              }}
            />
          ) : null}
          {href ? (
            <a
              href={href}
              title={label.description}
              onClick={(event) => {
                if (
                  event.button !== 0 ||
                  event.ctrlKey ||
                  event.metaKey ||
                  event.altKey ||
                  event.shiftKey
                )
                  return;
                if (!onOpen({ row: shown.index + 1, href })) event.preventDefault();
              }}
            >
              {label.text}
            </a>
          ) : (
            <span>{label.text}</span>
          )}
          <span className="muted">{label.description}</span>
        </>
      ) : (
        <span title={row.description || row.id}>{row.id}</span>
      )}
      {pinned ? <span className="alignment-pin">Reference</span> : null}
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
      // A place with no observation is drawn empty, apart from a gap.
      if (residue === ".") continue;
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

  // The cursor, and its column marked under the ruler's numbers (a box would hide them).
  const cx = xOf(cursor.column);
  const cy = cursor.row < pinned ? HEADER : top + (cursor.row - pinned) * ROW - scroll.top;
  context.fillStyle = palette.focus;
  context.fillRect(cx + 1, RULER - 4, CELL - 2, 4);
  context.strokeStyle = palette.focus;
  context.lineWidth = 2;
  if (cursor.row < pinned || cy >= top) context.strokeRect(cx + 1, cy + 1, CELL - 2, ROW - 2);
  context.restore();
}

export class AlignmentViewerElement extends ReactElement {
  #data: AlignmentData | null = null;
  #selection: readonly string[] = [];
  #position: AlignmentPosition | null = null;
  #revision = 0;

  get data(): AlignmentData | null {
    return this.#data;
  }
  set data(value: AlignmentData | null) {
    if (value === this.#data) return;
    this.#data = value;
    this.#selection = [];
    this.#position = null;
    this.#revision++;
    this.invalidate();
  }
  get position(): AlignmentPosition | null {
    return this.#position;
  }
  get selection(): readonly string[] {
    return this.#selection;
  }
  set selection(value: readonly string[]) {
    this.#selection = [...new Set(value)];
    this.invalidate();
  }

  readonly #reportSelection = (keys: string[]) => {
    this.selection = keys;
    this.dispatchEvent(
      new CustomEvent("selection-change", {
        detail: [...this.selection],
        bubbles: true,
        composed: true,
      }),
    );
  };
  readonly #reportPosition = (position: AlignmentPosition) => {
    this.#position = position;
    this.dispatchEvent(
      new CustomEvent("position-change", { detail: position, bubbles: true, composed: true }),
    );
  };
  readonly #open = (detail: AlignmentOpen) =>
    this.dispatchEvent(
      new CustomEvent("row-open", { detail, cancelable: true, bubbles: true, composed: true }),
    );

  protected override renderView() {
    if (!this.#data) return null;
    if (this.#data.alignment.length > MAX_COLUMNS) {
      return <p>The alignment exceeds the viewer’s {number.format(MAX_COLUMNS)} column limit.</p>;
    }
    return (
      <AlignmentGrid
        key={this.#revision}
        data={this.#data}
        picked={this.#selection}
        onPick={this.#reportSelection}
        onPosition={this.#reportPosition}
        onOpen={this.#open}
      />
    );
  }
}

if (!customElements.get("iki-alignment-viewer"))
  customElements.define("iki-alignment-viewer", AlignmentViewerElement);
declare global {
  interface HTMLElementTagNameMap {
    "iki-alignment-viewer": AlignmentViewerElement;
  }
}
