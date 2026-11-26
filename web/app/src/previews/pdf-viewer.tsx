/**
 * A PDF, page by page (ADR 0226), drawn by Mozilla's pdf.js. The file is read whole and
 * checked against its SHA-256, then handed to pdf.js's worker, one of the app's own (its
 * script gets the workers' policy: no `eval`, nothing from another origin). The main thread
 * paints each page on a canvas, with its text laid over it, so text can be selected and
 * read by screen readers. Nothing in the PDF runs: no scripts, forms or links are rendered.
 */

import {
  getDocument,
  InvalidPDFException,
  PasswordException,
  PDFWorker,
  TextLayer,
  type PDFDocumentProxy,
  type PDFPageProxy,
  type RenderTask,
} from "pdfjs-dist";
import jbig2 from "pdfjs-dist/wasm/jbig2.wasm?url";
import openjpeg from "pdfjs-dist/wasm/openjpeg.wasm?url";
import qcms from "pdfjs-dist/wasm/qcms_bg.wasm?url";
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";

import { newWorker } from "../reading";
import { Button, Loading } from "../ui";
import pdfWorkerUrl from "../workers/pdf.ts?worker&url";
import type { ViewerProps } from "./file-preview";
import { PreviewNote, Verified } from "./notes";
import { ReadError } from "./source";
import { ReadFailed, useRead } from "./use-read";

/** The standard fonts PDFs name but needn't embed, which pdf.js asks for by name: Foxit's,
 * from PDFium (BSD-3-Clause). pdf.js's Liberation fonts are GPL-licensed and left out; for
 * those it uses the reader's own fonts. */
const FONTS = import.meta.glob<string>("/node_modules/pdfjs-dist/standard_fonts/*.pfb", {
  query: "?url&no-inline",
  import: "default",
  eager: true,
});
/** Adobe's character maps for CJK fonts (BSD-3-Clause). */
const CMAPS = import.meta.glob<string>("/node_modules/pdfjs-dist/cmaps/*.bcmap", {
  query: "?url&no-inline",
  import: "default",
  eager: true,
});
const WASM: Record<string, string> = {
  "jbig2.wasm": jbig2,
  "openjpeg.wasm": openjpeg,
  "qcms_bg.wasm": qcms,
};

function byName(urls: Record<string, string>): Map<string, string> {
  return new Map(Object.entries(urls).map(([path, url]) => [path.split("/").at(-1) ?? path, url]));
}

const ASSETS: Record<string, Map<string, string>> = {
  standardFontDataUrl: byName(FONTS),
  cMapUrl: byName(CMAPS),
  wasmUrl: new Map(Object.entries(WASM)),
};

/** What pdf.js asks the page for, by kind and name: only the app's own files. */
class Assets {
  async fetch({ kind, filename }: { kind: string; filename: string }): Promise<Uint8Array> {
    const url = ASSETS[kind]?.get(filename);
    if (!url) throw new Error(`pdf.js asked for ${filename}, which Iki doesn’t have.`);
    const response = await fetch(url);
    if (!response.ok) throw new Error(`${filename} didn’t load (${String(response.status)}).`);
    return new Uint8Array(await response.arrayBuffer());
  }
}

/** Zooms, as a share of the page's own width; "fit" fits the frame's width. */
const ZOOMS = ["fit", "0.5", "0.75", "1", "1.25", "1.5", "2", "3"] as const;
type Zoom = (typeof ZOOMS)[number];
/** The most pixels a page's canvas may be: a huge page at a high zoom is drawn smaller. */
const MAX_CANVAS_PIXELS = 32 * 1024 * 1024;
/** How long a page has to draw. */
const RENDER_TIMEOUT_MS = 30_000;

interface Opened {
  document: PDFDocumentProxy;
  close: () => void;
}

