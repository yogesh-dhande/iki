/**
 * A Sanger chromatogram from an ABIF (.ab1) file (ADR 0226): its four traces, the bases the
 * basecaller called at its peaks, and each call's quality, read in the previews' worker by
 * iki-previews' ABIF reader (Applied Biosystems' ABIF specification). The trace scrolls
 * sideways; only the part in view is drawn. The calls are given as text too, with their
 * qualities, so the sequence can be read without the picture.
 */

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import { Button, Loading } from "../ui";
import type { ViewerProps } from "./file-preview";
import { count, plural, PreviewNote, Verified } from "./notes";
import type { Chromatogram } from "./readings";
import { ReadFailed, useRead } from "./use-read";
import { watchRedraw } from "./watch";

const BASES = ["A", "C", "G", "T"] as const;
type Base = (typeof BASES)[number];
/** Pixels per trace point, from far out to close in. */
const SCALES = [0.5, 1, 2, 3, 4, 6] as const;
const HEIGHT = 260;
/** Room above the traces for the calls and their quality bars. */
const CALLS = 44;
/** Phred quality's bar height is out of this. */
const MAX_QUALITY = 60;

export interface Summary {
  bases: number;
  mean: number | null;
  q20: number | null;
  q30: number | null;
}

/** Its length and how many calls reach Q20 and Q30: what a sequencing core reports. */
export function summarise(chromatogram: Chromatogram): Summary {
  const quality = chromatogram.quality;
  const bases = chromatogram.bases.length;
  if (!quality?.length) return { bases, mean: null, q20: null, q30: null };
  let sum = 0;
  let q20 = 0;
  let q30 = 0;
  for (const q of quality) {
    sum += q;
    if (q >= 20) q20++;
    if (q >= 30) q30++;
  }
  return { bases, mean: sum / quality.length, q20, q30 };
}

/** The calls in lines of 60, numbered as a sequence is, for reading and copying. */
export function sequenceLines(bases: string, width = 60): { start: number; text: string }[] {
  const lines: { start: number; text: string }[] = [];
  for (let i = 0; i < bases.length; i += width) {
    const chunk = bases.slice(i, i + width);
    lines.push({ start: i + 1, text: chunk.replace(/(.{10})(?=.)/g, "$1 ") });
  }
  return lines;
}

export default function ChromatogramViewer({ file, stored, reader }: ViewerProps) {
  const read = useRead(
    async (signal) => {
      const bytes = new Uint8Array(await stored.whole(signal));
      const { value } = await reader.read("abif", [], bytes);
      return value;
    },
    [stored, reader],
  );
  if (read.phase === "failed") return <ReadFailed file={file} failed={read} />;
  if (read.phase === "reading") return <Loading label={`Reading ${file.name}…`} />;
  return <Shown name={file.name} chromatogram={read.value} />;
}

