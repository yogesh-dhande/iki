/**
 * A PNG or JPEG image (ADR 0226), read whole, checked against its SHA-256 and shown from
 * a blob of those bytes, so the image shown is the file's. It fits the preview's width and
 * zooms; the browser's own decoder draws it, as it draws any image on a page.
 */

import { useEffect, useId, useState } from "react";

import { Loading } from "../ui";
import type { ViewerProps } from "./file-preview";
import { count, PreviewNote, Verified } from "./notes";
import { ReadFailed, useRead } from "./use-read";
import { ZoomControl, type Zoom } from "./zoom";

export default function ImageViewer({ file, stored }: ViewerProps) {
  const read = useRead(
    async (signal) =>
      URL.createObjectURL(new Blob([await stored.whole(signal)], { type: file.media_type })),
    [stored, file.media_type],
  );
  useEffect(() => {
    if (read.phase !== "read") return;
    const url = read.value;
    return () => {
      URL.revokeObjectURL(url);
    };
  }, [read]);
  const [zoom, setZoom] = useState<Zoom>("fit");
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [broken, setBroken] = useState(false);
  const id = useId();
  if (read.phase === "failed") return <ReadFailed file={file} failed={read} />;
  if (read.phase === "reading") return <Loading label={`Reading ${file.name}…`} />;
  if (broken) {
    return (
      <ReadFailed
        file={file}
        failed={{ phase: "failed", held: false, message: "The browser couldn’t decode the image." }}
      />
    );
  }
  return (
    <div className="preview-image">
      <div className="preview-toolbar" role="toolbar" aria-label={`View of ${file.name}`}>
        <ZoomControl id={id} zoom={zoom} onZoom={setZoom} />
        {size ? (
          <span className="muted">
            {count(size.width)} × {count(size.height)} pixels
          </span>
        ) : null}
      </div>
      <div
        className="preview-image-frame"
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable region
        tabIndex={0}
        role="group"
        aria-label={`${file.name}, scrollable`}
        data-zoom={zoom}
      >
        <img
          src={read.value}
          alt={file.name}
          decoding="async"
          style={
            zoom === "fit" || !size
              ? undefined
              : { width: `${String(size.width * zoom)}px`, maxWidth: "none" }
          }
          onLoad={(event) => {
            const image = event.currentTarget;
            setSize({ width: image.naturalWidth, height: image.naturalHeight });
          }}
          onError={() => {
            setBroken(true);
          }}
        />
      </div>
      <PreviewNote>
        <Verified />
      </PreviewNote>
    </div>
  );
}
