/**
 * Scanning (ADR 0180): telling a keyboard-mode scanner's keystrokes from a person's, and
 * which scan box on screen hears a scan.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { cameraUnavailable, isEditable, MAX_GAP_MS, useScanner, Wedge } from "./scan";

/** Feeds `text` then Enter, `gap` ms apart, from `start`. */
function feedAll(wedge: Wedge, text: string, gap: number, start = 1000) {
  const out = Array.from(text).map((key, i) => wedge.feed(key, start + i * gap));
  out.push(wedge.feed("Enter", start + text.length * gap));
  return out;
}

describe("the keyboard wedge", () => {
  it("takes a fast burst ending in Enter as a scan", () => {
    const out = feedAll(new Wedge(), "TUBE-000012", 8);
    expect(out.at(-1)).toEqual({ kind: "scan", code: "TUBE-000012" });
    // From its second key it's too fast to be typing, so its keys go no further.
    expect(out[0]).toEqual({ kind: "char", swallow: false });
    expect(out[1]).toEqual({ kind: "char", swallow: true });
  });

  it("ends a scan on Tab too, as scanners can be set to", () => {
    const wedge = new Wedge();
    Array.from("LB-0042").forEach((k, i) => wedge.feed(k, 100 + i * 5));
    expect(wedge.feed("Tab", 140)).toEqual({ kind: "scan", code: "LB-0042" });
  });

  it("ignores typing, which is slower than a scanner", () => {
    const out = feedAll(new Wedge(), "TUBE-1", MAX_GAP_MS + 80);
    expect(out.at(-1)).toEqual({ kind: "none" });
    expect(out.every((f) => f.kind !== "char" || !f.swallow)).toBe(true);
  });

  it("ignores a burst too short to be a code", () => {
    expect(feedAll(new Wedge(), "A1", 5).at(-1)).toEqual({ kind: "none" });
  });

  it("starts again after a pause, so typed keys before a scan aren't part of it", () => {
    const wedge = new Wedge();
    wedge.feed("x", 0);
    wedge.feed("y", 400);
    const out = feedAll(wedge, "BOX-000014", 6, 2000);
    expect(out.at(-1)).toEqual({ kind: "scan", code: "BOX-000014" });
  });

  it("drops a burst when a modifier is held: a shortcut, not a scan", () => {
    const wedge = new Wedge();
    Array.from("TUBE").forEach((k, i) => wedge.feed(k, i * 5));
    expect(wedge.feed("v", 25, true)).toEqual({ kind: "none" });
    expect(wedge.feed("Enter", 30)).toEqual({ kind: "none" });
  });

  it("passes Shift by, which a scanner presses for capitals", () => {
    const wedge = new Wedge();
    wedge.feed("Shift", 0);
    Array.from("ABCD").forEach((k, i) => wedge.feed(k, 1 + i * 5));
    expect(wedge.feed("Enter", 30)).toEqual({ kind: "scan", code: "ABCD" });
  });
});

describe("where typing is typing", () => {
  it("is a text field, a text area, a select, or something editable; not a button", () => {
    const text = document.createElement("input");
    const box = document.createElement("input");
    box.type = "checkbox";
    const area = document.createElement("textarea");
    const button = document.createElement("button");
    expect(isEditable(text)).toBe(true);
    expect(isEditable(area)).toBe(true);
    expect(isEditable(box)).toBe(false);
    expect(isEditable(button)).toBe(false);
    expect(isEditable(document.body)).toBe(false);
  });
});

/** Types `code` as a scanner would, at the element with focus. */
function scan(code: string, target: EventTarget = document.body) {
  let t = 1000;
  for (const key of [...Array.from(code), "Enter"]) {
    const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
    Object.defineProperty(event, "timeStamp", { value: (t += 5) });
    target.dispatchEvent(event);
  }
}

describe("useScanner", () => {
  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("hears a scan wherever focus is but a text field", () => {
    const heard = vi.fn();
    renderHook(() => {
      useScanner(heard);
    });
    const button = document.createElement("button");
    document.body.append(button);
    act(() => {
      scan("TUBE-000012", button);
    });
    expect(heard).toHaveBeenCalledWith("TUBE-000012");
    const input = document.createElement("input");
    document.body.append(input);
    act(() => {
      scan("TUBE-000013", input);
    });
    expect(heard).toHaveBeenCalledTimes(1);
  });

  it("keeps a scan's Enter from pressing the button with focus", () => {
    renderHook(() => {
      useScanner(() => undefined);
    });
    const pressed = vi.fn();
    const button = document.createElement("button");
    button.addEventListener("keydown", (e) => {
      if (e.key === "Enter") pressed();
    });
    document.body.append(button);
    act(() => {
      scan("TUBE-000012", button);
    });
    expect(pressed).not.toHaveBeenCalled();
  });

  it("gives a scan to the latest listening, a dialog's over its page's, then back", () => {
    const page = vi.fn();
    const dialog = vi.fn();
    renderHook(() => {
      useScanner(page);
    });
    const open = renderHook(() => {
      useScanner(dialog);
    });
    act(() => {
      scan("BOX-000014");
    });
    expect(dialog).toHaveBeenCalledWith("BOX-000014");
    expect(page).not.toHaveBeenCalled();
    open.unmount();
    act(() => {
      scan("BOX-000015");
    });
    expect(page).toHaveBeenCalledWith("BOX-000015");
  });

  it("hears nothing while it's turned off", () => {
    const heard = vi.fn();
    renderHook(() => {
      useScanner(heard, false);
    });
    act(() => {
      scan("TUBE-000012");
    });
    expect(heard).not.toHaveBeenCalled();
  });
});

describe("the camera", () => {
  afterEach(() => {
    delete (globalThis as { BarcodeDetector?: unknown }).BarcodeDetector;
  });

  it("says why it can't read codes in a browser without a reader", async () => {
    expect(await cameraUnavailable()).toMatch(/can’t read codes from the camera/);
  });

  it("says so when the reader doesn't read Iki's codes", async () => {
    (globalThis as { BarcodeDetector?: unknown }).BarcodeDetector = {
      getSupportedFormats: () => Promise.resolve(["ean_13"]),
    };
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia: () => Promise.reject(new Error("no")) },
      configurable: true,
    });
    expect(await cameraUnavailable()).toMatch(/doesn’t read DataMatrix, QR or Code 128/);
    (globalThis as { BarcodeDetector?: unknown }).BarcodeDetector = {
      getSupportedFormats: () => Promise.resolve(["data_matrix"]),
    };
    expect(await cameraUnavailable()).toBeNull();
  });
});