function Shown({ name, chromatogram }: { name: string; chromatogram: Chromatogram }) {
  const summary = useMemo(() => summarise(chromatogram), [chromatogram]);
  const empty = chromatogram.points === 0;
  return (
    <div className="preview-chromatogram">
      <dl className="preview-facts">
        {chromatogram.sample ? (
          <div>
            <dt>Sample</dt>
            <dd>{chromatogram.sample}</dd>
          </div>
        ) : null}
        <div>
          <dt>Bases called</dt>
          <dd>{count(summary.bases)}</dd>
        </div>
        {summary.mean !== null ? (
          <>
            <div>
              <dt>Mean quality</dt>
              <dd>Q{summary.mean.toFixed(1)}</dd>
            </div>
            <div>
              <dt>Q20 or better</dt>
              <dd>{count(summary.q20 ?? 0)}</dd>
            </div>
            <div>
              <dt>Q30 or better</dt>
              <dd>{count(summary.q30 ?? 0)}</dd>
            </div>
          </>
        ) : null}
        {chromatogram.instrument ? (
          <div>
            <dt>Instrument</dt>
            <dd>{chromatogram.instrument}</dd>
          </div>
        ) : null}
      </dl>
      {empty ? (
        <p>{name} has no traces to draw.</p>
      ) : (
        <Trace name={name} chromatogram={chromatogram} />
      )}
      {chromatogram.bases ? <Sequence chromatogram={chromatogram} /> : null}
      <PreviewNote>
        {chromatogram.quality ? "" : "The file gives no qualities for its calls. "}
        <Verified />
      </PreviewNote>
      {chromatogram.problems.length ? (
        <ul className="preview-problems">
          {chromatogram.problems.slice(0, 20).map((p, i) => (
            <li key={i}>{p}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function isBase(call: string): call is Base {
  return (BASES as readonly string[]).includes(call);
}

function css(element: Element, name: string, fallback: string): string {
  return getComputedStyle(element).getPropertyValue(name).trim() || fallback;
}

function Trace({ name, chromatogram }: { name: string; chromatogram: Chromatogram }) {
  const scroller = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [scale, setScale] = useState<number>(2);
  const [gain, setGain] = useState(1);
  const [base, setBase] = useState("");
  const id = useId();
  const points = chromatogram.points;
  const peak = useMemo(() => {
    let max = 1;
    for (const b of BASES) for (const v of chromatogram.traces[b]) if (v > max) max = v;
    return max;
  }, [chromatogram]);

  const redraw = useCallback(() => {
    const frame = scroller.current;
    const target = canvas.current;
    const context = target?.getContext("2d");
    if (!frame || !target || !context) return;
    const ratio = window.devicePixelRatio || 1;
    const width = frame.clientWidth;
    target.style.width = `${String(width)}px`;
    target.style.height = `${String(HEIGHT)}px`;
    target.width = Math.round(width * ratio);
    target.height = Math.round(HEIGHT * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, HEIGHT);
    const left = frame.scrollLeft;
    const from = Math.max(0, Math.floor(left / scale) - 1);
    const to = Math.min(points, Math.ceil((left + width) / scale) + 1);
    const plot = HEIGHT - CALLS - 4;
    const y = (v: number) => CALLS + plot - Math.min(1, (v / peak) * gain) * plot;
    const colours: Record<Base, string> = {
      A: css(target, "--trace-a", "#1e8a3c"),
      C: css(target, "--trace-c", "#1f5fbf"),
      G: css(target, "--trace-g", "#1d1d1f"),
      T: css(target, "--trace-t", "#c62828"),
    };
    context.lineWidth = 1.25;
    for (const b of BASES) {
      const trace = chromatogram.traces[b];
      context.strokeStyle = colours[b];
      context.beginPath();
      // Far out, a pixel holds several points: draw each column's extent.
      const step = Math.max(1, Math.floor(1 / scale));
      for (let i = from; i < to; i += step) {
        let v = trace[i] ?? 0;
        for (let j = 1; j < step; j++) v = Math.max(v, trace[i + j] ?? 0);
        const x = i * scale - left;
        if (i === from) context.moveTo(x, y(v));
        else context.lineTo(x, y(v));
      }
      context.stroke();
    }
    // The calls and, under each, its quality as a bar.
    const font = getComputedStyle(target).fontFamily || "monospace";
    const muted = css(target, "--muted", "#666");
    const grid = css(target, "--border", "#ddd");
    context.textAlign = "center";
    context.textBaseline = "top";
    const spacing =
      chromatogram.peaks.length > 1 ? (points / chromatogram.peaks.length) * scale : 12;
    const labelled = spacing >= 7;
    context.font = `${spacing >= 10 ? "12" : "10"}px ${font}`;
    const first = lowerBound(chromatogram.peaks, from);
    for (let i = first; i < chromatogram.peaks.length; i++) {
      const at = chromatogram.peaks[i] ?? 0;
      if (at > to) break;
      const x = at * scale - left;
      const call = chromatogram.bases[i] ?? "N";
      const q = chromatogram.quality?.[i];
      if (q !== undefined) {
        const h = (Math.min(q, MAX_QUALITY) / MAX_QUALITY) * 20;
        context.fillStyle = grid;
        context.fillRect(
          x - Math.max(1, spacing * 0.35),
          22 + (20 - h),
          Math.max(2, spacing * 0.7),
          h,
        );
      }
      if (labelled) {
        context.fillStyle = isBase(call) ? colours[call] : muted;
        context.fillText(call, x, 2);
        if (spacing >= 28 && (i + 1) % 10 === 0) {
          context.fillStyle = muted;
          context.font = `10px ${font}`;
          context.fillText(String(i + 1), x, CALLS - 2);
          context.font = `${spacing >= 10 ? "12" : "10"}px ${font}`;
        }
      }
    }
  }, [chromatogram, scale, gain, peak, points]);

  useEffect(() => {
    redraw();
    const frame = scroller.current;
    if (!frame) return;
    return watchRedraw(frame, redraw);
  }, [redraw]);

  const goTo = (position: number) => {
    const frame = scroller.current;
    const at = chromatogram.peaks[position - 1];
    if (!frame || at === undefined) return;
    frame.scrollLeft = Math.max(0, at * scale - frame.clientWidth / 2);
  };
  const rescale = (next: number) => {
    const frame = scroller.current;
    if (frame) {
      // Keep the middle of what's in view where it is.
      const middle = (frame.scrollLeft + frame.clientWidth / 2) / scale;
      setScale(next);
      requestAnimationFrame(() => {
        frame.scrollLeft = Math.max(0, middle * next - frame.clientWidth / 2);
      });
    } else setScale(next);
  };
  const scaleIndex = SCALES.indexOf(scale as (typeof SCALES)[number]);
  const onKey = (event: KeyboardEvent) => {
    const frame = scroller.current;
    if (!frame) return;
    const moves: Record<string, number> = {
      ArrowLeft: -frame.clientWidth * 0.1,
      ArrowRight: frame.clientWidth * 0.1,
      PageUp: -frame.clientWidth * 0.9,
      PageDown: frame.clientWidth * 0.9,
    };
    if (event.key in moves) {
      event.preventDefault();
      frame.scrollLeft += moves[event.key] ?? 0;
    } else if (event.key === "Home") {
      event.preventDefault();
      frame.scrollLeft = 0;
    } else if (event.key === "End") {
      event.preventDefault();
      frame.scrollLeft = frame.scrollWidth;
    } else if ((event.key === "+" || event.key === "=") && scaleIndex < SCALES.length - 1) {
      event.preventDefault();
      rescale(SCALES[scaleIndex + 1] ?? scale);
    } else if (event.key === "-" && scaleIndex > 0) {
      event.preventDefault();
      rescale(SCALES[scaleIndex - 1] ?? scale);
    }
  };
  const legend = BASES.join(", ");
  return (
    <>
      <div className="preview-toolbar" role="toolbar" aria-label={`Trace of ${name}`}>
        <Button
          onClick={() => {
            rescale(SCALES[scaleIndex + 1] ?? scale);
          }}
          disabled={scaleIndex >= SCALES.length - 1}
        >
          Zoom in
        </Button>
        <Button
          onClick={() => {
            rescale(SCALES[scaleIndex - 1] ?? scale);
          }}
          disabled={scaleIndex <= 0}
        >
          Zoom out
        </Button>
        <span className="preview-zoom">
          <label htmlFor={`${id}-gain`}>Peak height</label>
          <select
            id={`${id}-gain`}
            value={gain}
            onChange={(event) => {
              setGain(Number(event.target.value));
            }}
          >
            {[1, 2, 4, 8].map((g) => (
              <option key={g} value={g}>
                ×{g}
              </option>
            ))}
          </select>
        </span>
        {chromatogram.bases ? (
          <form
            className="preview-page-field"
            onSubmit={(event) => {
              event.preventDefault();
              goTo(Number(base));
            }}
          >
            <label htmlFor={`${id}-base`}>Go to base</label>
            <input
              id={`${id}-base`}
              className="preview-page-input"
              type="number"
              inputMode="numeric"
              min={1}
              max={chromatogram.bases.length}
              value={base}
              onChange={(event) => {
                setBase(event.target.value);
              }}
            />
            <Button type="submit">Go</Button>
          </form>
        ) : null}
        <span className="preview-legend" aria-hidden="true">
          {BASES.map((b) => (
            <span key={b} className={`preview-legend-${b.toLowerCase()}`}>
              {b}
            </span>
          ))}
        </span>
      </div>
      {/* A scrolling region people reach with Tab, so the keyboard scrolls and zooms it. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- its keys scroll and zoom */}
      <div
        ref={scroller}
        className="preview-scroll"
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable region
        tabIndex={0}
        role="img"
        aria-label={`Chromatogram of ${name}: ${plural(points, "trace point", "trace points")} for each of ${legend}, with ${plural(chromatogram.bases.length, "base", "bases")} called. The calls and their qualities follow as text.`}
        onKeyDown={onKey}
        onScroll={redraw}
      >
        <div
          className="preview-track"
          style={{
            width: `${String(Math.max(1, points * scale))}px`,
            height: `${String(HEIGHT)}px`,
          }}
        >
          <canvas ref={canvas} className="preview-canvas" />
        </div>
      </div>
    </>
  );
}

function lowerBound(values: number[], target: number): number {
  let lo = 0;
  let hi = values.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if ((values[mid] ?? 0) < target) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** The calls as text, numbered, and each call's quality in a table. */
function Sequence({ chromatogram }: { chromatogram: Chromatogram }) {
  const lines = useMemo(() => sequenceLines(chromatogram.bases), [chromatogram.bases]);
  const low = useMemo(() => {
    const quality = chromatogram.quality;
    if (!quality) return [];
    const out: number[] = [];
    quality.forEach((q, i) => {
      if (q < 20) out.push(i);
    });
    return out;
  }, [chromatogram.quality]);
  return (
    <details className="disclosure preview-data">
      <summary>{`Its ${plural(chromatogram.bases.length, "base", "bases")} as text`}</summary>
      <pre className="preview-bases">
        {lines.map((line) => `${String(line.start).padStart(6)}  ${line.text}`).join("\n")}
      </pre>
      {chromatogram.quality ? (
        <p className="muted">
          {low.length
            ? `${plural(low.length, "call is", "calls are")} below Q20, the usual cut-off for a call to trust.`
            : "Every call is Q20 or better."}
        </p>
      ) : null}
    </details>
  );
}
