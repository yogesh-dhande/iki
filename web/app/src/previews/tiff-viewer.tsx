/**
 * A TIFF, OME-TIFF or pyramidal TIFF (ADR 0227), shown a tile at a time: a 2 GB slide is
 * never in memory. The page reads its header and image directories in ranges, as Iki's
 * TIFF reader asks for them, then only the tiles in view, at the coarsest pyramid level
 * that still has a stored pixel for every screen pixel, each decoded to RGBA in the
 * previews' worker. Coarser tiles already read stand in while finer ones arrive.
 *
 * Images of more than 8 bits are shown through one window of values for black and white,
 * chosen from the image's own intensities, so neighbouring tiles match; the person can set
 * it. OME-TIFF planes are chosen by channel, focal plane and time point, as OME-XML maps
 * them to the file's images. Pixels past one screen pixel are drawn as blocks, never
 * smoothed: what's shown at high zoom is what was measured.
 */

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
  type RefObject,
} from "react";

import { formatSize } from "../entities";
import { Button, Loading } from "../ui";
import { openBytes, type ByteSource } from "./bytes";
import type { ViewerProps } from "./file-preview";
import { NoPreview } from "./no-preview";
import { count, Integrity, plural, PreviewNote } from "./notes";
import type { PreviewReader } from "./reader";
import type { Ome, TiffImage, TiffLayout } from "./readings";
import {
  bytesOf,
  centreTiles,
  chooseLevel,
  clampView,
  describePixels,
  fitView,
  levelsOf,
  MAX_LAYOUT_BYTES,
  MAX_VIEW_BYTES,
  MAX_VIEW_TILES,
  needsWindow,
  omeChannels,
  omePlane,
  pan,
  physicalSize,
  placement,
  readLayout,
  samplesShownApart,
  scaleLimits,
  smallestLevel,
  TileLoader,
  tilesIn,
  visibleRect,
  windowFrom,
  zoomAt,
  type Level,
  type Size,
  type TileJob,
  type TileRef,
  type View,
} from "./tiff";
import { ReadFailed, useRead } from "./use-read";
import { zoomLabel } from "./zoom";

type Bitmap = ImageBitmap | HTMLCanvasElement;

/** Files up to this are read whole and checked against their SHA-256; larger in ranges. */
const WHOLE = MAX_LAYOUT_BYTES;
/** Tiles measured for a first contrast, and for Auto contrast. */
const MEASURE_TILES = 9;
const AUTO_TILES = 16;
/** How long after the last keystroke a typed display range is applied. */
const RANGE_DELAY_MS = 400;

export default function TiffViewer({ file, stored, reader }: ViewerProps) {
  const read = useRead(
    async (signal) => {
      const source = await openBytes(stored, WHOLE, signal);
      const layout = await readLayout(
        source,
        async (input) => (await reader.read("tiff_layout", [], input)).value,
        signal,
      );
      return { source, layout };
    },
    [stored, reader],
  );
  if (read.phase === "failed") return <ReadFailed file={file} failed={read} />;
  if (read.phase === "reading") return <Loading label={`Reading ${file.name}…`} />;
  if (!read.value.layout.images.length) {
    return <NoPreview file={file}>Iki couldn’t find an image in {file.name}.</NoPreview>;
  }
  return (
    <Opened file={file} source={read.value.source} layout={read.value.layout} reader={reader} />
  );
}

async function toBitmap(
  pixels: Uint8ClampedArray<ArrayBuffer>,
  width: number,
  height: number,
): Promise<Bitmap | null> {
  if (typeof ImageData === "undefined") return null;
  const data = new ImageData(pixels, width, height);
  if (typeof createImageBitmap === "function") return createImageBitmap(data);
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  if (!context) return null;
  context.putImageData(data, 0, 0);
  return canvas;
}

/**
 * The tile loader for this preview, made when it's first needed and closed with it. (Made
 * lazily, not in state: React's checks mount, unmount and mount again, and a closed loader
 * can't be reused.)
 */
