/**
 * Scanning at the bench (ADR 0180). A keyboard-mode scanner sends a burst of
 * characters followed by Enter or Tab. A text field always takes ordinary input.
 */

import { useEffect, useRef } from "react";

/** A normal interval between scanner keys. One interval may include a stall. */
export const MAX_GAP_MS = 50;
const MAX_STALL_MS = 250;
/** The shortest code taken as a scan: an HRID or a barcode is longer. */
export const MIN_LENGTH = 4;

export type Fed =
  | { kind: "none" }
  /** Part of what may be a scan: `swallow` once it's fast enough not to be typing. */
  | { kind: "char"; swallow: boolean }
  | { kind: "scan"; code: string }
  | { kind: "rejected" };

/** Tells a scanner's keystrokes from a person's, a key at a time. */
export class Wedge {
  private chars: string[] = [];
  private last = Number.NEGATIVE_INFINITY;
  private stalls = 0;
  private plausible = false;

  /** A key pressed at `at` (ms): whether it's part of a scan, or ends one. */
  feed(key: string, at: number, modified = false): Fed {
    if (modified) {
      this.reset();
      return { kind: "none" };
    }
    const end = key === "Enter" || key === "Tab";
    if (!end && key.length !== 1) {
      // Scanners use Shift for capitals.
      return { kind: "none" };
    }

    const gap = at - this.last;
    if (!this.plausible && gap > MAX_STALL_MS) this.reset();
    if (this.last !== Number.NEGATIVE_INFINITY) {
      if (gap > MAX_STALL_MS) this.stalls = 2;
      else if (gap > MAX_GAP_MS) this.stalls++;
      else if (!end) this.plausible = true;
    }

    if (end) {
      let result: Fed = { kind: "none" };
      if (this.plausible) {
        result =
          this.stalls <= 1 && this.chars.length >= MIN_LENGTH
            ? { kind: "scan", code: this.chars.join("") }
            : { kind: "rejected" };
      }
      this.reset();
      return result;
    }

    // Once two characters arrive quickly, keep the terminator even if later timing
    // rules out the scan. Never start a second code from the rejected burst's tail.
    if (this.stalls <= 1) this.chars.push(key);
    this.last = at;
    return { kind: "char", swallow: this.plausible };
  }

  reset(): void {
    this.chars = [];
    this.last = Number.NEGATIVE_INFINITY;
    this.stalls = 0;
    this.plausible = false;
  }
}

/** Whether typing there is typing: a text field, a text area, or something editable. */
export function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  if (target instanceof HTMLTextAreaElement) return true;
  if (target instanceof HTMLSelectElement) return true;
  if (target instanceof HTMLInputElement) {
    return !["button", "checkbox", "radio", "submit", "reset", "range", "color"].includes(
      target.type,
    );
  }
  return false;
}

/** Who's listening, the latest last: a dialog's scan box, over its page's, takes the scans. */
const listeners: { current: (code: string | null) => void }[] = [];
let wedge: Wedge | null = null;

function onKeyDown(event: KeyboardEvent) {
  if (!wedge) return;
  if (event.isComposing || isEditable(event.target)) {
    wedge.reset();
    return;
  }
  const fed = wedge.feed(
    event.key,
    event.timeStamp || performance.now(),
    event.ctrlKey || event.metaKey || event.altKey,
  );
  if (fed.kind === "none" || (fed.kind === "char" && !fed.swallow)) return;
  event.preventDefault();
  event.stopPropagation();
  if (fed.kind !== "char") listeners.at(-1)?.current(fed.kind === "scan" ? fed.code : null);
}

/**
 * Calls `onScan` with each code a keyboard-mode scanner types while focus isn't in a text
 * field: on a box's map, a button, or the page itself. Its keys go no further, so a scan's
 * Enter doesn't press the button that has focus. A rejected burst reports null.
 * The most recent listener receives it, so a dialog takes precedence over its page.
 */
export function useScanner(onScan: (code: string | null) => void, enabled = true): void {
  const latest = useRef(onScan);
  useEffect(() => {
    latest.current = onScan;
  });
  useEffect(() => {
    if (!enabled) return;
    const me = {
      current: (code: string | null) => {
        latest.current(code);
      },
    };
    listeners.push(me);
    if (listeners.length === 1) {
      wedge = new Wedge();
      document.addEventListener("keydown", onKeyDown, true);
    }
    return () => {
      listeners.splice(listeners.indexOf(me), 1);
      if (!listeners.length) {
        document.removeEventListener("keydown", onKeyDown, true);
        wedge = null;
      }
    };
  }, [enabled]);
}

// --- The camera ---------------------------------------------------------------------------------

/** The codes Iki's labels and tubes carry. */
export const FORMATS = ["data_matrix", "qr_code", "code_128"] as const;

interface Detected {
  rawValue: string;
}

interface Detector {
  detect(source: CanvasImageSource): Promise<Detected[]>;
}

interface DetectorClass {
  new (options: { formats: string[] }): Detector;
  getSupportedFormats(): Promise<string[]>;
}

function detectorClass(): DetectorClass | undefined {
  return (globalThis as { BarcodeDetector?: DetectorClass }).BarcodeDetector;
}

/** Why the camera can't read codes here, or null if it can. */
export async function cameraUnavailable(): Promise<string | null> {
  const found = detectorClass();
  if (!found) {
    return "This browser can’t read codes from the camera. Use a scanner, or type the code.";
  }
  // Only on a secure origin: plain HTTP has no mediaDevices.
  if (!(navigator as Partial<Navigator>).mediaDevices) {
    return "This browser can’t use a camera here. Use a scanner, or type the code.";
  }
  try {
    const supported = await found.getSupportedFormats();
    if (!FORMATS.some((f) => supported.includes(f))) {
      return "This browser’s camera reader doesn’t read DataMatrix, QR or Code 128 codes.";
    }
  } catch {
    return "This browser can’t read codes from the camera. Use a scanner, or type the code.";
  }
  return null;
}

/** A detector for the formats this browser reads of Iki's. */
export async function makeDetector(): Promise<Detector | null> {
  const found = detectorClass();
  if (!found) return null;
  const supported = await found.getSupportedFormats();
  const formats = FORMATS.filter((f) => supported.includes(f));
  return formats.length ? new found({ formats: [...formats] }) : null;
}
