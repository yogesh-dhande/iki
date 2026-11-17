/**
 * The structure viewer: Mol*, in a sandboxed frame of Iki's own (ADR 0085). The page
 * reads each file, checks its bytes are the ones Iki stored, and hands them to the frame,
 * which can't read anything itself: no network, and no origin that's anyone's. A saved view
 * (ADR 0086, ADR 0110) can show several structures in one scene, each its own component:
 * the page hands the frame each one's colour, transform and selection, and where the camera
 * was, and asks for them back to save them. It can superpose the structures on the first.
 */

import type { Schemas } from "@ikisci/sdk";
import { useCallback, useEffect, useId, useRef, useState, type SyntheticEvent } from "react";

import { formatSize, type EntityType } from "../entities";
import { readStoredFile } from "../stored";
import { Button, Loading, useMessages } from "../ui";
import {
  listed,
  readCamera,
  readSelection,
  readTransform,
  STRUCTURE_COLOURS,
  type Camera,
  type Capture,
  type Span,
  type TargetState,
} from "../views";
import type { Source } from "./entity-viewer";

type Viewer = Schemas["StructureViewerOut"];

/** The formats Mol* reads here, by media type, and what they're called. */
const FORMATS: Record<string, { format: "mmcif" | "pdb"; label: string }> = {
  "chemical/x-mmcif": { format: "mmcif", label: "mmCIF" },
  "chemical/x-pdb": { format: "pdb", label: "PDB" },
};

/** Bigger files are left to desktop viewers: Mol* would need several times this in memory. */
export const MAX_STRUCTURE_BYTES = 256 * 1024 * 1024;
/** A scene's structures together: past this, the rest are left out. */
export const MAX_SCENE_BYTES = 512 * 1024 * 1024;
/** How long Mol* has to show the structures it's been given before the page says it didn't. */
export const SHOW_TIMEOUT_MS = 60_000;
export const STRUCTURE_FRAME = "/viewers/structure/";

/** One structure of a scene: an entity, as a source shows it, and what a view kept of it. */
export interface SceneItem {
  key: string;
  source: Source;
  type: EntityType;
  viewer: Viewer;
  /** What a saved view kept of it. */
  state?: TargetState | undefined;
  /** Its HRID, to tell it from others in the scene. */
  hrid?: string | undefined;
}

/** An item's file, and whether the frame can draw it. */
interface Prepared {
  item: SceneItem;
  label: string;
  fileId: string | null;
  file: Source["files"][string] | undefined;
  format: (typeof FORMATS)[string] | undefined;
  /** Why it isn't drawn, if it isn't. */
  problem: string | null;
}

type Phase =
  | { phase: "reading" }
  | { phase: "starting"; bytes: ArrayBuffer[] }
  | { phase: "shown" }
  | { phase: "failed"; message: string }
  | { phase: "unsupported" }
  | { phase: "left" };

/** How long Mol* has to say what it shows, or to superpose. */
const ASK_TIMEOUT_MS = 30_000;

interface StructureNow {
  transform?: number[];
  hidden?: boolean;
  selection?: Span[];
}
type Superposition =
  { index: number; rmsd: number; residues: number } | { index: number; failed: string };

type Message =
  | { shown: true }
  | { failed: string }
  | { unsupported: "webgl" }
  | { state: { camera: Camera | null; structures: StructureNow[] } | null }
  | { superposed: Superposition[] }
  | { error: string };

function readStructureNow(value: unknown): StructureNow {
  if (typeof value !== "object" || value === null) return {};
  const v = value as Record<string, unknown>;
  const transform = readTransform(v["transform"]);
  const selection = readSelection(v["selection"]);
  return {
    ...(transform ? { transform } : {}),
    ...(v["hidden"] === true ? { hidden: true } : {}),
    ...(selection.length ? { selection } : {}),
  };
}

function readSuperposition(value: unknown): Superposition | null {
  if (typeof value !== "object" || value === null) return null;
  const v = value as Record<string, unknown>;
  const index = v["index"];
  if (!Number.isInteger(index)) return null;
  if (typeof v["failed"] === "string")
    return { index: index as number, failed: v["failed"].slice(0, 200) };
  return typeof v["rmsd"] === "number" && Number.isInteger(v["residues"])
    ? { index: index as number, rmsd: v["rmsd"], residues: v["residues"] as number }
    : null;
}

