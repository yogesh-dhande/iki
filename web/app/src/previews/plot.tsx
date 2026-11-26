/**
 * A spectrum or chromatogram plot for previews (ADR 0226): a line for a continuous
 * spectrum, sticks for peaks, drawn on a canvas a pixel column at a time (each column's
 * least and greatest value), so a million points draw as quickly as a hundred. Drag across
 * it to zoom to a range; the keyboard zooms (+, −), pans (arrows) and resets (0), and the
 * buttons do the same. What it shows is said in words too: its range and highest peaks.
 */

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
} from "react";

import { Button } from "../ui";
import { watchRedraw } from "./watch";
import { count } from "./notes";

export interface Series {
  x: ArrayLike<number>;
  y: ArrayLike<number>;
  /** A continuous trace, or separate peaks. */
  kind: "line" | "sticks";
}

/** Points sorted by x, ascending, whatever order the file had them in. */
export interface Sorted {
  x: Float64Array;
  y: Float64Array;
  kind: Series["kind"];
}

export function sortSeries(series: Series): Sorted {
  const n = Math.min(series.x.length, series.y.length);
  const order = Array.from({ length: n }, (_, i) => i);
  let ascending = true;
  for (let i = 1; i < n; i++) {
    if ((series.x[i] ?? 0) < (series.x[i - 1] ?? 0)) {
      ascending = false;
      break;
    }
  }
  if (!ascending) order.sort((a, b) => (series.x[a] ?? 0) - (series.x[b] ?? 0));
  const x = new Float64Array(n);
  const y = new Float64Array(n);
  order.forEach((from, to) => {
    x[to] = series.x[from] ?? 0;
    y[to] = series.y[from] ?? 0;
  });
  return { x, y, kind: series.kind };
}

/** The first index with x ≥ value. */
export function lowerBound(x: Float64Array, value: number): number {
  let lo = 0;
  let hi = x.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if ((x[mid] ?? 0) < value) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** About `target` round tick values across [min, max]: 1, 2 or 5 times a power of ten. */
export function ticks(min: number, max: number, target = 6): number[] {
  if (!(max > min) || !Number.isFinite(min) || !Number.isFinite(max)) return [min];
  const raw = (max - min) / target;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * power).find((s) => s >= raw) ?? power * 10;
  const out: number[] = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) {
    out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    if (out.length > 50) break;
  }
  return out;
}

const compact = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 4 });
const exact = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 8 });

/** A tick's label: plain where it fits, exponents for very large or small values. */
export function tickLabel(value: number): string {
  const size = Math.abs(value);
  if (size !== 0 && (size >= 1e6 || size < 1e-3)) return value.toExponential(1).replace("e+", "e");
  return compact.format(value);
}

export function exactNumber(value: number): string {
  return exact.format(value);
}

/** The n highest points, by y, highest first. */
export function highest(sorted: Sorted, n: number, from = 0, to = sorted.x.length): number[] {
  const indices: number[] = [];
  for (let i = from; i < to; i++) {
    const y = sorted.y[i] ?? 0;
    if (indices.length < n) {
      indices.push(i);
      if (indices.length === n) indices.sort((a, b) => (sorted.y[b] ?? 0) - (sorted.y[a] ?? 0));
    } else if (y > (sorted.y[indices[n - 1] ?? 0] ?? 0)) {
      indices[n - 1] = i;
      indices.sort((a, b) => (sorted.y[b] ?? 0) - (sorted.y[a] ?? 0));
    }
  }
  return indices.sort((a, b) => (sorted.y[b] ?? 0) - (sorted.y[a] ?? 0));
}

const MARGIN = { left: 64, right: 16, top: 12, bottom: 40 };

interface View {
  x0: number;
  x1: number;
}

function color(element: Element, name: string, fallback: string): string {
  return getComputedStyle(element).getPropertyValue(name).trim() || fallback;
}