function useLoader(source: ByteSource, reader: PreviewReader, listener: RefObject<() => void>) {
  const loader = useRef<TileLoader<Bitmap> | null>(null);
  useEffect(
    () => () => {
      loader.current?.close();
      loader.current = null;
    },
    [source, reader],
  );
  return useCallback(
    () =>
      (loader.current ??= new TileLoader<Bitmap>({
        read: (offset, length, signal) => source.read(offset, length, signal),
        decode: (input, requestLength) => reader.read("tiff_tile", [requestLength], input),
        bitmap: toBitmap,
        release: (bitmap) => {
          if (typeof ImageBitmap !== "undefined" && bitmap instanceof ImageBitmap) bitmap.close();
        },
        changed: () => {
          listener.current();
        },
      })),
    [source, reader, listener],
  );
}

/** What's chosen: an image, or an OME plane, and a sample. */
interface Plane {
  image: TiffImage | null;
  sample: number | null;
  /** For the frame's label: "channel DAPI, Z 3 of 10". */
  describe: string;
  /** Contrast is kept per channel, as image viewers keep it. */
  channelKey: string;
  /** Why there's no image for it, if there isn't. */
  missing: string | null;
}

function channelName(ome: Ome, c: number): string {
  const name = ome.channels[c]?.name?.trim();
  return name === undefined || name === "" ? `Channel ${String(c + 1)}` : name;
}

function choosePlane(
  layout: TiffLayout,
  choice: { image: number; c: number; z: number; t: number; sample: number },
): Plane {
  const { ome, images } = layout;
  const first = images[0];
  if (ome && first) {
    const located = omePlane(ome, choice.c, choice.z, choice.t, first);
    const parts: string[] = [];
    if (omeChannels(ome, first.samples_per_pixel) > 1 || (located?.sample ?? null) !== null) {
      parts.push(`channel ${channelName(ome, choice.c)}`);
    }
    if (ome.size_z > 1) parts.push(`Z ${String(choice.z + 1)} of ${String(ome.size_z)}`);
    if (ome.size_t > 1) parts.push(`time point ${String(choice.t + 1)} of ${String(ome.size_t)}`);
    const describe = parts.join(", ");
    if (!located) {
      return {
        image: null,
        sample: null,
        describe,
        channelKey: `c${String(choice.c)}`,
        missing: "Its OME-XML doesn’t say which of its images holds this plane.",
      };
    }
    const image = images.find((i) => i.index === located.ifd) ?? null;
    return {
      image,
      sample: located.sample,
      describe,
      channelKey: `c${String(choice.c)}`,
      missing: image
        ? null
        : `Its OME-XML puts this plane in image ${count(located.ifd + 1)}, which the file doesn’t have.`,
    };
  }
  const image = images[Math.min(choice.image, images.length - 1)] ?? null;
  const apart = image !== null && samplesShownApart(image);
  const parts: string[] = [];
  if (images.length > 1)
    parts.push(`image ${String(choice.image + 1)} of ${String(images.length)}`);
  if (apart)
    parts.push(`sample ${String(choice.sample + 1)} of ${String(image.samples_per_pixel)}`);
  return {
    image,
    sample: apart ? choice.sample : null,
    describe: parts.join(", "),
    channelKey: `i${String(choice.image)}s${String(apart ? choice.sample : 0)}`,
    missing: null,
  };
}

/** A window that shows a type's whole range, for when none could be measured. */
function typeRange(image: TiffImage): [number, number] {
  const bits = image.bits_per_sample;
  if (image.sample_format === 3) return [0, 1];
  if (image.sample_format === 2) return [-(2 ** (bits - 1)), 2 ** (bits - 1) - 1];
  return [0, 2 ** bits - 1];
}

function tileKey(image: TiffImage, level: Level, tile: TileRef, sample: number | null): string {
  return `${String(image.index)}/${String(level.index)}/${String(tile.index)}/${sample === null ? "-" : String(sample)}`;
}

function jobsFor(
  image: TiffImage,
  level: Level,
  tiles: readonly TileRef[],
  sample: number | null,
  window: [number, number] | null,
): TileJob[] {
  return tiles.map((tile) => ({
    key: tileKey(image, level, tile, sample),
    stored: level.stored,
    tile,
    sample,
    window,
  }));
}

/** Tiles nearest a point in a level's pixels first. */
function nearest(tiles: TileRef[], level: Level, view: View): TileRef[] {
  const x = view.cx / level.downsampleX;
  const y = view.cy / level.downsampleY;
  const distance = (t: TileRef) =>
    (t.x + t.shownWidth / 2 - x) ** 2 + (t.y + t.shownHeight / 2 - y) ** 2;
  return [...tiles].sort((a, b) => distance(a) - distance(b));
}

