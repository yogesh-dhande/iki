// The page side of a viewer's frame: the frame is the site's own page, so the page can
// measure it and size it to what it drew, with no scrollbar of its own.
export function fit(frame: HTMLIFrameElement): void {
  const inner = frame.contentDocument?.documentElement;
  if (!inner) {
    return;
  }
  const resize = () => {
    frame.style.height = `${String(Math.ceil(inner.scrollHeight))}px`;
  };
  new ResizeObserver(resize).observe(inner);
  resize();
}

export function fitFrames(document: Document): void {
  for (const frame of document.querySelectorAll<HTMLIFrameElement>("iframe[data-viewer]")) {
    if (
      frame.contentDocument?.readyState === "complete" &&
      frame.contentDocument.URL !== "about:blank"
    ) {
      fit(frame);
    }
    frame.addEventListener("load", () => {
      fit(frame);
    });
  }
}

/** The frame side: it wears the theme of the page that holds it, and follows its changes. */
export function followTheme(frame: Window): void {
  const parent = frame.parent === frame ? null : frame.parent;
  let outer: HTMLElement | undefined;
  try {
    outer = parent?.document.documentElement;
  } catch {
    return; // Framed by another site: the frame follows the system's theme instead.
  }
  if (!outer) {
    return;
  }
  const root = frame.document.documentElement;
  const copy = () => {
    const theme = outer.dataset["theme"];
    if (theme) {
      root.dataset["theme"] = theme;
    } else {
      delete root.dataset["theme"];
    }
  };
  new MutationObserver(copy).observe(outer, { attributes: true, attributeFilter: ["data-theme"] });
  copy();
}
