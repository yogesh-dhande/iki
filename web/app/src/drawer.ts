/**
 * A sidebar that is a drawer on a narrow screen (ADR 0176), as the main sidebar is and as
 * iki.js's setUpDrawer makes the server's: opened from the menu button in the bar above
 * the page, it's a modal dialog over the page, with a backdrop. The rest of the page is
 * out of reach while it's open; Escape, the close button, a click on the backdrop, going
 * somewhere or a screen wide enough for the sidebar to stay close it.
 */

import { useCallback, useEffect, useRef, useState, type RefObject } from "react";

/** Below this width a sidebar is a drawer (iki.css). */
export const DRAWER = "(max-width: 56rem)";

export interface Drawer {
  open: boolean;
  show: () => void;
  dismiss: () => void;
  /** The menu button: it has the focus again once the drawer is dismissed. */
  opener: RefObject<HTMLButtonElement | null>;
  /** The close button: it takes the focus when the drawer opens. */
  closer: RefObject<HTMLButtonElement | null>;
  panel: RefObject<HTMLDivElement | null>;
  backdrop: RefObject<HTMLDivElement | null>;
  /** The panel's own attributes: a modal dialog, named, while it's open. */
  panelProps: { "data-open"?: "true"; role?: "dialog"; "aria-modal"?: true; "aria-label"?: string };
}

/** The drawer's state, for a sidebar whose `pathname` going somewhere closes it. */
export function useDrawer(pathname: string, label: string): Drawer {
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const closer = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const backdrop = useRef<HTMLDivElement>(null);
  // Dismissed, the menu button has the focus again, once it's back within reach; when the
  // reader goes somewhere, the page they go to takes it.
  const refocus = useRef(false);
  const dismiss = useCallback(() => {
    refocus.current = true;
    setOpen(false);
  }, []);
  const show = useCallback(() => {
    setOpen(true);
  }, []);
  // Going somewhere closes it.
  const [shownFor, setShownFor] = useState(pathname);
  if (shownFor !== pathname) {
    setShownFor(pathname);
    setOpen(false);
  }
  // So does a screen wide enough for it to stay.
  useEffect(() => {
    // jsdom, which the tests run in, has none.
    if (typeof window.matchMedia !== "function") return;
    const narrow = window.matchMedia(DRAWER);
    const changed = (event: MediaQueryListEvent) => {
      if (!event.matches) setOpen(false);
    };
    narrow.addEventListener("change", changed);
    return () => {
      narrow.removeEventListener("change", changed);
    };
  }, []);
  // Open, it's a dialog: the rest of the page is out of reach, and Escape or a click beside
  // it dismisses it.
  useEffect(() => {
    if (!open) return;
    closer.current?.focus();
    const sidebar = panel.current;
    const outside = [...(sidebar?.parentElement?.children ?? [])].filter(
      (el): el is HTMLElement =>
        el instanceof HTMLElement && el !== sidebar && el !== backdrop.current,
    );
    for (const el of outside) el.inert = true;
    const pressed = (event: KeyboardEvent) => {
      if (event.key === "Escape") dismiss();
    };
    const away = backdrop.current;
    const menu = opener.current;
    document.addEventListener("keydown", pressed);
    away?.addEventListener("click", dismiss);
    return () => {
      for (const el of outside) el.inert = false;
      document.removeEventListener("keydown", pressed);
      away?.removeEventListener("click", dismiss);
      if (refocus.current) menu?.focus();
      refocus.current = false;
    };
  }, [open, dismiss]);
  const panelProps = open
    ? ({ "data-open": "true", role: "dialog", "aria-modal": true, "aria-label": label } as const)
    : {};
  return { open, show, dismiss, opener, closer, panel, backdrop, panelProps };
}
