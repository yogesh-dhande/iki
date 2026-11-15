/**
 * The structure viewer: Mol*, in a sandboxed frame of Iki's own (ADR 0085). The page
 * reads the file, checks its bytes are the ones Iki stored, and hands them to the frame,
 * which can't read anything itself: no network, and no origin that's anyone's. For a saved
 * view (ADR 0086), it also hands it where the camera was, and asks where it is to save it.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { useEffect, useRef, useState, type SyntheticEvent } from "react";

import { iki } from "../api";
import { formatSize, type EntityType } from "../entities";
import { Loading } from "../ui";
import { readCamera, type Camera, type Capture } from "../views";
import type { Source } from "./entity-viewer";

type Viewer = Schemas["StructureViewerOut"];

/** The formats Mol* reads here, by media type, and what they're called. */
const FORMATS: Record<string, { format: "mmcif" | "pdb"; label: string }> = {
  "chemical/x-mmcif": { format: "mmcif", label: "mmCIF" },
  "chemical/x-pdb": { format: "pdb", label: "PDB" },
};

/** Bigger files are left to desktop viewers: Mol* would need several times this in memory. */
export const MAX_STRUCTURE_BYTES = 256 * 1024 * 1024;
/** How long Mol* has to show a structure it's been given before the page says it didn't. */
export const SHOW_TIMEOUT_MS = 60_000;
export const STRUCTURE_FRAME = "/viewers/structure/";

type State =
  | { phase: "reading" }
  | { phase: "starting"; bytes: ArrayBuffer }
  | { phase: "shown" }
  | { phase: "failed"; message: string }
  | { phase: "unsupported" }
  | { phase: "left" };

/** How long Mol* has to say where its camera is. */
const CAMERA_TIMEOUT_MS = 5_000;