function draw(
  canvas: HTMLCanvasElement,
  sorted: Sorted,
  view: View,
  labels: { x: string; y: string },
  reversed: boolean,
  selection: View | null,
): void {
  const context = canvas.getContext("2d");
  if (!context) return;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  const line = color(canvas, "--plot-line", "#2563eb");
  const grid = color(canvas, "--plot-grid", "#ddd");
  const text = color(canvas, "--plot-text", "#666");
  const font = getComputedStyle(canvas).fontFamily || "sans-serif";
  const plotW = Math.max(10, width - MARGIN.left - MARGIN.right);
  const plotH = Math.max(10, height - MARGIN.top - MARGIN.bottom);

  const from = lowerBound(sorted.x, view.x0);
  const to = lowerBound(sorted.x, view.x1 + Number.EPSILON * Math.abs(view.x1));
  let yMin = 0;
  let yMax = 0;
  for (let i = from; i < Math.min(to + 1, sorted.y.length); i++) {
    const y = sorted.y[i] ?? 0;
    if (y > yMax) yMax = y;
    if (y < yMin) yMin = y;
  }
  if (yMax === yMin) yMax = yMin + 1;
  yMax += (yMax - yMin) * 0.05;
  const span = view.x1 - view.x0 || 1;
  const px = (x: number) =>
    MARGIN.left + (reversed ? (view.x1 - x) / span : (x - view.x0) / span) * plotW;
  const py = (y: number) => MARGIN.top + plotH - ((y - yMin) / (yMax - yMin)) * plotH;

  context.font = `12px ${font}`;
  context.lineWidth = 1;
  context.strokeStyle = grid;
  context.fillStyle = text;
  context.textAlign = "center";
  context.textBaseline = "top";
  for (const t of ticks(view.x0, view.x1)) {
    const x = px(t);
    context.beginPath();
    context.moveTo(x, MARGIN.top);
    context.lineTo(x, MARGIN.top + plotH);
    context.stroke();
    context.fillText(tickLabel(t), x, MARGIN.top + plotH + 6);
  }
  context.textAlign = "right";
  context.textBaseline = "middle";
  for (const t of ticks(yMin, yMax, 5)) {
    const y = py(t);
    context.beginPath();
    context.moveTo(MARGIN.left, y);
    context.lineTo(MARGIN.left + plotW, y);
    context.stroke();
    context.fillText(tickLabel(t), MARGIN.left - 6, y);
  }
  context.textAlign = "center";
  context.textBaseline = "bottom";
  context.fillText(labels.x, MARGIN.left + plotW / 2, height - 2);
  context.save();
  context.translate(12, MARGIN.top + plotH / 2);
  context.rotate(-Math.PI / 2);
  context.textBaseline = "top";
  context.fillText(labels.y, 0, -8);
  context.restore();

  context.save();
  context.beginPath();
  context.rect(MARGIN.left, MARGIN.top, plotW, plotH);
  context.clip();
  context.strokeStyle = line;
  // Sticks a pixel wide on a pixel's centre, so each is crisp and the same colour.
  context.lineWidth = sorted.kind === "sticks" ? 1 : 1.25;
  context.beginPath();
  const baseline = py(Math.max(0, yMin));
  // Each pixel column draws its points' extent: a line joins columns, sticks rise from 0.
  const lo = Math.max(0, from - 1);
  const hi = Math.min(sorted.x.length, to + 1);
  let column = Number.NaN;
  let cMin = 0;
  let cMax = 0;
  let first = true;
  const flush = () => {
    if (Number.isNaN(column)) return;
    if (sorted.kind === "sticks") {
      context.moveTo(column + 0.5, baseline);
      context.lineTo(column + 0.5, py(cMax));
    } else {
      if (first) context.moveTo(column, py(cMin));
      else context.lineTo(column, py(cMin));
      context.lineTo(column, py(cMax));
      first = false;
    }
  };
  for (let i = lo; i < hi; i++) {
    const x = Math.round(px(sorted.x[i] ?? 0));
    const y = sorted.y[i] ?? 0;
    if (x !== column) {
      flush();
      column = x;
      cMin = y;
      cMax = y;
    } else {
      if (y < cMin) cMin = y;
      if (y > cMax) cMax = y;
    }
  }
  flush();
  context.stroke();
  if (selection) {
    const a = px(selection.x0);
    const b = px(selection.x1);
    context.fillStyle = line;
    context.globalAlpha = 0.15;
    context.fillRect(Math.min(a, b), MARGIN.top, Math.abs(b - a), plotH);
    context.globalAlpha = 1;
  }
  context.restore();
}

