/**
 * A part of a page whose cards are drawn where they stay: a project's Overview.
 *
 * Each card reads for itself, and answers come in any order. Drawn as they came, a card
 * that grew (a list in place of "Finding…", a line it hadn't had) moved every card below
 * it, and a press on one of their links ended on whatever had moved under the pointer.
 *
 * So the cards are laid out hidden, each saying with `useSettled` whether what decides its
 * lines is in, and the part is drawn once they all are, or once its `budget` is spent,
 * whichever is first: a slow read never keeps the rest from being drawn. A card whose read
 * wasn't in by then is late, and draws what it waits for in a `Reserve`: a slot of a fixed
 * height, where it'll be, that says it's loading and then holds what came, scrolling in it
 * if that's taller. Nothing around it moves. What can only grow (a list at the foot of a
 * column, below everything that can be pressed) needs no slot.
 *
 * A part in a part (what's at the foot of the page, say) is drawn once its own cards are
 * all in, and not before the part around it: with no budget, as nothing comes after it.
 *
 * As TabStrip's tabs (tab-strip.tsx), though for a part of the page, not a row.
 */

import {
  createContext,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { Loading } from "./ui";

interface Waits {
  /** A card says whether its reads are in. */
  report: (id: string, settled: boolean) => void;
  /** A card that's gone waits for nothing. */
  leave: (id: string) => void;
  /** The cards that were in when the part was drawn; null till it is. */
  inTime: ReadonlySet<string> | null;
}

const Context = createContext<Waits | null>(null);

/** In a card: whether what decides its lines has come (or failed). It answers whether the
 * card is late: drawn before its reads were in, so what they decide goes in a `Reserve`.
 * Outside a `Settling`, as on a person's home, it's never late. */
export function useSettled(settled: boolean): boolean {
  const waits = useContext(Context);
  const id = useId();
  // Before the part's own check, which runs after its cards' (the children's first).
  useLayoutEffect(() => {
    waits?.report(id, settled);
  }, [waits, id, settled]);
  useLayoutEffect(
    () => () => {
      waits?.leave(id);
    },
    [waits, id],
  );
  return Boolean(waits?.inTime && !waits.inTime.has(id));
}

/** The part of the page, `label` saying what it's doing till it's drawn (at the foot of the
 * page, give none: it's drawn there when it comes, and a line said meanwhile would go,
 * making the page shorter). Key it by what it's about (the project), so another's is
 * waited for in turn. */
export function Settling({
  label,
  className,
  budget,
  children,
}: {
  label?: string;
  className: string;
  /** How long its cards are waited for, in milliseconds; for ever if it's not given. */
  budget?: number;
  children: ReactNode;
}) {
  const outer = useContext(Context);
  const states = useRef(new Map<string, boolean>());
  // Each report is a new check, after the cards' own.
  const [checks, recheck] = useReducer((n: number) => n + 1, 0);
  const [spent, spend] = useState(false);
  const [inTime, draw] = useState<ReadonlySet<string> | null>(null);
  useEffect(() => {
    if (budget === undefined) return;
    const timer = setTimeout(() => {
      spend(true);
    }, budget);
    return () => {
      clearTimeout(timer);
    };
  }, [budget]);
  const report = useMemo(
    () => ({
      report: (id: string, settled: boolean) => {
        states.current.set(id, settled);
        recheck();
      },
      leave: (id: string) => {
        states.current.delete(id);
        recheck();
      },
    }),
    [],
  );
  const waits = useMemo<Waits>(() => ({ ...report, inTime }), [report, inTime]);
  // Outside another part, there's none to wait for.
  const outerDrawn = outer?.inTime !== null;
  // Before it's painted, so each card is drawn as it'll stay, late ones in their slots. Once
  // drawn, it stays drawn (a refetch doesn't hide it again).
  useLayoutEffect(() => {
    if (inTime || !outerDrawn) return;
    const all = [...states.current.values()].every(Boolean);
    if (!all && !spent) return;
    draw(new Set([...states.current].filter(([, settled]) => settled).map(([id]) => id)));
  }, [checks, spent, inTime, outerDrawn]);
  const drawn = inTime !== null;
  const box = useRef<HTMLDivElement>(null);
  // Once drawn, it never gets shorter while it's as wide: what's at its foot ending shorter
  // than it was (a line said while waiting, gone) would make the page shorter, and the
  // browser would pull it down under a press if it was scrolled to the end.
  useLayoutEffect(() => {
    const at = box.current;
    if (!drawn || !at) return;
    // Measured exactly, not to the whole pixel: a shrink of a fraction of one moves it too.
    const size = () => at.getBoundingClientRect();
    let width = size().width;
    let tallest = 0;
    const keep = () => {
      const now = size();
      if (now.width !== width) {
        width = now.width;
        tallest = 0;
        at.style.minHeight = "";
      }
      if (now.height > tallest) {
        tallest = now.height;
        at.style.minHeight = `${String(tallest)}px`;
      }
    };
    keep();
    if (typeof ResizeObserver !== "function") return;
    const watch = new ResizeObserver(keep);
    watch.observe(at);
    return () => {
      watch.disconnect();
    };
  }, [drawn]);
  return (
    <Context.Provider value={waits}>
      <div className="settling" ref={box}>
        {drawn || label === undefined ? null : (
          <div className="settling-wait">
            <Loading label={label} />
          </div>
        )}
        {/* Laid out while hidden, so it's drawn as it'll stay; nothing in it can be pressed
            or reached till then. */}
        <div
          className={className}
          aria-busy={drawn ? undefined : true}
          style={drawn ? undefined : { visibility: "hidden" }}
        >
          {children}
        </div>
      </div>
    </Context.Provider>
  );
}

/**
 * In a late card, where its read's answer goes: a slot `size` high (a class, `reserve-…`,
 * as tall as the answer usually is) that says it's loading till `ready`, then holds what
 * came, scrolling if it's taller. In a card that was in time, just what it holds.
 */
export function Reserve({
  late,
  ready,
  size,
  label,
  name,
  children,
}: {
  late: boolean;
  ready: boolean;
  size: string;
  /** What it says till it's ready: "Finding what changed…". */
  label: string;
  /** What it holds, for one who scrolls it from the keyboard: "Recent changes". */
  name: string;
  children: ReactNode;
}) {
  const slot = useRef<HTMLDivElement>(null);
  const [scrolls, setScrolls] = useState(false);
  // Taller than its slot, it scrolls, and is reached by the keyboard to be scrolled.
  useLayoutEffect(() => {
    const at = slot.current;
    if (at) setScrolls(at.scrollHeight > at.clientHeight + 1);
  }, [ready, children]);
  if (!late) return <>{children}</>;
  return (
    <div
      ref={slot}
      className={`reserve ${size}`}
      {...(scrolls ? { tabIndex: 0, role: "group", "aria-label": name } : {})}
    >
      {ready ? children : <Loading label={label} />}
    </div>
  );
}