/** What Mol* said, if it's something it says: anything else is ignored. */
function readMessage(data: unknown): Message | null {
  if (typeof data !== "object" || data === null) return null;
  const message = data as Record<string, unknown>;
  if (message["shown"] === true) return { shown: true };
  if (typeof message["failed"] === "string") return { failed: message["failed"].slice(0, 500) };
  if (typeof message["error"] === "string") return { error: message["error"].slice(0, 500) };
  if (message["unsupported"] === "webgl") return { unsupported: "webgl" };
  if ("state" in message) {
    const state = message["state"] as Record<string, unknown> | null;
    if (!state) return { state: null };
    const structures = Array.isArray(state["structures"]) ? state["structures"] : [];
    return {
      state: {
        camera: readCamera(state["camera"]),
        structures: structures.map(readStructureNow),
      },
    };
  }
  if (Array.isArray(message["superposed"])) {
    return {
      superposed: message["superposed"].flatMap((s: unknown) => {
        const read = readSuperposition(s);
        return read ? [read] : [];
      }),
    };
  }
  return null;
}

function prepare(items: SceneItem[]): Prepared[] {
  let total = 0;
  return items.map((item) => {
    const value = item.source.values[item.viewer.structure];
    const fileId = typeof value === "string" ? value : null;
    const file = fileId ? item.source.files[fileId] : undefined;
    const field = item.type.fields.find((f) => f.name === item.viewer.structure);
    const format = file ? FORMATS[file.media_type] : undefined;
    const label = item.state?.label ?? item.source.title;
    let problem: string | null = null;
    if (!fileId) problem = `It has no ${field?.label.toLowerCase() ?? "structure"} yet.`;
    else if (!file)
      problem = `Its ${field?.label.toLowerCase() ?? "structure"} is a file you can’t read.`;
    else if (!format) problem = `The structure viewer doesn’t read ${file.media_type} files.`;
    else if (file.size > MAX_STRUCTURE_BYTES)
      problem = `It’s bigger than the structure viewer opens (${formatSize(MAX_STRUCTURE_BYTES)}). Download it to open it in a desktop viewer, such as ChimeraX or PyMOL.`;
    else if (total + file.size > MAX_SCENE_BYTES)
      problem = `With the structures before it, it’s more than the structure viewer opens at once (${formatSize(MAX_SCENE_BYTES)}).`;
    if (!problem && file) total += file.size;
    return { item, label, fileId, file, format, problem };
  });
}

/** One structure, or several in one scene, each its own component. */
export function StructureScene({
  items,
  title,
  camera,
  onCapture,
}: {
  items: SceneItem[];
  /** What the frame is called, for screen readers. */
  title: string;
  /** Where a saved view's camera was. */
  camera?: Camera | undefined;
  onCapture?: ((capture: Capture | null) => void) | undefined;
}) {
  const prepared = prepare(items);
  const drawn = prepared.filter((p) => !p.problem);
  const several = items.length > 1;
  return (
    <section className="structure" aria-label={several ? title : undefined}>
      {several ? (
        <SceneLegend prepared={prepared} />
      ) : prepared[0] ? (
        <OneFile prepared={prepared[0]} />
      ) : null}
      {drawn.length ? (
        <StructureFrame
          key={drawn.map((p) => p.fileId).join(" ")}
          prepared={prepared}
          title={title}
          camera={camera}
          onCapture={onCapture}
        />
      ) : null}
    </section>
  );
}

function download(p: Prepared) {
  return p.fileId && p.file ? (
    <a href={`/api/v1/files/${p.fileId}/content`} download={p.file.name}>
      Download {p.file.name}
    </a>
  ) : null;
}

