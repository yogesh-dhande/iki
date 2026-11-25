/** Redraws a canvas when its box changes size or the reader's colour scheme changes, as
 * a canvas doesn't follow either by itself. Returns what stops watching. */
export function watchRedraw(element: Element, redraw: () => void): () => void {
  const stops: (() => void)[] = [];
  if (typeof ResizeObserver === "function") {
    const observer = new ResizeObserver(redraw);
    observer.observe(element);
    stops.push(() => {
      observer.disconnect();
    });
  }
  if (typeof window.matchMedia === "function") {
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    scheme.addEventListener("change", redraw);
    stops.push(() => {
      scheme.removeEventListener("change", redraw);
    });
  }
  return () => {
    for (const stop of stops) stop();
  };
}
