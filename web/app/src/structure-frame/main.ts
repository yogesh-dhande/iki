/** The host keeps its file access, idle notifications and private port in the opaque frame. */
import {
  StructureViewer,
  type Span,
  type StructureCamera,
  type StructureInput,
  type StructureScene,
} from "@ikisci/science/structure-viewer";

// The frame's Permissions-Policy denies XR, including Mol*'s capability probe.
Object.defineProperty(navigator, "xr", { value: undefined, configurable: true });

type Ask =
  | { get: "state" }
  | { superpose: { onto: number } }
  | { colour: { index: number; colour: string | null } }
  | { hidden: { index: number; hidden: boolean } };

const FORMATS = new Set(["mmcif", "pdb"]);

/** What the page gave, if it's a scene to show: anything else is ignored. */
function readShow(data: unknown): StructureScene | null {
  if (typeof data !== "object" || data === null) return null;
  const { structures, camera } = data as Record<string, unknown>;
  if (!Array.isArray(structures) || !structures.length) return null;
  const given = structures.flatMap((s: unknown) => {
    if (typeof s !== "object" || s === null) return [];
    const v = s as Record<string, unknown>;
    if (!(v["structure"] instanceof ArrayBuffer) || !FORMATS.has(String(v["format"]))) return [];
    const transform = v["transform"];
    return [
      {
        structure: v["structure"],
        format: v["format"] as StructureInput["format"],
        label: typeof v["label"] === "string" ? v["label"].slice(0, 200) : "Structure",
        ...(typeof v["colour"] === "string" && /^#[0-9a-f]{6}$/.test(v["colour"])
          ? { colour: v["colour"] }
          : {}),
        ...(v["hidden"] === true ? { hidden: true } : {}),
        ...(Array.isArray(transform) &&
        transform.length === 16 &&
        transform.every((n) => typeof n === "number" && Number.isFinite(n))
          ? { transform: transform as number[] }
          : {}),
        ...(Array.isArray(v["selection"]) ? { selection: v["selection"] as Span[] } : {}),
      },
    ];
  });
  if (given.length !== structures.length) return null;
  return { structures: given, ...(camera ? { camera: camera as StructureCamera } : {}) };
}

async function answer(viewer: StructureViewer, ask: Ask, port: MessagePort): Promise<void> {
  if ("get" in ask) port.postMessage({ state: viewer.getState() });
  else if ("superpose" in ask) {
    port.postMessage({ superposed: await viewer.superpose(ask.superpose.onto) });
  } else if ("colour" in ask) {
    await viewer.setColour(ask.colour.index, ask.colour.colour);
  } else {
    viewer.setHidden(ask.hidden.index, ask.hidden.hidden);
  }
}

function readAsk(data: unknown, count: number): Ask | null {
  if (typeof data !== "object" || data === null) return null;
  const v = data as Record<string, unknown>;
  const index = (o: unknown) => {
    const i = (o as Record<string, unknown> | null)?.["index"];
    return Number.isInteger(i) && (i as number) >= 0 && (i as number) < count;
  };
  if (v["get"] === "state") return { get: "state" };
  const onto = (v["superpose"] as Record<string, unknown> | null | undefined)?.["onto"];
  if (Number.isInteger(onto) && (onto as number) >= 0 && (onto as number) < count)
    return { superpose: { onto: onto as number } };
  if (index(v["colour"])) {
    const c = v["colour"] as { index: number; colour: unknown };
    const value = typeof c.colour === "string" && /^#[0-9a-f]{6}$/.test(c.colour) ? c.colour : null;
    return { colour: { index: c.index, colour: value } };
  }
  if (index(v["hidden"])) {
    const h = v["hidden"] as { index: number; hidden: unknown };
    return { hidden: { index: h.index, hidden: h.hidden === true } };
  }
  return null;
}

/** How often the frame tells the page that the person is turning or zooming the structure,
 * which the page can't see: so their session's idle timeout counts it (ADR 0153). */
const INPUT_EVERY_MS = 15_000;

function tellOfInput(port: MessagePort): void {
  let told = 0;
  const tell = () => {
    const now = Date.now();
    if (now - told < INPUT_EVERY_MS) return;
    told = now;
    port.postMessage({ input: true });
  };
  for (const type of ["pointerdown", "pointermove", "keydown", "wheel", "touchstart"]) {
    window.addEventListener(type, tell, { capture: true, passive: true });
  }
}

function accept(event: MessageEvent): void {
  // Only Iki may frame this page (frame-ancestors), so the parent's port is Iki's. Mol*
  // posts messages to its own window too (to schedule work), so wait for that one.
  const port = event.ports[0];
  if (event.source !== window.parent || !port) return;
  window.removeEventListener("message", accept);
  tellOfInput(port);
  let viewer: StructureViewer | null = null;
  let count = 0;
  port.onmessage = (message: MessageEvent<unknown>) => {
    if (viewer) {
      const ask = readAsk(message.data, count);
      if (ask)
        answer(viewer, ask, port).catch((error: unknown) => {
          port.postMessage({ error: error instanceof Error ? error.message : String(error) });
        });
      return;
    }
    const scene = readShow(message.data);
    const target = document.getElementById("structure");
    if (!target || !scene) return;
    count = scene.structures.length;
    viewer = new StructureViewer();
    viewer.style.height = "100%";
    viewer.addEventListener("structure-ready", () => {
      port.postMessage({ shown: true });
    });
    viewer.addEventListener("structure-unsupported", () => {
      port.postMessage({ unsupported: "webgl" });
    });
    viewer.addEventListener("structure-error", (event) => {
      port.postMessage({ failed: event.detail });
    });
    viewer.data = scene;
    target.replaceChildren(viewer);
  };
}

window.addEventListener("message", accept);