export function Plot({
  series,
  xLabel,
  yLabel,
  reversed = false,
  label,
  height = 320,
}: {
  series: Series;
  xLabel: string;
  yLabel: string;
  /** Whether x runs from high to low, as IR wavenumbers and NMR shifts are drawn. */
  reversed?: boolean;
  /** What it is, for screen readers. */
  label: string;
  height?: number;
}) {
  const sorted = useMemo(() => sortSeries(series), [series]);
  const full = useMemo<View>(() => {
    const x0 = sorted.x[0] ?? 0;
    const x1 = sorted.x[sorted.x.length - 1] ?? 1;
    return x1 > x0 ? { x0, x1 } : { x0: x0 - 0.5, x1: x0 + 0.5 };
  }, [sorted]);
  // The view chosen of this series; a new series starts with all of it.
  const [chosen, setChosen] = useState<{ full: View; view: View }>({ full, view: full });
  const view = chosen.full === full ? chosen.view : full;
  const setView = (next: View) => {
    setChosen({ full, view: next });
  };
  const [selection, setSelection] = useState<View | null>(null);
  const [hover, setHover] = useState<number | null>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const drag = useRef<{ start: number } | null>(null);
  const id = useId();

  const redraw = useCallback(() => {
    if (canvas.current)
      draw(canvas.current, sorted, view, { x: xLabel, y: yLabel }, reversed, selection);
  }, [sorted, view, xLabel, yLabel, reversed, selection]);
  useEffect(() => {
    redraw();
    const element = canvas.current;
    if (!element) return;
    return watchRedraw(element, redraw);
  }, [redraw]);

  const xAt = (clientX: number): number => {
    const rect = canvas.current?.getBoundingClientRect();
    if (!rect) return view.x0;
    const plotW = Math.max(10, rect.width - MARGIN.left - MARGIN.right);
    const t = Math.min(1, Math.max(0, (clientX - rect.left - MARGIN.left) / plotW));
    const span = view.x1 - view.x0;
    return reversed ? view.x1 - t * span : view.x0 + t * span;
  };
  const clamp = (next: View): View => {
    const span = Math.max(next.x1 - next.x0, (full.x1 - full.x0) * 1e-6);
    let x0 = Math.max(full.x0, next.x0);
    let x1 = Math.min(full.x1, x0 + span);
    x0 = Math.max(full.x0, x1 - span);
    if (x1 <= x0) x1 = x0 + span;
    return { x0, x1 };
  };
  const zoom = (factor: number, around = (view.x0 + view.x1) / 2) => {
    const x0 = around - (around - view.x0) * factor;
    const x1 = around + (view.x1 - around) * factor;
    setView(clamp({ x0, x1 }));
  };
  const pan = (share: number) => {
    const shift = (view.x1 - view.x0) * share * (reversed ? -1 : 1);
    setView(clamp({ x0: view.x0 + shift, x1: view.x1 + shift }));
  };
  const onKey = (event: KeyboardEvent) => {
    const actions: Record<string, () => void> = {
      "+": () => {
        zoom(0.5);
      },
      "=": () => {
        zoom(0.5);
      },
      "-": () => {
        zoom(2);
      },
      _: () => {
        zoom(2);
      },
      "0": () => {
        setView(full);
      },
      ArrowLeft: () => {
        pan(-0.2);
      },
      ArrowRight: () => {
        pan(0.2);
      },
      Home: () => {
        setView(full);
      },
    };
    const action = actions[event.key];
    if (!action) return;
    event.preventDefault();
    action();
  };
  const onDown = (event: PointerEvent<HTMLCanvasElement>) => {
    if (event.button !== 0) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { start: xAt(event.clientX) };
  };
  const onMove = (event: PointerEvent<HTMLCanvasElement>) => {
    const x = xAt(event.clientX);
    if (drag.current) {
      setSelection({ x0: Math.min(drag.current.start, x), x1: Math.max(drag.current.start, x) });
    } else {
      const i = lowerBound(sorted.x, x);
      const before = sorted.x[i - 1];
      const after = sorted.x[i];
      const nearest =
        before !== undefined && (after === undefined || x - before < after - x) ? i - 1 : i;
      setHover(nearest < sorted.x.length ? nearest : null);
    }
  };
  const onUp = () => {
    if (selection && selection.x1 - selection.x0 > (view.x1 - view.x0) / 200) {
      setView(clamp(selection));
    }
    drag.current = null;
    setSelection(null);
  };

  const zoomed = view.x0 > full.x0 || view.x1 < full.x1;
  const shownFrom = lowerBound(sorted.x, view.x0);
  const shownTo = lowerBound(sorted.x, view.x1 + Number.EPSILON * Math.abs(view.x1));
  const top = highest(sorted, 1, shownFrom, Math.max(shownFrom, shownTo))[0];
  const description = `${label}: ${count(sorted.x.length)} points, ${xLabel} ${tickLabel(full.x0)} to ${tickLabel(full.x1)}${
    zoomed ? `, zoomed to ${tickLabel(view.x0)} to ${tickLabel(view.x1)}` : ""
  }${top !== undefined ? `. Highest shown: ${yLabel} ${tickLabel(sorted.y[top] ?? 0)} at ${tickLabel(sorted.x[top] ?? 0)}` : ""}.`;

  return (
    <div className="preview-plot-box">
      <div className="preview-toolbar" role="toolbar" aria-label={`${label} view`}>
        <Button
          onClick={() => {
            zoom(0.5);
          }}
        >
          Zoom in
        </Button>
        <Button
          onClick={() => {
            zoom(2);
          }}
          disabled={!zoomed}
        >
          Zoom out
        </Button>
        <Button
          onClick={() => {
            setView(full);
          }}
          disabled={!zoomed}
        >
          Show all
        </Button>
        <span className="muted" id={`${id}-help`}>
          Drag across the plot to zoom to a range.
        </span>
      </div>
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- its keys zoom and pan */}
      <div
        className="preview-plot"
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- its keys zoom and pan
        tabIndex={0}
        role="img"
        aria-label={description}
        aria-describedby={`${id}-help`}
        onKeyDown={onKey}
      >
        <canvas
          ref={canvas}
          className="preview-plot-canvas"
          style={{ height: `${String(height)}px` }}
          onPointerDown={onDown}
          onPointerMove={onMove}
          onPointerUp={onUp}
          onPointerCancel={onUp}
          onPointerLeave={() => {
            setHover(null);
          }}
          onDoubleClick={() => {
            setView(full);
          }}
        />
      </div>
      <p className="preview-note muted" aria-hidden="true">
        {hover !== null
          ? `${xLabel} ${exactNumber(sorted.x[hover] ?? 0)}, ${yLabel} ${exactNumber(sorted.y[hover] ?? 0)}`
          : " "}
      </p>
    </div>
  );
}

/** The highest points, as a table: the plot's values in words. */
export function Peaks({
  series,
  xLabel,
  yLabel,
  shown = 20,
}: {
  series: Series;
  xLabel: string;
  yLabel: string;
  shown?: number;
}) {
  const sorted = useMemo(() => sortSeries(series), [series]);
  const top = useMemo(() => highest(sorted, shown), [sorted, shown]);
  if (!top.length) return null;
  return (
    <details className="disclosure preview-data">
      <summary>{`Its ${String(top.length)} highest points`}</summary>
      <table className="list-table">
        <thead>
          <tr>
            <th scope="col">{xLabel}</th>
            <th scope="col">{yLabel}</th>
          </tr>
        </thead>
        <tbody>
          {top.map((i) => (
            <tr key={i}>
              <td>{exactNumber(sorted.x[i] ?? 0)}</td>
              <td>{exactNumber(sorted.y[i] ?? 0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}