async function open(bytes: ArrayBuffer): Promise<Opened> {
  const port = newWorker(pdfWorkerUrl);
  const worker = new PDFWorker({ port: port as unknown as null });
  const task = getDocument({
    data: new Uint8Array(bytes),
    worker,
    BinaryDataFactory: Assets,
    cMapPacked: true,
    // Fonts a PDF names but doesn't embed: the reader's own, then Foxit's.
    useSystemFonts: true,
    // pdf.js's own renderer worker would be created without the app's Trusted Types
    // policy; pages are painted on the main thread instead.
    disableWorkerRendering: true,
    enableXfa: false,
    stopAtErrors: false,
    maxImageSize: 64 * 1024 * 1024,
    verbosity: 0,
  });
  const close = () => {
    void task.destroy();
    worker.destroy();
    port.terminate();
  };
  try {
    return { document: await task.promise, close };
  } catch (error) {
    close();
    if (error instanceof PasswordException) {
      throw new ReadError(
        "It’s protected by a password, which a preview doesn’t ask for. Download it to open it.",
      );
    }
    if (error instanceof InvalidPDFException) {
      throw new ReadError("It isn’t a PDF that can be read: its structure is broken.");
    }
    throw error;
  }
}

export default function PdfViewer({ file, stored }: ViewerProps) {
  const read = useRead(async (signal) => open(await stored.whole(signal)), [stored]);
  // The document and its worker end with the preview.
  useEffect(() => {
    if (read.phase !== "read") return;
    const opened = read.value;
    return () => {
      opened.close();
    };
  }, [read]);
  if (read.phase === "failed") return <ReadFailed file={file} failed={read} />;
  if (read.phase === "reading") return <Loading label={`Reading ${file.name}…`} />;
  return <Pages file={file} document={read.value.document} />;
}

function Pages({ file, document }: { file: ViewerProps["file"]; document: PDFDocumentProxy }) {
  const count = document.numPages;
  const [page, setPage] = useState(1);
  const [typed, setTyped] = useState("1");
  const [zoom, setZoom] = useState<Zoom>("fit");
  const id = useId();
  const [keyed, setKeyed] = useState(false);
  const go = (to: number, fromKeys = false) => {
    const next = Math.min(count, Math.max(1, to));
    setPage(next);
    setTyped(String(next));
    setKeyed(fromKeys);
  };
  const onKey = (event: KeyboardEvent) => {
    const moves: Record<string, number> = {
      PageDown: page + 1,
      PageUp: page - 1,
      ArrowRight: page + 1,
      ArrowLeft: page - 1,
      Home: 1,
      End: count,
    };
    const to = moves[event.key];
    if (to === undefined) return;
    event.preventDefault();
    // The page turned from the keyboard keeps the keyboard: the next page has its focus.
    go(to, true);
  };
  return (
    <div className="preview-pdf">
      <div className="preview-toolbar" role="toolbar" aria-label={`Pages of ${file.name}`}>
        <Button
          onClick={() => {
            go(page - 1);
          }}
          disabled={page <= 1}
        >
          Previous page
        </Button>
        <span className="preview-page-field">
          <label htmlFor={`${id}-page`}>Page</label>
          <input
            id={`${id}-page`}
            className="preview-page-input"
            type="number"
            inputMode="numeric"
            min={1}
            max={count}
            value={typed}
            onChange={(event) => {
              setTyped(event.target.value);
              const n = Number(event.target.value);
              if (Number.isInteger(n) && n >= 1 && n <= count) {
                setPage(n);
                setKeyed(false);
              }
            }}
            onBlur={() => {
              setTyped(String(page));
            }}
          />
          <span>of {count.toLocaleString()}</span>
        </span>
        <Button
          onClick={() => {
            go(page + 1);
          }}
          disabled={page >= count}
        >
          Next page
        </Button>
        <span className="preview-zoom">
          <label htmlFor={`${id}-zoom`}>Zoom</label>
          <select
            id={`${id}-zoom`}
            value={zoom}
            onChange={(event) => {
              setZoom(event.target.value as Zoom);
            }}
          >
            {ZOOMS.map((z) => (
              <option key={z} value={z}>
                {z === "fit" ? "Fit width" : `${String(Math.round(Number(z) * 100))}%`}
              </option>
            ))}
          </select>
        </span>
      </div>
      <PdfPage
        key={page}
        document={document}
        number={page}
        count={count}
        zoom={zoom}
        focus={keyed}
        onKeyDown={onKey}
      />
      <PreviewNote>
        <Verified />
      </PreviewNote>
    </div>
  );
}

