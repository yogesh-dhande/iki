/** Reads for a document can resume if the browser restores it from the back/forward cache. */
const left = new DOMException("The document is leaving.", "AbortError");
let lifetime = new AbortController();
let restored: Promise<void> | undefined;
let restore: (() => void) | undefined;
/** Listening for a sign that a departure was called off, from beforeunload until pagehide. */
let watching: AbortController | undefined;

function depart(): void {
  if (restored) return;
  restored = new Promise<void>((resolve) => {
    restore = resolve;
  });
  lifetime.abort(left);
}

function resume(): void {
  watching?.abort();
  watching = undefined;
  if (!restored) return;
  lifetime = new AbortController();
  restored = undefined;
  restore?.();
  restore = undefined;
}

// A browser stops a document's loads once a navigation away begins: WebKit before pagehide,
// reporting each read it cuts off as an access-control failure. So the page's reads stop at
// beforeunload. The navigation may still not happen (a download, a 204, the person stopping
// it or choosing to stay): the next thing they do on the page resumes them. Waiting for that
// rather than a timer keeps a slow navigation's old page from starting reads again.
window.addEventListener(
  "beforeunload",
  () => {
    depart();
    if (watching) return;
    watching = new AbortController();
    for (const type of ["pointerdown", "keydown", "touchstart"]) {
      window.addEventListener(type, resume, { capture: true, signal: watching.signal });
    }
  },
  { capture: true },
);
window.addEventListener(
  "pagehide",
  () => {
    watching?.abort();
    watching = undefined;
    depart();
  },
  { capture: true },
);
window.addEventListener(
  "pageshow",
  (event) => {
    if (event.persisted) resume();
  },
  { capture: true },
);

/** Aborted when either is. `AbortSignal.any` arrived in Safari 17.4 and Firefox 124, after
 * some of the ES2023 browsers the app is built for. Without it, the page's listener is
 * released once the response arrives, so finished reads don't pile up on a long-lived page;
 * the caller's stays, with the caller's signal. */
function either(caller: AbortSignal, page: AbortSignal): [AbortSignal, () => void] {
  if ("any" in AbortSignal) return [AbortSignal.any([caller, page]), () => undefined];
  const both = new AbortController();
  const listening = new AbortController();
  const follow = (source: AbortSignal) => () => {
    both.abort(source.reason);
    listening.abort();
  };
  const aborted = [caller, page].find((source) => source.aborted);
  if (aborted) {
    both.abort(aborted.reason);
  } else {
    caller.addEventListener("abort", follow(caller), { once: true });
    page.addEventListener("abort", follow(page), { once: true, signal: listening.signal });
  }
  return [
    both.signal,
    () => {
      listening.abort();
    },
  ];
}

/** Every page read: GET and HEAD through the app's client, and a query that sends JSON and
 * selects this. The document's lifetime is part of each read's signal, whatever its caller
 * passes, so leaving the page stops a read on the wire. One that was still preparing when the
 * document left (the SDK signs before it reaches transport) waits here, including between
 * pages of a query. A write, or an operation such as an upload, keeps its own lifetime. */
export const fetchPageRead: typeof fetch = async (input, init) => {
  const caller =
    init?.signal !== undefined ? init.signal : input instanceof Request ? input.signal : null;
  while (restored) {
    caller?.throwIfAborted();
    await restored;
  }
  caller?.throwIfAborted();
  if (!caller) return globalThis.fetch(input, { ...init, signal: lifetime.signal });
  const [signal, release] = either(caller, lifetime.signal);
  try {
    return await globalThis.fetch(input, { ...init, signal });
  } finally {
    release();
  }
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
