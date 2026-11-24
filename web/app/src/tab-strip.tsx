/**
 * A row of tabs that holds still once it can be pressed: a project's and a record's.
 *
 * Which tabs there are, and how wide each is, can wait on reads: a count, the installed
 * types, a plugin's components. Drawn before those answer, a tab moves under the finger as
 * they arrive, and a press ends on its neighbour, or on nothing. So the tabs aren't drawn
 * until `settled`; till then a quiet bar for each tab there's sure to be holds their place.
 */

import { useRouterState } from "@tanstack/react-router";
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

interface Props {
  /** The navigation's name: "Project", "Record". */
  label: string;
  /** Its own class, beside `tabs`: `project-tabs`, `record-tabs`. */
  className: string;
  /** Whether every read that decides which tabs there are and their widths has answered,
   * or failed. */
  settled: boolean;
  /** The tabs there's sure to be, for the bars in their place till then. */
  sure: readonly string[];
  /** What a tab's width changes with (its counts, say): when it does, the tab you're on is
   * brought into view again. */
  widths: string;
  children: ReactNode;
}

export function TabStrip({ label, className, settled, sure, widths, children }: Props) {
  const nav = useRef<HTMLElement>(null);
  const path = useRouterState({ select: (state) => state.location.pathname });
  // While a press is on it, it's drawn as it was when the press began: what changes
  // meanwhile (a count, refetched) is drawn once it's let go, so the press ends on the tab
  // it began on.
  // Let go in the task after the press ends, so its click lands on the tabs as they were.
  const [held, hold] = useState<{ tabs: ReactNode } | null>(null);
  useEffect(() => {
    if (!held) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const release = () => {
      timer = setTimeout(() => {
        hold(null);
      });
    };
    window.addEventListener("pointerup", release, true);
    window.addEventListener("pointercancel", release, true);
    return () => {
      clearTimeout(timer);
      window.removeEventListener("pointerup", release, true);
      window.removeEventListener("pointercancel", release, true);
    };
  }, [held]);
  // On a phone the tabs scroll across: the one you're on is in view from when they're
  // drawn, and again when a width changes. Before they're painted, so they don't move once
  // they can be pressed.
  useLayoutEffect(() => {
    const strip = nav.current;
    const current = strip?.querySelector<HTMLElement>('[aria-current="page"]');
    if (settled && strip && current && strip.scrollWidth > strip.clientWidth) {
      current.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }, [path, widths, settled]);
  return (
    <>
      {settled ? null : (
        <div className={`tabs ${className} tabs-settling`} aria-hidden="true">
          {sure.map((name) => (
            <span key={name} className="tab-settling">
              <span style={{ width: `${String(name.length * 0.6)}em` }} />
            </span>
          ))}
        </div>
      )}
      <nav
        ref={nav}
        className={`tabs ${className}`}
        aria-label={label}
        hidden={!settled}
        onPointerDown={() => {
          hold((now) => now ?? { tabs: children });
        }}
      >
        {held ? held.tabs : children}
      </nav>
    </>
  );
}
