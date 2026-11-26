/**
 * A file's preview (ADR 0226): what Iki's viewer for its format shows of it, without its
 * reader downloading it. The format's declaration names the viewer (ADR 0213); each is
 * Iki's own code, loaded when it's first needed. A file whose format has no viewer, or
 * that's past what its viewer reads, says so and offers the download instead. A file held
 * by the deployment's malware scan (62g) shows why, never its content.
 */

import { lazy, Suspense, useEffect, useMemo, useState, type ComponentType } from "react";

import { formatSize } from "../entities";
import { useMedia } from "../media";
import { Loading } from "../ui";
import { NoPreview } from "./no-preview";
import { PreviewReader } from "./reader";
import { StoredBytes, type PreviewFile } from "./source";

/** What every viewer is given. */
export interface ViewerProps {
  file: PreviewFile;
  /** The file's bytes, read through the audited link. */
  stored: StoredBytes;
  /** The preview's own readers' worker, ended with it. */
  reader: PreviewReader;
  /** The format's name, such as "ABI trace". */
  label: string;
  /** The viewer its format names, such as "table-tsv": one component shows several. */
  viewer: string;
}

interface Viewer {
  /** What it shows, for the preview's heading. */
  shows: string;
  /** The most a file may be for it; null for viewers that read ranges of any size. */
  max: number | null;
  /** What to open a file this viewer can't in, when it can't. */
  desktop: string;
  component: ComponentType<ViewerProps>;
}

const MiB = 1024 * 1024;

const pdf = lazy(() => import("./pdf-viewer"));
const image = lazy(() => import("./image-viewer"));
const tiff = lazy(() => import("./tiff-viewer"));
const table = lazy(() => import("./table-viewer"));
const chromatogram = lazy(() => import("./chromatogram-viewer"));
const spectrum = lazy(() => import("./spectrum-viewer"));

/** The viewers a format's declaration can name, and what each reads. */
export const VIEWERS: Record<string, Viewer> = {
  pdf: { shows: "Pages", max: 100 * MiB, desktop: "a PDF reader", component: pdf },
  image: { shows: "Image", max: 64 * MiB, desktop: "an image viewer", component: image },
  "image-tiff": {
    shows: "Image",
    max: null,
    desktop: "Fiji or QuPath",
    component: tiff,
  },
  "table-csv": { shows: "Table", max: null, desktop: "a spreadsheet", component: table },
  "table-tsv": { shows: "Table", max: null, desktop: "a spreadsheet", component: table },
  "table-xlsx": { shows: "Table", max: 64 * MiB, desktop: "Excel", component: table },
  "chromatogram-abif": {
    shows: "Chromatogram",
    max: 32 * MiB,
    desktop: "a trace viewer, such as SnapGene Viewer",
    component: chromatogram,
  },
  "spectrum-mzml": {
    shows: "Spectra",
    max: null,
    desktop: "a mass spectrometry viewer, such as SeeMS or TOPPView",
    component: spectrum,
  },
  "spectrum-jcamp-dx": {
    shows: "Spectrum",
    max: 64 * MiB,
    desktop: "your spectroscopy software",
    component: spectrum,
  },
};

/** The deployment's scan of the file, where the server says (62g). */
interface Scan {
  state: string;
  usable: boolean;
  reason?: string;
}

function scanOf(file: PreviewFile): Scan | undefined {
  const scan = (file as PreviewFile & { scan?: unknown }).scan;
  if (typeof scan !== "object" || scan === null) return undefined;
  const { state, usable, reason } = scan as Record<string, unknown>;
  return typeof state === "string" && typeof usable === "boolean"
    ? { state, usable, ...(typeof reason === "string" ? { reason } : {}) }
    : undefined;
}

/** Why a held file can't be shown, as the server words it when it refuses a download. */
export function heldMessage(scan: Scan): string {
  if (scan.state === "pending") return "This file is being scanned. Try again shortly.";
  if (scan.state === "infected") {
    return "This file is quarantined: the deployment’s malware scan found something in it. An administrator reviews held files.";
  }
  return "This file is held because the scanner couldn’t inspect it completely. An administrator reviews held files.";
}

/** The viewer for a file's format, or undefined. */
export function useViewer(file: PreviewFile): {
  viewer: Viewer | undefined;
  name: string;
  label: string;
} {
  const media = useMedia();
  const format = media.get(file.media_type);
  const name = format?.viewer ?? "";
  return {
    viewer: Object.hasOwn(VIEWERS, name) ? VIEWERS[name] : undefined,
    name,
    label: format?.label ?? file.media_type,
  };
}

export function FilePreview({ file }: { file: PreviewFile }) {
  const { viewer, name, label } = useViewer(file);
  const scan = scanOf(file);
  // One link cache and one worker per preview of a file, ended with it.
  const stored = useMemo(() => new StoredBytes(file), [file]);
  const [reader] = useState(() => new PreviewReader());
  useEffect(
    () => () => {
      reader.close();
    },
    [reader],
  );

  if (scan && !scan.usable) {
    return (
      <NoPreview file={file} download={false}>
        {heldMessage(scan)}
      </NoPreview>
    );
  }
  if (!viewer) {
    return (
      <NoPreview file={file}>
        Iki doesn’t show {label} files in the browser. Download it to open it.
      </NoPreview>
    );
  }
  if (file.size === 0)
    return <NoPreview file={file}>It’s empty: it has no bytes to show.</NoPreview>;
  if (viewer.max !== null && file.size > viewer.max) {
    return (
      <NoPreview file={file}>
        It’s {formatSize(file.size)}, bigger than the preview opens ({formatSize(viewer.max)}).
        Download it to open it in {viewer.desktop}.
      </NoPreview>
    );
  }
  const Shown = viewer.component;
  return (
    <Suspense fallback={<Loading label="Loading the viewer…" />}>
      <Shown
        key={file.id}
        file={file}
        stored={stored}
        reader={reader}
        label={label}
        viewer={name}
      />
    </Suspense>
  );
}

/** Whether Iki has a viewer for the file's format. */
export function usePreviewable(file: PreviewFile): boolean {
  return useViewer(file).viewer !== undefined;
}