function PdfPage({
  document,
  number,
  count,
  zoom,
  focus,
  onKeyDown,
}: {
  document: PDFDocumentProxy;
  number: number;
  count: number;
  zoom: Zoom;
  /** Whether it takes the focus when it's shown: a page turned to from the keyboard. */
  focus: boolean;
  onKeyDown: (event: KeyboardEvent) => void;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const text = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [state, setState] = useState<
    | { phase: "drawing" }
    | { phase: "drawn"; width: number; height: number }
    | { phase: "failed"; message: string }
  >({ phase: "drawing" });

  useEffect(() => {
    if (focus) frame.current?.focus();
    // Only as it's shown: a page keeps the focus it was given.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fit width follows the frame's width.
  useEffect(() => {
    const element = frame.current;
    if (!element || typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setWidth(Math.floor(entry.contentRect.width));
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
    };
  }, []);

  useEffect(() => {
    // Until the frame's been measured, its width as laid out.
    const fit = width || (frame.current?.clientWidth ?? 0);
    if (!fit && zoom === "fit") return;
    // What this drawing started, for its cleanup and its timeout to end.
    const stop = new AbortController();
    // A call, not a property, so each check reads it afresh after an await.
    const stopped = () => stop.signal.aborted;
    const run: {
      task: RenderTask | null;
      layer: TextLayer | null;
      page: PDFPageProxy | null;
    } = { task: null, layer: null, page: null };
    const timer = setTimeout(() => {
      run.task?.cancel();
      if (!stopped()) {
        setState({ phase: "failed", message: "The page took too long to draw, so Iki stopped." });
      }
    }, RENDER_TIMEOUT_MS);
    void (async () => {
      try {
        const pageProxy = await document.getPage(number);
        run.page = pageProxy;
        const natural = pageProxy.getViewport({ scale: 1 });
        const scale = zoom === "fit" ? fit / natural.width : Number(zoom);
        const viewport = pageProxy.getViewport({ scale });
        const ratio = Math.min(
          window.devicePixelRatio || 1,
          Math.sqrt(MAX_CANVAS_PIXELS / (viewport.width * viewport.height)),
        );
        const target = canvas.current;
        const overlay = text.current;
        if (!target || !overlay || stopped()) return;
        target.width = Math.floor(viewport.width * ratio);
        target.height = Math.floor(viewport.height * ratio);
        const task = pageProxy.render({
          canvas: target,
          viewport,
          transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
          background: "#ffffff",
        });
        run.task = task;
        await task.promise;
        if (stopped()) return;
        overlay.replaceChildren();
        overlay.style.setProperty("--total-scale-factor", String(scale));
        const layer = new TextLayer({
          textContentSource: pageProxy.streamTextContent(),
          container: overlay,
          viewport,
        });
        run.layer = layer;
        await layer.render();
        if (!stopped())
          setState({ phase: "drawn", width: viewport.width, height: viewport.height });
      } catch (error) {
        if (
          stop.signal.aborted ||
          (error instanceof Error && error.name === "RenderingCancelledException")
        ) {
          return;
        }
        setState({
          phase: "failed",
          message: error instanceof Error ? error.message : String(error),
        });
      } finally {
        clearTimeout(timer);
      }
    })();
    return () => {
      stop.abort();
      clearTimeout(timer);
      run.task?.cancel();
      run.layer?.cancel();
      run.page?.cleanup();
    };
  }, [document, number, zoom, width]);

  const drawn = state.phase === "drawn" ? state : null;
  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- its keys turn pages
    <div
      ref={frame}
      className="preview-pdf-frame"
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable region
      tabIndex={0}
      role="group"
      aria-roledescription="page"
      aria-label={`Page ${String(number)} of ${String(count)}`}
      onKeyDown={onKeyDown}
    >
      {state.phase === "failed" ? (
        <p className="preview-pdf-failed" role="alert">
          Iki couldn’t draw page {number}. {state.message}
        </p>
      ) : null}
      {state.phase === "drawing" ? <Loading label={`Drawing page ${String(number)}…`} /> : null}
      <div
        className="preview-pdf-page"
        hidden={state.phase === "failed"}
        style={
          drawn
            ? { width: `${String(drawn.width)}px`, height: `${String(drawn.height)}px` }
            : undefined
        }
      >
        <canvas ref={canvas} aria-hidden="true" />
        <div ref={text} className="preview-pdf-text" />
      </div>
    </div>
  );
}