function isMac(): boolean {
  return (
    typeof navigator !== "undefined" &&
    /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)
  );
}

function Opened({
  file,
  source,
  layout,
  reader,
}: {
  file: ViewerProps["file"];
  source: ByteSource;
  layout: TiffLayout;
  reader: PreviewReader;
}) {
  const id = useId();
  const { ome, images } = layout;
  const [choice, setChoice] = useState({ image: 0, c: 0, z: 0, t: 0, sample: 0 });
  const plane = useMemo(() => choosePlane(layout, choice), [layout, choice]);
  const image = plane.image;
  const levels = useMemo(() => (image ? levelsOf(image) : []), [image]);
  const usable = levels.some((level) => level.unusable === null);
  // Whether the frame is there to draw in: the effects that watch it start when it is.
  const framed = image !== null && plane.missing === null && usable;

  const frameRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [frame, setFrame] = useState<Size>({ width: 0, height: 0 });
  // Null until the person zooms or pans: the image fits the frame as it resizes, unless
  // that would read too much of it (a huge image without a pyramid), when it opens at 100%
  // in the middle. "fit" is fitted whatever it reads.
  const [chosenView, setView] = useState<View | "fit" | null>(null);
  const [windows, setWindows] = useState<Record<string, [number, number]>>({});
  const [loading, setLoading] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [hint, setHint] = useState(false);
  const [measuring, setMeasuring] = useState(false);

  const size: Size = image ?? { width: 1, height: 1 };
  const ratio = typeof window === "undefined" ? 1 : window.devicePixelRatio || 1;
  // The level a view is drawn at, its tiles, and whether there are too many to read at once.
  const plan = (v: View) => {
    const level = frame.width && usable ? chooseLevel(levels, v.scale * ratio) : undefined;
    const rect = visibleRect(v, frame);
    const visible = level ? tilesIn(level, rect) : [];
    const viewBytes = level ? bytesOf(level.stored, visible) : 0;
    return {
      level,
      rect,
      visible,
      viewBytes,
      tooMuch: viewBytes > MAX_VIEW_BYTES || visible.length > MAX_VIEW_TILES,
    };
  };
  const fitted = fitView(size, frame);
  const view =
    chosenView === "fit"
      ? fitted
      : chosenView
        ? clampView(chosenView, size, frame)
        : plan(fitted).tooMuch
          ? clampView({ cx: size.width / 2, cy: size.height / 2, scale: 1 }, size, frame)
          : fitted;
  const { level, rect, visible, viewBytes, tooMuch } = plan(view);
  const wantsWindow = image !== null && needsWindow(image);
  const contrast = wantsWindow ? windows[plane.channelKey] : null;
  const integer = image !== null && image.sample_format !== 3;

  // What the drawing and the handlers read: the latest render's, without re-subscribing.
  const latest = useRef({ view, frame, size, levels, level, image, sample: plane.sample });
  useLayoutEffect(() => {
    latest.current = { view, frame, size, levels, level, image, sample: plane.sample };
  });

  // Moves and zooms build on the newest view, even between renders (a fast drag or wheel).
  const change = useCallback((next: (v: View, image: Size, frame: Size) => View) => {
    setView((old) => {
      const { view: shown, size: s, frame: f } = latest.current;
      return next(old !== null && old !== "fit" ? old : shown, s, f);
    });
  }, []);

  // The loader tells the view when a tile arrives; the view redraws (once a frame).
  const changed = useRef<() => void>(() => undefined);
  const getLoader = useLoader(source, reader, changed);
  const drawing = useRef<number | null>(null);
  const draw = () => {
    drawing.current = null;
    const loader = getLoader();
    setLoading(loader.loading > 0);
    const problems = loader.problems;
    const first = problems[0];
    setProblem(
      first
        ? `Iki couldn’t show ${plural(
            problems.reduce((n, p) => n + p.tiles, 0),
            "tile",
            "tiles",
          )}: ${first.message}`
        : null,
    );
    const canvas = canvasRef.current;
    const { view: v, frame: f, levels: ls, level: chosen, image: img, sample } = latest.current;
    const context = canvas?.getContext("2d");
    if (!canvas || !context || !img) return;
    const dpr = window.devicePixelRatio || 1;
    const width = Math.round(f.width * dpr);
    const height = Math.round(f.height * dpr);
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    context.setTransform(1, 0, 0, 1, 0, 0);
    context.clearRect(0, 0, width, height);
    if (!chosen) return;
    const shown = visibleRect(v, f);
    // Coarsest first: finer tiles, as they arrive, are drawn over the coarser ones.
    const order = ls
      .filter((l) => l.unusable === null && l.downsampleX >= chosen.downsampleX)
      .sort((a, b) => b.downsampleX - a.downsampleX);
    for (const l of order) {
      for (const tile of tilesIn(l, shown)) {
        const entry = loader.entry(tileKey(img, l, tile, sample));
        if (!entry?.bitmap) continue;
        const at = placement(l, tile, v, f, dpr);
        context.imageSmoothingEnabled = !at.magnified;
        context.drawImage(
          entry.bitmap,
          0,
          0,
          tile.shownWidth,
          tile.shownHeight,
          at.dx,
          at.dy,
          at.dw,
          at.dh,
        );
      }
    }
  };
  const redraw = () => {
    if (drawing.current !== null) return;
    drawing.current =
      typeof requestAnimationFrame === "function"
        ? requestAnimationFrame(draw)
        : (setTimeout(draw, 16) as unknown as number);
  };
  useLayoutEffect(() => {
    changed.current = redraw;
  });

  // The frame's size, in CSS pixels.
  useEffect(() => {
    const element = frameRef.current;
    if (!element || typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(([entry]) => {
      if (!entry) return;
      const next = {
        width: Math.floor(entry.contentRect.width),
        height: Math.floor(entry.contentRect.height),
      };
      setFrame((old) => (old.width === next.width && old.height === next.height ? old : next));
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
    };
  }, [framed]);

  // A first contrast for each channel, from the smallest level's middle tiles.
  const needsMeasure = wantsWindow && contrast === undefined && usable;
  const channelKey = plane.channelKey;
  useEffect(() => {
    if (!needsMeasure) return;
    const smallest = smallestLevel(levels);
    if (!smallest) return;
    const abort = new AbortController();
    const jobs = jobsFor(image, smallest, centreTiles(smallest, MEASURE_TILES), plane.sample, null);
    void getLoader()
      .measure(jobs, abort.signal)
      .then(
        (measured) => windowFrom(measured, image.sample_format !== 3) ?? typeRange(image),
        () => typeRange(image),
      )
      .then((found) => {
        if (!abort.signal.aborted) setWindows((old) => ({ ...old, [channelKey]: found }));
      });
    return () => {
      abort.abort();
    };
    // getLoader is the same loader for the preview's source and reader.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [needsMeasure, image, levels, plane.sample, channelKey]);

  // The tiles the view wants. A window being measured holds them back, so none is decoded
  // with a contrast that's about to change.
  const jobsKey =
    level && image ? `${String(image.index)}/${String(level.index)}/${String(plane.sample)}` : "";
  useEffect(() => {
    const loader = getLoader();
    if (!level || !image || (wantsWindow && contrast === undefined) || tooMuch) {
      loader.want([]);
      redraw();
      return;
    }
    const window = contrast ?? null;
    const jobs = jobsFor(image, level, nearest(visible, level, view), plane.sample, window);
    // A margin of half a frame round the view: reads there go on through a small pan.
    const margin = { x: frame.width / view.scale / 2, y: frame.height / view.scale / 2 };
    const around = tilesIn(level, {
      x0: rect.x0 - margin.x,
      y0: rect.y0 - margin.y,
      x1: rect.x1 + margin.x,
      y1: rect.y1 + margin.y,
    });
    loader.want(
      jobs,
      new Set(jobsFor(image, level, around, plane.sample, window).map((j) => j.key)),
      window,
    );
    redraw();
    // The view's numbers are what change it; the derived lists follow them.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    jobsKey,
    view.cx,
    view.cy,
    view.scale,
    frame.width,
    frame.height,
    contrast,
    tooMuch,
    wantsWindow,
  ]);

  // The wheel zooms, round the pointer, where the person means it to: with Ctrl or ⌘ (and a
  // trackpad's pinch, which browsers send as Ctrl), or once they've clicked into the image.
  // Otherwise the page scrolls, and a hint says how.
  useEffect(() => {
    const element = frameRef.current;
    if (!element) return;
    let hiding: ReturnType<typeof setTimeout> | undefined;
    const onWheel = (event: WheelEvent) => {
      if (!(event.ctrlKey || event.metaKey || document.activeElement === element)) {
        setHint(true);
        clearTimeout(hiding);
        hiding = setTimeout(() => {
          setHint(false);
        }, 1500);
        return;
      }
      event.preventDefault();
      const lines = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? element.clientHeight : 1;
      const factor = Math.exp(-event.deltaY * lines * 0.002);
      const box = element.getBoundingClientRect();
      const at = { x: event.clientX - box.left, y: event.clientY - box.top };
      change((v, s, f) => zoomAt(v, factor, at, s, f));
    };
    element.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      clearTimeout(hiding);
      element.removeEventListener("wheel", onWheel);
    };
  }, [framed, change]);

  // Dragging pans; two fingers pinch.
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const was = pointers.current.get(event.pointerId);
    if (!was) return;
    const now = { x: event.clientX, y: event.clientY };
    const others = [...pointers.current]
      .filter(([key]) => key !== event.pointerId)
      .map(([, p]) => p);
    const other = others[0];
    if (other && others.length === 1) {
      const before = Math.hypot(was.x - other.x, was.y - other.y);
      const after = Math.hypot(now.x - other.x, now.y - other.y);
      const box = event.currentTarget.getBoundingClientRect();
      const mid = { x: (now.x + other.x) / 2 - box.left, y: (now.y + other.y) / 2 - box.top };
      if (before > 0) change((v, s, f) => zoomAt(v, after / before, mid, s, f));
    } else if (!others.length) {
      change((v, s, f) => pan(v, now.x - was.x, now.y - was.y, s, f));
    }
    pointers.current.set(event.pointerId, now);
  };
  const onPointerUp = (event: PointerEvent<HTMLDivElement>) => {
    pointers.current.delete(event.pointerId);
  };

  const limits = scaleLimits(size, frame);
  const zoomBy = (factor: number) => {
    change((v, s, f) => zoomAt(v, factor, { x: f.width / 2, y: f.height / 2 }, s, f));
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const step = event.shiftKey ? 0.5 : 0.1;
    const moves: Record<string, [number, number]> = {
      ArrowLeft: [frame.width * step, 0],
      ArrowRight: [-frame.width * step, 0],
      ArrowUp: [0, frame.height * step],
      ArrowDown: [0, -frame.height * step],
    };
    const move = moves[event.key];
    if (move) change((v, s, f) => pan(v, move[0], move[1], s, f));
    else if (event.key === "+" || event.key === "=") zoomBy(2);
    else if (event.key === "-" || event.key === "_") zoomBy(0.5);
    else if (event.key === "0") setView("fit");
    else if (event.key === "1") change((v, s, f) => clampView({ ...v, scale: 1 }, s, f));
    else return;
    event.preventDefault();
  };

  const auto = async () => {
    if (!image || !level) return;
    setMeasuring(true);
    try {
      const abort = new AbortController();
      const tiles = nearest(visible, level, view).slice(0, AUTO_TILES);
      const measured = await getLoader().measure(
        jobsFor(image, level, tiles, plane.sample, null),
        abort.signal,
      );
      const found = windowFrom(measured, integer);
      if (found) setWindows((old) => ({ ...old, [plane.channelKey]: found }));
    } finally {
      setMeasuring(false);
    }
  };

  const status = (() => {
    if (!usable || !image) return null;
    if (wantsWindow && contrast === undefined)
      return "Measuring its intensities to set the contrast…";
    if (!level) return null;
    if (tooMuch) {
      return `At this zoom the view needs ${formatSize(viewBytes)} of the file, more than a preview reads at once. Zoom in to see part of it.`;
    }
    const where =
      levels.length > 1
        ? `level ${String(level.index + 1)} of ${String(levels.length)} (1:${count(Math.round(level.downsampleX))})`
        : "full resolution";
    return loading ? `Loading tiles at ${where}…` : `Showing ${where}`;
  })();

  const fine = levels[0];
  const reduced = fine?.unusable && usable ? fine.unusable : null;
  const pixelSize = physicalSize(ome);
  const keysId = `${id}-keys`;
  const label = image
    ? `${file.name}: ${count(image.width)} × ${count(image.height)} pixels${plane.describe ? `, ${plane.describe}` : ""}, at ${zoomLabel(view.scale)}`
    : file.name;

  return (
    <div className="preview-tiff">
      <div className="preview-toolbar" role="toolbar" aria-label={`View of ${file.name}`}>
        <PlaneControls
          id={id}
          layout={layout}
          choice={choice}
          onChoice={(next) => {
            // Another image may be another size: it opens fitted. Planes of one keep the view.
            if (next.image !== choice.image) setView(null);
            setChoice(next);
          }}
        />
        <span className="tiff-zoom">
          <Button
            onClick={() => {
              zoomBy(0.5);
            }}
            disabled={!usable || view.scale <= limits.min}
          >
            Zoom out
          </Button>
          <Button
            onClick={() => {
              zoomBy(2);
            }}
            disabled={!usable || view.scale >= limits.max}
          >
            Zoom in
          </Button>
          <Button
            onClick={() => {
              setView("fit");
            }}
            disabled={!usable}
            label="Fit the image to the frame"
          >
            Fit
          </Button>
          <Button
            onClick={() => {
              change((v, s, f) => clampView({ ...v, scale: 1 }, s, f));
            }}
            disabled={!usable}
            label="100%: one image pixel to a screen pixel"
          >
            100%
          </Button>
          <output className="tiff-zoom-level" aria-label="Zoom">
            {zoomLabel(view.scale)}
          </output>
        </span>
      </div>
      {wantsWindow && contrast ? (
        <ContrastControls
          id={id}
          window={contrast}
          integer={integer}
          measuring={measuring}
          onWindow={(next) => {
            setWindows((old) => ({ ...old, [plane.channelKey]: next }));
          }}
          onAuto={() => void auto()}
        />
      ) : null}
      {image ? <Facts image={image} levels={levels} ome={ome} pixelSize={pixelSize} /> : null}
      {plane.missing || !image ? (
        <NoPreview file={file}>
          {plane.missing ?? "Iki couldn’t find this image in the file."}
        </NoPreview>
      ) : !usable ? (
        <NoPreview file={file}>
          Iki can’t show {images.length > 1 || ome ? "this image" : file.name}. {fine?.unusable}
        </NoPreview>
      ) : (
        <>
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- a pannable, zoomable image, moved by keys */}
          <div
            ref={frameRef}
            className="tiff-frame"
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a pannable image takes focus for its keys
            tabIndex={0}
            role="img"
            aria-label={label}
            aria-describedby={keysId}
            data-zoom={view.scale}
            onKeyDown={onKeyDown}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            onDoubleClick={(event) => {
              const box = event.currentTarget.getBoundingClientRect();
              const at = { x: event.clientX - box.left, y: event.clientY - box.top };
              const factor = event.shiftKey ? 0.5 : 2;
              change((v, s, f) => zoomAt(v, factor, at, s, f));
            }}
          >
            <canvas ref={canvasRef} className="tiff-canvas" aria-hidden="true" />
            {hint ? (
              <p className="tiff-hint" aria-hidden="true">
                Hold {isMac() ? "⌘" : "Ctrl"} and scroll to zoom, or click the image first
              </p>
            ) : null}
          </div>
          <p className="tiff-status muted">{status}</p>
          {problem ? (
            <p className="tiff-problem" role="status">
              {problem}
            </p>
          ) : null}
          <PreviewNote>
            <span id={keysId}>
              Drag to move it, and scroll with {isMac() ? "⌘" : "Ctrl"} to zoom; or use the arrow
              keys, + and −, 0 to fit and 1 for 100%.
            </span>
          </PreviewNote>
        </>
      )}
      <PreviewNote>
        <Integrity source={source} />
        {reduced ? ` Full resolution isn’t shown: ${reduced}` : null}
        {layout.truncated
          ? " Some of its images or tiles are past what a preview lists, so they aren’t shown."
          : null}
      </PreviewNote>
    </div>
  );
}

/** A 1-based number field for a 0-based index, like the PDF viewer's page field. */
function IndexField({
  id,
  label,
  value,
  total,
  onValue,
}: {
  id: string;
  label: string;
  value: number;
  total: number;
  onValue: (value: number) => void;
}) {
  const [typed, setTyped] = useState(String(value + 1));
  const [seen, setSeen] = useState(value);
  if (seen !== value) {
    setSeen(value);
    setTyped(String(value + 1));
  }
  return (
    <span className="preview-page-field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        className="preview-page-input"
        type="number"
        inputMode="numeric"
        min={1}
        max={total}
        value={typed}
        onChange={(event) => {
          setTyped(event.target.value);
          const n = Number(event.target.value);
          if (Number.isInteger(n) && n >= 1 && n <= total) onValue(n - 1);
        }}
        onBlur={() => {
          setTyped(String(value + 1));
        }}
      />
      <span>of {count(total)}</span>
    </span>
  );
}

interface Choice {
  image: number;
  c: number;
  z: number;
  t: number;
  sample: number;
}

function PlaneControls({
  id,
  layout,
  choice,
  onChoice,
}: {
  id: string;
  layout: TiffLayout;
  choice: Choice;
  onChoice: (choice: Choice) => void;
}) {
  const { ome, images } = layout;
  const first = images[0];
  if (ome && first) {
    const spp = Math.max(1, first.samples_per_pixel);
    // An RGB image's three samples are one channel to show: OME counts them as three.
    const rgb = first.photometric === 2 && spp > 1 && ome.size_c % spp === 0;
    const channels = rgb
      ? Array.from({ length: omeChannels(ome, spp) }, (_, i) => ({
          c: i * spp,
          name: `Channel ${String(i + 1)}`,
        }))
      : Array.from({ length: Math.max(1, ome.size_c) }, (_, c) => ({
          c,
          name: channelName(ome, c),
        }));
    return (
      <>
        {channels.length > 1 ? (
          <span>
            <label htmlFor={`${id}-channel`}>Channel</label>
            <select
              id={`${id}-channel`}
              value={String(choice.c)}
              onChange={(event) => {
                onChoice({ ...choice, c: Number(event.target.value) });
              }}
            >
              {channels.map(({ c, name }) => (
                <option key={c} value={String(c)}>
                  {name}
                </option>
              ))}
            </select>
          </span>
        ) : null}
        {ome.size_z > 1 ? (
          <IndexField
            id={`${id}-z`}
            label="Z plane"
            value={choice.z}
            total={ome.size_z}
            onValue={(z) => {
              onChoice({ ...choice, z });
            }}
          />
        ) : null}
        {ome.size_t > 1 ? (
          <IndexField
            id={`${id}-t`}
            label="Time point"
            value={choice.t}
            total={ome.size_t}
            onValue={(t) => {
              onChoice({ ...choice, t });
            }}
          />
        ) : null}
      </>
    );
  }
  const image = images[choice.image];
  return (
    <>
      {images.length > 1 ? (
        <span>
          <label htmlFor={`${id}-image`}>Image</label>
          <select
            id={`${id}-image`}
            value={String(choice.image)}
            onChange={(event) => {
              onChoice({ ...choice, image: Number(event.target.value), sample: 0 });
            }}
          >
            {images.map((i, at) => (
              <option key={i.index} value={String(at)}>
                {`${String(at + 1)}: ${count(i.width)} × ${count(i.height)}${i.unsupported ? " (can’t be shown)" : ""}`}
              </option>
            ))}
          </select>
        </span>
      ) : null}
      {image && samplesShownApart(image) ? (
        <span>
          <label htmlFor={`${id}-sample`}>Sample</label>
          <select
            id={`${id}-sample`}
            value={String(choice.sample)}
            onChange={(event) => {
              onChoice({ ...choice, sample: Number(event.target.value) });
            }}
          >
            {Array.from({ length: image.samples_per_pixel }, (_, s) => (
              <option key={s} value={String(s)}>
                {String(s + 1)}
              </option>
            ))}
          </select>
        </span>
      ) : null}
    </>
  );
}

function formatValue(value: number, integer: boolean): string {
  if (integer) return String(Math.round(value));
  return String(Number(value.toPrecision(6)));
}

/** The values shown as black and white, typed or measured. */
function ContrastControls({
  id,
  window,
  integer,
  measuring,
  onWindow,
  onAuto,
}: {
  id: string;
  window: [number, number];
  integer: boolean;
  measuring: boolean;
  onWindow: (window: [number, number]) => void;
  onAuto: () => void;
}) {
  const [low, setLow] = useState(formatValue(window[0], integer));
  const [high, setHigh] = useState(formatValue(window[1], integer));
  const [seen, setSeen] = useState(window);
  // A window set elsewhere (Auto contrast, another channel's) replaces what's typed.
  if (seen !== window) {
    setSeen(window);
    if (Number(low) !== window[0]) setLow(formatValue(window[0], integer));
    if (Number(high) !== window[1]) setHigh(formatValue(window[1], integer));
  }
  const lowN = low.trim() === "" ? NaN : Number(low);
  const highN = high.trim() === "" ? NaN : Number(high);
  const valid = Number.isFinite(lowN) && Number.isFinite(highN) && lowN < highN;
  const apply = useRef(onWindow);
  useLayoutEffect(() => {
    apply.current = onWindow;
  });
  // Tiles are decoded again for a new window: once typing stops, not at every key.
  useEffect(() => {
    if (!valid || (lowN === window[0] && highN === window[1])) return;
    const timer = setTimeout(() => {
      apply.current([lowN, highN]);
    }, RANGE_DELAY_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [valid, lowN, highN, window]);
  const errorId = `${id}-range-error`;
  return (
    <div className="preview-toolbar tiff-contrast" role="group" aria-labelledby={`${id}-range`}>
      <span id={`${id}-range`} className="tiff-contrast-label">
        Display range
      </span>
      <span>
        <label htmlFor={`${id}-low`}>Minimum</label>
        <input
          id={`${id}-low`}
          className="tiff-range-input"
          type="number"
          step={integer ? 1 : "any"}
          value={low}
          aria-invalid={!valid || undefined}
          aria-describedby={valid ? undefined : errorId}
          onChange={(event) => {
            setLow(event.target.value);
          }}
        />
      </span>
      <span>
        <label htmlFor={`${id}-high`}>Maximum</label>
        <input
          id={`${id}-high`}
          className="tiff-range-input"
          type="number"
          step={integer ? 1 : "any"}
          value={high}
          aria-invalid={!valid || undefined}
          aria-describedby={valid ? undefined : errorId}
          onChange={(event) => {
            setHigh(event.target.value);
          }}
        />
      </span>
      <Button onClick={onAuto} busy={measuring} busyText="Measuring…">
        Auto contrast
      </Button>
      {valid ? null : (
        <span id={errorId} className="tiff-range-error">
          The minimum must be a number less than the maximum.
        </span>
      )}
    </div>
  );
}

function Facts({
  image,
  levels,
  ome,
  pixelSize,
}: {
  image: TiffImage;
  levels: readonly Level[];
  ome: Ome | null;
  pixelSize: string | null;
}) {
  const smallest = levels[levels.length - 1];
  const channels = ome
    ? image.photometric === 2
      ? omeChannels(ome, image.samples_per_pixel)
      : ome.size_c
    : 0;
  const planes = ome
    ? [
        channels > 1 ? plural(channels, "channel", "channels") : null,
        ome.size_z > 1 ? plural(ome.size_z, "Z plane", "Z planes") : null,
        ome.size_t > 1 ? plural(ome.size_t, "time point", "time points") : null,
      ].filter((p): p is string => p !== null)
    : [];
  return (
    <dl className="preview-facts">
      <div>
        <dt>Size</dt>
        <dd>
          {count(image.width)} × {count(image.height)} pixels
        </dd>
      </div>
      <div>
        <dt>Pixels</dt>
        <dd>{describePixels(image)}</dd>
      </div>
      {pixelSize ? (
        <div>
          <dt>Pixel size</dt>
          <dd>{pixelSize}</dd>
        </div>
      ) : null}
      {levels.length > 1 && smallest ? (
        <div>
          <dt>Pyramid</dt>
          <dd>
            {plural(levels.length, "level", "levels")}, down to {count(smallest.stored.width)} ×{" "}
            {count(smallest.stored.height)}
          </dd>
        </div>
      ) : null}
      {planes.length ? (
        <div>
          <dt>Planes</dt>
          <dd>{planes.join(", ")}</dd>
        </div>
      ) : null}
    </dl>
  );
}
