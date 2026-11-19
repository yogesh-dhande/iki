/**
 * Only what the person does keeps their session open (ADR 0153): what the page asks for by
 * itself, once they've left it alone, says so.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ACTIVE_WITHIN_MS,
  BACKGROUND_HEADER,
  isIdle,
  resetActivity,
  whileWorking,
} from "./activity";
import { iki } from "./api";
import { answer, calls } from "./test-server";

describe("the person's activity", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    resetActivity();
    answer("GET /api/v1/me", {});
  });

  afterEach(() => {
    vi.useRealTimers();
    resetActivity();
  });

  async function sentHeader(): Promise<string | null> {
    await iki.GET("/api/v1/me");
    return calls("GET", "/api/v1/me").at(-1)?.headers.get(BACKGROUND_HEADER) ?? null;
  }

  it("counts requests while the person uses the page", async () => {
    expect(isIdle()).toBe(false);
    expect(await sentHeader()).toBeNull();
  });

  it("marks requests once nobody has touched the page for a while", async () => {
    vi.advanceTimersByTime(ACTIVE_WITHIN_MS + 1);
    expect(isIdle()).toBe(true);
    expect(await sentHeader()).toBe("1");
  });

  it.each(["pointerdown", "pointermove", "keydown", "wheel", "touchstart"])(
    "counts %s as the person being there",
    async (type) => {
      vi.advanceTimersByTime(ACTIVE_WITHIN_MS + 1);
      window.dispatchEvent(new Event(type));
      expect(isIdle()).toBe(false);
      expect(await sentHeader()).toBeNull();
    },
  );

  it("counts work the person waits on, such as an upload, while it runs", async () => {
    let finish!: () => void;
    const upload = whileWorking(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    vi.advanceTimersByTime(10 * ACTIVE_WITHIN_MS);
    expect(await sentHeader()).toBeNull();
    finish();
    await upload;
    expect(await sentHeader()).toBe("1");
  });
});