function OneFile({ prepared: p }: { prepared: Prepared }) {
  if (!p.file) return <p className="muted">{p.problem}</p>;
  return (
    <>
      <p className="structure-file">
        <span>{p.file.name}</span>{" "}
        <span className="muted">
          {formatSize(p.file.size)}
          {p.format ? ` · ${p.format.label}` : ""}
        </span>{" "}
        · {download(p)}
      </p>
      {p.problem ? <p>{p.problem}</p> : null}
    </>
  );
}

/** Which structure is which: its colour, its name and file, and why it isn't drawn. */
function SceneLegend({ prepared }: { prepared: Prepared[] }) {
  return (
    <ul className="scene-legend" aria-label="Structures">
      {prepared.map((p) => (
        <li key={p.item.key}>
          <span
            className="scene-swatch"
            aria-hidden="true"
            style={p.item.state?.colour ? { background: p.item.state.colour } : undefined}
          />
          <span>
            <strong>{p.label}</strong>
            {p.item.hrid ? <span className="muted"> {p.item.hrid}</span> : null}
            {p.file ? (
              <span className="muted">
                {" "}
                · {p.file.name}, {formatSize(p.file.size)} · {download(p)}
              </span>
            ) : null}
            {p.problem ? <span className="scene-problem"> {p.problem}</span> : null}
            {p.item.state?.superposed?.rmsd !== undefined ? (
              <span className="muted">
                {" "}
                · superposed, RMSD {p.item.state.superposed.rmsd.toFixed(2)} Å
                {p.item.state.superposed.residues !== undefined
                  ? ` over ${String(p.item.state.superposed.residues)} residues`
                  : ""}
              </span>
            ) : null}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** What the page keeps of a structure: what the frame doesn't say. */
function pageParts(s: TargetState): TargetState {
  return {
    ...(s.label ? { label: s.label } : {}),
    ...(s.colour ? { colour: s.colour } : {}),
    ...(s.superposed ? { superposed: s.superposed } : {}),
  };
}

/** The colour Iki gives a structure of several that a view hasn't coloured. */
function colourOf(state: TargetState | undefined, index: number, several: boolean) {
  if (state?.colour) return state.colour;
  return several ? STRUCTURE_COLOURS[index % STRUCTURE_COLOURS.length]?.colour : undefined;
}

function StructureFrame({
  prepared,
  title,
  camera,
  onCapture,
}: {
  prepared: Prepared[];
  title: string;
  camera?: Camera | undefined;
  onCapture?: ((capture: Capture | null) => void) | undefined;
}) {
  const drawn = prepared.filter((p) => !p.problem);
  const several = prepared.length > 1;
  const names = listed(drawn.map((p) => p.file?.name ?? p.label));
  const [phase, setPhase] = useState<Phase>({ phase: "reading" });
  // What the page keeps of each drawn structure, beyond what the frame says.
  const [kept, setKept] = useState<TargetState[]>(() =>
    drawn.map((p) => {
      const colour = colourOf(p.item.state, prepared.indexOf(p), several);
      return { ...p.item.state, ...(colour ? { colour } : {}) };
    }),
  );
  const port = useRef<MessagePort | null>(null);
  const loads = useRef(0);
  const waiting = useRef<{ want: "state" | "superposed"; done: (m: Message | null) => void }[]>([]);
  const [superposing, setSuperposing] = useState(false);
  const { say } = useMessages();
  const ids = useId();

  const ask = useCallback(
    (message: object, want: "state" | "superposed") =>
      new Promise<Message>((resolve, reject) => {
        const timer = setTimeout(() => {
          reject(new Error("The structure viewer didn’t answer."));
        }, ASK_TIMEOUT_MS);
        waiting.current.push({
          want,
          done: (answer) => {
            clearTimeout(timer);
            if (!answer || "error" in answer)
              reject(new Error(answer && "error" in answer ? answer.error : "No answer."));
            else resolve(answer);
          },
        });
        port.current?.postMessage(message);
      }),
    [],
  );

  useEffect(() => {
    if (phase.phase !== "shown" || !onCapture) return;
    onCapture(async () => {
      const answer = await ask({ get: "state" }, "state");
      const now = "state" in answer ? answer.state : null;
      // Each structure the page gave, in order, then those it couldn't draw as they were.
      let at = 0;
      const targets = prepared.map((p) => {
        if (p.problem) return p.item.state ?? {};
        const index = at++;
        // What the frame says now of where it is, whether it's hidden and what's selected,
        // over what the page keeps.
        return { ...pageParts(kept[index] ?? {}), ...now?.structures[index] };
      });
      return { ...(now?.camera ? { camera: now.camera } : {}), targets };
    });
    return () => {
      onCapture(null);
    };
  }, [phase.phase, onCapture, ask, prepared, kept]);

  const fileIds = drawn.map((p) => p.fileId ?? "").join(" ");
  useEffect(() => {
    const abort = new AbortController();
    const reading = drawn.map((p) =>
      readStoredFile(p.fileId ?? "", abort.signal).catch((error: unknown) => {
        throw new Error(
          `Iki couldn’t read ${p.file?.name ?? p.label}. ${error instanceof Error ? error.message : ""}`,
        );
      }),
    );
    Promise.all(reading).then(
      (bytes) => {
        setPhase({ phase: "starting", bytes });
      },
      (error: unknown) => {
        if (abort.signal.aborted) return;
        setPhase({ phase: "failed", message: error instanceof Error ? error.message : "" });
      },
    );
    return () => {
      abort.abort();
    };
    // The files are what matter: a new list of the same ones reads nothing again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileIds]);
  useEffect(() => () => port.current?.close(), []);
  useEffect(() => {
    if (phase.phase !== "starting") return;
    const timer = setTimeout(() => {
      setPhase((now) =>
        now.phase === "starting"
          ? { phase: "failed", message: "The structure viewer didn’t start. Reload to try again." }
          : now,
      );
    }, SHOW_TIMEOUT_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [phase.phase]);

  function loaded(event: SyntheticEvent<HTMLIFrameElement>) {
    loads.current += 1;
    if (loads.current > 1 || phase.phase !== "starting") {
      // Something opened another page in its frame: whatever that is, it isn't Mol*.
      port.current?.close();
      port.current = null;
      setPhase({ phase: "left" });
      return;
    }
    const channel = new MessageChannel();
    channel.port1.onmessage = (incoming) => {
      const message = readMessage(incoming.data);
      if (!message) return;
      if ("state" in message || "superposed" in message || "error" in message) {
        const want = "superposed" in message ? "superposed" : "state";
        const at = waiting.current.findIndex((w) => "error" in message || w.want === want);
        if (at >= 0) waiting.current.splice(at, 1)[0]?.done(message);
        return;
      }
      setPhase((now) => {
        if (now.phase !== "starting") return now;
        if ("shown" in message) return { phase: "shown" };
        if ("unsupported" in message) return { phase: "unsupported" };
        return { phase: "failed", message: `Mol* couldn’t read ${names}: ${message.failed}` };
      });
    };
    port.current = channel.port1;
    // Its origin is no one's, so there's no origin to name but "*": the port is what's private.
    event.currentTarget.contentWindow?.postMessage({ type: "iki-connect" }, "*", [channel.port2]);
    const bytes = phase.bytes;
    channel.port1.postMessage(
      {
        structures: drawn.map((p, i) => {
          const state = kept[i] ?? {};
          return {
            structure: bytes[i],
            format: p.format?.format,
            label: several ? p.label : (p.file?.name ?? p.label),
            ...(state.colour ? { colour: state.colour } : {}),
            ...(state.hidden ? { hidden: true } : {}),
            ...(state.transform ? { transform: state.transform } : {}),
            ...(state.selection ? { selection: state.selection } : {}),
          };
        }),
        ...(camera ? { camera } : {}),
      },
      bytes,
    );
  }

  function recolour(index: number, colour: string | null) {
    setKept((now) =>
      now.map((s, i) => {
        if (i !== index) return s;
        const changed = { ...s };
        if (colour) changed.colour = colour;
        else delete changed.colour;
        return changed;
      }),
    );
    port.current?.postMessage({ colour: { index, colour } });
  }

  function hide(index: number, hidden: boolean) {
    setKept((now) => now.map((s, i) => (i === index ? { ...s, hidden } : s)));
    port.current?.postMessage({ hidden: { index, hidden } });
  }

  async function superpose() {
    setSuperposing(true);
    try {
      const answer = await ask({ superpose: { onto: 0 } }, "superposed");
      const results = "superposed" in answer ? answer.superposed : [];
      const first = drawn[0];
      setKept((now) =>
        now.map((s, i) => {
          const result = results.find((r) => r.index === i);
          if (!result || "failed" in result) return s;
          const onto = first ? prepared.indexOf(first) : 0;
          return { ...s, superposed: { onto, rmsd: result.rmsd, residues: result.residues } };
        }),
      );
      say(
        results
          .map((r) => {
            const name = drawn[r.index]?.label ?? "";
            return "failed" in r
              ? `${name} wasn’t superposed: ${r.failed}.`
              : `${name}: RMSD ${r.rmsd.toFixed(2)} Å over ${String(r.residues)} residues.`;
          })
          .join(" ") || "There was nothing to superpose.",
      );
    } catch (error) {
      say(error instanceof Error ? error.message : "The structure viewer didn’t superpose them.");
    } finally {
      setSuperposing(false);
    }
  }

  if (phase.phase === "left") {
    return (
      <div className="alert alert-error" role="alert">
        <p>The structure viewer tried to open another page, so Iki closed it.</p>
      </div>
    );
  }
  if (phase.phase === "unsupported") {
    return (
      <p>
        This browser can’t draw 3D structures: WebGL is off or unavailable, as it often is on
        virtual desktops. Download {names} to open {drawn.length > 1 ? "them" : "it"} in a desktop
        viewer, such as ChimeraX or PyMOL.
      </p>
    );
  }
  if (phase.phase === "failed") {
    return (
      <div className="alert alert-error" role="alert">
        <p>{phase.message}</p>
      </div>
    );
  }
  if (phase.phase === "reading") return <Loading label={`Reading ${names}…`} />;
  return (
    <>
      {phase.phase === "starting" ? <Loading label={`Drawing ${names}…`} /> : null}
      {several && phase.phase === "shown" ? (
        <div className="scene-controls">
          {drawn.map((p, i) => (
            <fieldset key={p.item.key} className="scene-structure">
              <legend>{p.label}</legend>
              <label htmlFor={`${ids}-colour-${String(i)}`}>Colour</label>
              <select
                id={`${ids}-colour-${String(i)}`}
                value={kept[i]?.colour ?? ""}
                onChange={(event) => {
                  recolour(i, event.target.value || null);
                }}
              >
                <option value="">By chain</option>
                {STRUCTURE_COLOURS.map((c) => (
                  <option key={c.colour} value={c.colour}>
                    {c.name}
                  </option>
                ))}
              </select>
              <label className="choice" htmlFor={`${ids}-shown-${String(i)}`}>
                <input
                  id={`${ids}-shown-${String(i)}`}
                  type="checkbox"
                  checked={!kept[i]?.hidden}
                  onChange={(event) => {
                    hide(i, !event.target.checked);
                  }}
                />
                <span className="choice-text">Shown</span>
              </label>
            </fieldset>
          ))}
          {drawn.length > 1 ? (
            <Button
              busy={superposing}
              busyText="Superposing…"
              onClick={() => {
                void superpose();
              }}
            >
              Superpose on {drawn[0]?.label}
            </Button>
          ) : null}
        </div>
      ) : null}
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

/** One entity's structure, on its tab or in a view of it alone. */
export function StructureViewer({
  source,
  type,
  viewer,
  camera,
  state,
  onCapture,
}: {
  source: Source;
  type: EntityType;
  viewer: Viewer;
  /** Where a saved view's camera was. */
  camera?: Camera | undefined;
  /** What a saved view kept of it. */
  state?: TargetState | undefined;
  onCapture?: ((capture: Capture | null) => void) | undefined;
}) {
  return (
    <StructureScene
      items={[{ key: "structure", source, type, viewer, state }]}
      title={`${viewer.label} of ${source.title}`}
      camera={camera}
      onCapture={onCapture}
    />
  );
}
