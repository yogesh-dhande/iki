/** The Trusted Types policy that creates the parsers' workers: Iki's own scripts only. */

import { describe, expect, it } from "vitest";

import { workerScriptUrl } from "./reading";

describe("which scripts may run as workers", () => {
  it("allows the app's own worker scripts", () => {
    const url = `${location.origin}/static/app/assets/worker-reader-BfcaIDAH.js`;
    expect(workerScriptUrl(url)).toBe(url);
  });

  it("refuses scripts from anywhere else", () => {
    expect(() => workerScriptUrl("https://cdn.example.com/assets/worker-reader-1.js")).toThrow(
      "isn't one of Iki's workers",
    );
    expect(() => workerScriptUrl("data:text/javascript,postMessage(1)")).toThrow();
    expect(() => workerScriptUrl("blob:https://cdn.example.com/1")).toThrow();
  });
});
