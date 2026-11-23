/**
 * Scanning at the bench (ADR 0180). A USB or Bluetooth scanner in keyboard mode types its
 * code faster than anyone can, then Enter or Tab: a burst of at least four characters, each
 * within 50 ms of the last, is a scan wherever focus is on the storage pages, unless it's in
 * a text field, which takes what's typed as typing. A camera reads codes with the browser's
 * BarcodeDetector, where it has one.
 */

import { useEffect, useRef } from "react";

/** The longest a scanner's keystrokes are apart; people type slower. */
export const MAX_GAP_MS = 50;
/** The shortest code taken as a scan: an HRID or a barcode is longer. */
export const MIN_LENGTH = 4;

export type Fed =
  | { kind: "none" }
  /** Part of what may be a scan: `swallow` once it's fast enough not to be typing. */
  | { kind: "char"; swallow: boolean }
  | { kind: "scan"; code: string };

/** Tells a scanner's keystrokes from a person's, a key at a time. */
export class Wedge {
  private chars: string[] = [];
  private last = Number.NEGATIVE_INFINITY;

  constructor(
    private readonly maxGap = MAX_GAP_MS,
    private readonly minLength = MIN_LENGTH,
  ) {}

  /** A key pressed at `at` (ms): whether it's part of a scan, or ends one. */
  feed(key: string, at: number, modified = false): Fed {
    if (modified) {
      this.reset();
      return { kind: "none" };
    }
    if (key === "Enter" || key === "Tab") {
      const code = this.chars.join("");
      const fast = at - this.last <= this.maxGap * 2;
      this.reset();
      return code.length >= this.minLength && fast ? { kind: "scan", code } : { kind: "none" };
    }
    if (key.length !== 1) {
      // Shift, which a scanner presses for capitals, and the like.
      return { kind: "none" };
    }
    if (at - this.last > this.maxGap) this.chars = [];
    this.chars.push(key);
    this.last = at;
    return { kind: "char", swallow: this.chars.length >= 2 };
  }

  reset(): void {
    this.chars = [];
    this.last = Number.NEGATIVE_INFINITY;
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
const listeners: { current: (code: string) => void }[] = [];
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
  if (fed.kind === "scan" || (fed.kind === "char" && fed.swallow)) {
    event.preventDefault();
    event.stopPropagation();
  }
  if (fed.kind === "scan") listeners.at(-1)?.current(fed.code);
}

/**
 * Calls `onScan` with each code a keyboard-mode scanner types while focus isn't in a text
 * field: on a box's map, a button, or the page itself. Its keys go no further, so a scan's
 * Enter doesn't press the button that has focus. Of several on screen, the one that started
 * listening last hears it: a dialog's, not the page's under it.
 */
export function useScanner(onScan: (code: string) => void, enabled = true): void {
  const latest = useRef(onScan);
  useEffect(() => {
    latest.current = onScan;
  });
  useEffect(() => {
    if (!enabled) return;
    const me = {
      current: (code: string) => {
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
