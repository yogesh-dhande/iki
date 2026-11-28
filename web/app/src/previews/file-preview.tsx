/**
 * A file's preview (ADR 0226): what Iki's viewer for its format shows of it, without its
 * reader downloading it. The format's declaration names the viewer (ADR 0213); each is
 * Iki's own code, loaded when it's first needed. A file whose format has no viewer, or
 * that's past what its viewer reads, says so and offers the download instead. A file held
 * by the deployment's malware scan (ADR 0216), or still being scanned, shows why, never its
 * content: nothing reads it until the scan lets it be used.
 */

import {
  lazy,
  Suspense,
  useEffect,
  useMemo,
  useState,
  type ComponentType,
  type ReactNode,
} from "react";

import { formatSize } from "../entities";
import { useMedia } from "../media";
import { heldBecause, type Scan } from "../quarantine";
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

/** Why a file the scan holds, or hasn't finished, isn't shown, as its page words it. */
export function heldMessage(scan: Scan): ReactNode {
  if (scan.state === "pending") {
    return "Scanning… It’s shown once the deployment’s malware scan finds it clean.";
  }
  const why = heldBecause(scan);
  if (scan.state === "infected") {
    return (
      <>
        <strong>Quarantined</strong>: the scanner found{" "}
        {why.finding ? <code>{why.finding}</code> : "malware"}. It isn’t shown or downloaded unless
        a Quarantine Admin finds it a false positive.
      </>
    );
  }
  return (
    <>
      <strong>Held</strong>: {why.label}. {why.detail} It isn’t shown or downloaded.
    </>
  );
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
  // One link cache and one worker per preview of a file, ended with it.
  const stored = useMemo(() => new StoredBytes(file), [file]);
  const [reader] = useState(() => new PreviewReader());
  useEffect(
    () => () => {
      reader.close();
    },
    [reader],
  );

  if (!file.scan.usable) {
    return (
      <NoPreview file={file} download={false}>
        {heldMessage(file.scan)}
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