async function hex(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** The file's bytes, read from the store with a link Iki signed, and checked against
 * the hash Iki recorded when it was stored. */
async function readFile(fileId: string, signal: AbortSignal): Promise<ArrayBuffer> {
  const link = unwrap(
    await iki.GET("/api/v1/files/{file_id}/content-url", {
      params: { path: { file_id: fileId } },
      signal,
    }),
  );
  const response = await fetch(link.url, { signal, credentials: "omit" });
  if (!response.ok) throw new Error(`The file store answered ${String(response.status)}.`);
  const bytes = await response.arrayBuffer();
  if ((await hex(bytes)) !== link.sha256) {
    throw new Error("What the file store sent isn’t the file Iki stored, so Iki didn’t show it.");
  }
  return bytes;
}

type Message =
  { shown: true } | { failed: string } | { unsupported: "webgl" } | { camera: Camera | null };

/** What Mol* said, if it's something it says: anything else is ignored. */
function readMessage(data: unknown): Message | null {
  if (typeof data !== "object" || data === null) return null;
  const message = data as Record<string, unknown>;
  if (message["shown"] === true) return { shown: true };
  if (typeof message["failed"] === "string") return { failed: message["failed"].slice(0, 500) };
  if (message["unsupported"] === "webgl") return { unsupported: "webgl" };
  if ("camera" in message) return { camera: readCamera(message["camera"]) };
  return null;
}

export function StructureViewer({
  source,
  type,
  viewer,
  camera,
  onCapture,
}: {
  source: Source;
  type: EntityType;
  viewer: Viewer;
  /** Where a saved view's camera was. */
  camera?: Camera | undefined;
  onCapture?: ((capture: Capture | null) => void) | undefined;
}) {
  const value = source.values[viewer.structure];
  const fileId = typeof value === "string" ? value : null;
  const file = fileId ? source.files[fileId] : undefined;
  const field = type.fields.find((f) => f.name === viewer.structure);
  if (!fileId) {
    return <p className="muted">It has no {field?.label.toLowerCase() ?? "structure"} yet.</p>;
  }
  if (!file)
    return <p className="muted">Its {field?.label.toLowerCase()} is a file you can’t read.</p>;
  const format = FORMATS[file.media_type];
  const download = (
    <a href={`/api/v1/files/${fileId}/content`} download={file.name}>
      Download {file.name}
    </a>
  );
  return (
    <section className="structure" aria-labelledby="structure-file">
      <p id="structure-file" className="structure-file">
        <span>{file.name}</span>{" "}
        <span className="muted">
          {formatSize(file.size)}
          {format ? ` · ${format.label}` : ""}
        </span>{" "}
        · {download}
      </p>
      {!format ? (
        <p>The structure viewer doesn’t read {file.media_type} files.</p>
      ) : file.size > MAX_STRUCTURE_BYTES ? (
        <p>
          It’s bigger than the structure viewer opens ({formatSize(MAX_STRUCTURE_BYTES)}). Download
          it to open it in a desktop viewer, such as ChimeraX or PyMOL.
        </p>
      ) : (
        <StructureFrame
          key={fileId}
          fileId={fileId}
          name={file.name}
          format={format.format}
          title={`${viewer.label} of ${source.title}`}
          camera={camera}
          onCapture={onCapture}
        />
      )}
    </section>
  );
}

function StructureFrame({
  fileId,
  name,
  format,
  title,
  camera,
  onCapture,
}: {
  fileId: string;
  name: string;
  format: "mmcif" | "pdb";
  title: string;
  camera?: Camera | undefined;
  onCapture?: ((capture: Capture | null) => void) | undefined;
}) {
  const [state, setState] = useState<State>({ phase: "reading" });
  const port = useRef<MessagePort | null>(null);
  const loads = useRef(0);
  const asked = useRef<((camera: Camera | null) => void)[]>([]);

  useEffect(() => {
    if (state.phase !== "shown" || !onCapture) return;
    onCapture(
      () =>
        new Promise((resolve, reject) => {
          const timer = setTimeout(() => {
            reject(new Error("The structure viewer didn’t say where its camera is."));
          }, CAMERA_TIMEOUT_MS);
          asked.current.push((now) => {
            clearTimeout(timer);
            resolve({ version: 1, ...(now ? { camera: now } : {}) });
          });
          port.current?.postMessage({ get: "camera" });
        }),
    );
    return () => {
      onCapture(null);
    };
  }, [state.phase, onCapture]);

  useEffect(() => {
    const abort = new AbortController();
    readFile(fileId, abort.signal).then(
      (bytes) => {
        setState({ phase: "starting", bytes });
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
  useEffect(() => () => port.current?.close(), []);
  useEffect(() => {
    if (state.phase !== "starting") return;
    const timer = setTimeout(() => {
      setState((now) =>
        now.phase === "starting"
          ? { phase: "failed", message: "The structure viewer didn’t start. Reload to try again." }
          : now,
      );
    }, SHOW_TIMEOUT_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [state.phase]);

  function loaded(event: SyntheticEvent<HTMLIFrameElement>) {
    loads.current += 1;
    if (loads.current > 1 || state.phase !== "starting") {
      // Something opened another page in its frame: whatever that is, it isn't Mol*.
      port.current?.close();
      port.current = null;
      setState({ phase: "left" });
      return;
    }
    const channel = new MessageChannel();
    channel.port1.onmessage = (incoming) => {
      const message = readMessage(incoming.data);
      if (!message) return;
      if ("camera" in message) {
        asked.current.shift()?.(message.camera);
        return;
      }
      setState((now) => {
        if (now.phase !== "starting") return now;
        if ("shown" in message) return { phase: "shown" };
        if ("unsupported" in message) return { phase: "unsupported" };
        return { phase: "failed", message: `Mol* couldn’t read ${name}: ${message.failed}` };
      });
    };
    port.current = channel.port1;
    // Its origin is no one's, so there's no origin to name but "*": the port is what's private.
    event.currentTarget.contentWindow?.postMessage({ type: "iki-connect" }, "*", [channel.port2]);
    const bytes = state.bytes;
    channel.port1.postMessage(
      { structure: bytes, format, label: name, ...(camera ? { camera } : {}) },
      [bytes],
    );
  }

  if (state.phase === "left") {
    return (
      <div className="alert alert-error" role="alert">
        <p>The structure viewer tried to open another page, so Iki closed it.</p>
      </div>
    );
  }
  if (state.phase === "unsupported") {
    return (
      <p>
        This browser can’t draw 3D structures: WebGL is off or unavailable, as it often is on
        virtual desktops. Download {name} to open it in a desktop viewer, such as ChimeraX or PyMOL.
      </p>
    );
  }
  if (state.phase === "failed") {
    return (
      <div className="alert alert-error" role="alert">
        <p>{state.message}</p>
      </div>
    );
  }
  if (state.phase === "reading") return <Loading label={`Reading ${name}…`} />;
  return (
    <>
      {state.phase === "starting" ? <Loading label={`Drawing ${name}…`} /> : null}
      <iframe
        title={title}
        src={STRUCTURE_FRAME}
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        className="structure-frame"
        onLoad={loaded}
      />
    </>
  );
}
