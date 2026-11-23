/**
 * Every page read carries the document's lifetime, whatever signal its caller passes.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchPageRead } from "./page-reads";

function transition(name: "pagehide" | "pageshow") {
  window.dispatchEvent(new PageTransitionEvent(name, { persisted: true }));
}

/** The transport: each request it was sent, held until the test answers it or its signal
 * aborts, when it rejects with the signal's reason as the browser's fetch does. */
function transport() {
  const sent: { request: Request; answer: () => void }[] = [];
  const spy = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const request = new Request(input, init);
    return new Promise<Response>((resolve, reject) => {
      sent.push({
        request,
        answer: () => {
          resolve(Response.json({ ok: true }));
        },
      });
      request.signal.addEventListener("abort", () => {
        reject(request.signal.reason as Error);
      });
    });
  });
  return { sent, spy };
}

const url = "http://localhost/api/v1/projects?limit=100";

afterEach(() => {
  vi.restoreAllMocks();
  // A test that failed while the page was hidden leaves it shown for the next.
  transition("pageshow");
});

describe.each([
  { engine: "AbortSignal.any", any: true },
  { engine: "a browser without AbortSignal.any", any: false },
])("a page read, in $engine", ({ any }) => {
  /** Runs a test as the engine would: without `AbortSignal.any`, if it lacks it. */
  async function withEngine(test: () => Promise<void>) {
    const descriptor = Object.getOwnPropertyDescriptor(AbortSignal, "any");
    if (!any) Reflect.deleteProperty(AbortSignal, "any");
    try {
      expect("any" in AbortSignal).toBe(any);
      await test();
    } finally {
      if (descriptor) Object.defineProperty(AbortSignal, "any", descriptor);
    }
  }

  it("stops when the page goes away, though its caller passed no signal", () =>
    withEngine(async () => {
      const { sent } = transport();
      const read = fetchPageRead(url);
      await vi.waitFor(() => {
        expect(sent).toHaveLength(1);
      });
      transition("pagehide");
      try {
        await expect(read).rejects.toMatchObject({
          name: "AbortError",
          message: "The document is leaving.",
        });
      } finally {
        transition("pageshow");
      }
    }));

  it.each([
    { how: "in its options", read: (signal: AbortSignal) => fetchPageRead(url, { signal }) },
    {
      how: "on its request, as the SDK sends it",
      read: (signal: AbortSignal) => fetchPageRead(new Request(url, { signal })),
    },
  ])("stops when the page goes away, with a signal of its caller's $how", ({ read }) =>
    withEngine(async () => {
      const { sent } = transport();
      const caller = new AbortController();
      const reading = read(caller.signal);
      await vi.waitFor(() => {
        expect(sent).toHaveLength(1);
      });
      expect(sent[0]?.request.signal.aborted).toBe(false);
      transition("pagehide");
      try {
        await expect(reading).rejects.toMatchObject({ message: "The document is leaving." });
        expect(caller.signal.aborted).toBe(false);
      } finally {
        transition("pageshow");
      }
    }),
  );

  it.each([
    { how: "in its options", read: (signal: AbortSignal) => fetchPageRead(url, { signal }) },
    {
      how: "on its request",
      read: (signal: AbortSignal) => fetchPageRead(new Request(url, { signal })),
    },
  ])("stops when its caller cancels it, with the caller's reason, by a signal $how", ({ read }) =>
    withEngine(async () => {
      const { sent } = transport();
      const caller = new AbortController();
      const reading = read(caller.signal);
      await vi.waitFor(() => {
        expect(sent).toHaveLength(1);
      });
      const unmounted = new DOMException("Unmounted.", "AbortError");
      caller.abort(unmounted);
      await expect(reading).rejects.toBe(unmounted);
    }),
  );

  it("doesn't start for a caller that already cancelled", () =>
    withEngine(async () => {
      const { spy } = transport();
      const caller = new AbortController();
      caller.abort();
      await expect(fetchPageRead(url, { signal: caller.signal })).rejects.toMatchObject({
        name: "AbortError",
      });
      expect(spy).not.toHaveBeenCalled();
    }));

  it("is answered while the page stays, and a later page hide changes nothing", () =>
    withEngine(async () => {
      const { sent } = transport();
      const caller = new AbortController();
      const reading = fetchPageRead(url, { signal: caller.signal });
      await vi.waitFor(() => {
        expect(sent).toHaveLength(1);
      });
      sent[0]?.answer();
      const response = await reading;
      expect(await response.json()).toEqual({ ok: true });
      transition("pagehide");
      transition("pageshow");
      expect(caller.signal.aborted).toBe(false);
    }));

  it("waits while the page is hidden, then starts with the restored page's lifetime", () =>
    withEngine(async () => {
      const { sent } = transport();
      transition("pagehide");
      const reading = fetchPageRead(url, { signal: new AbortController().signal });
      await Promise.resolve();
      expect(sent).toHaveLength(0);
      transition("pageshow");
      await vi.waitFor(() => {
        expect(sent).toHaveLength(1);
      });
      expect(sent[0]?.request.signal.aborted).toBe(false);
      sent[0]?.answer();
      await expect(reading).resolves.toBeInstanceOf(Response);
    }));
});

describe("a navigation away", () => {
  function leave() {
    window.dispatchEvent(new Event("beforeunload", { cancelable: true }));
  }

  it("stops the page's reads as it begins, before the browser cuts them off", async () => {
    const { sent } = transport();
    const reading = fetchPageRead(url, { signal: new AbortController().signal });
    await vi.waitFor(() => {
      expect(sent).toHaveLength(1);
    });
    leave();
    await expect(reading).rejects.toMatchObject({ message: "The document is leaving." });
    const later = fetchPageRead(url);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(sent).toHaveLength(1);
    transition("pagehide");
    transition("pageshow");
    await vi.waitFor(() => {
      expect(sent).toHaveLength(2);
    });
    sent[1]?.answer();
    await expect(later).resolves.toBeInstanceOf(Response);
  });

  it.each(["pointerdown", "keydown", "touchstart"])(
    "that doesn't happen, as with a download, resumes them on the next %s on the page",
    async (type) => {
      const { sent } = transport();
      leave();
      const reading = fetchPageRead(url);
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(sent).toHaveLength(0);
      document.body.dispatchEvent(new Event(type, { bubbles: true }));
      await vi.waitFor(() => {
        expect(sent).toHaveLength(1);
      });
      expect(sent[0]?.request.signal.aborted).toBe(false);
      sent[0]?.answer();
      await expect(reading).resolves.toBeInstanceOf(Response);
    },
  );

  it("that the page left for stays stopped whatever the old page hears, until restoration", async () => {
    const { sent } = transport();
    leave();
    transition("pagehide");
    const reading = fetchPageRead(url);
    document.body.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(sent).toHaveLength(0);
    transition("pageshow");
    await vi.waitFor(() => {
      expect(sent).toHaveLength(1);
    });
    sent[0]?.answer();
    await expect(reading).resolves.toBeInstanceOf(Response);
  });
});
