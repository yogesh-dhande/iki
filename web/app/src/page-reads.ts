/** Reads for a document can resume if the browser restores it from the back/forward cache. */
const left = new DOMException("The document is leaving.", "AbortError");
let lifetime = new AbortController();
let restored: Promise<void> | undefined;
let restore: (() => void) | undefined;

window.addEventListener(
  "pagehide",
  () => {
    if (restored) return;
    restored = new Promise<void>((resolve) => {
      restore = resolve;
    });
    lifetime.abort(left);
  },
  { capture: true },
);
window.addEventListener(
  "pageshow",
  (event) => {
    if (!event.persisted || !restored) return;
    lifetime = new AbortController();
    restored = undefined;
    restore?.();
    restore = undefined;
  },
  { capture: true },
);

/** Both the query's caller and the outgoing document may cancel this read. */
export function pageReadSignal(signal: AbortSignal): AbortSignal {
  return AbortSignal.any([signal, lifetime.signal]);
}

/** The SDK finishes signing before it reaches transport. A read that was still
 * preparing when the document left must also wait here, including between pages
 * of a query. An explicit operation such as an upload keeps its own lifetime. */
export const fetchPageRead: typeof fetch = async (input, init) => {
  const signal =
    init?.signal === undefined && input instanceof Request ? input.signal : init?.signal;
  while (restored) {
    signal?.throwIfAborted();
    await restored;
  }
  signal?.throwIfAborted();
  return globalThis.fetch(input, init);
};

/** A query scheduled during departure waits. A read interrupted by pagehide starts
 * again with fresh request parameters on restoration, if its caller still wants it. */
export async function runPageQuery<T>(
  read: () => T | Promise<T>,
  caller: () => AbortSignal,
): Promise<T> {
  for (;;) {
    while (restored) {
      const signal = caller();
      await restored;
      signal.throwIfAborted();
    }
    try {
      return await read();
    } catch (error) {
      if (error !== left) throw error;
      caller().throwIfAborted();
    }
  }
}
