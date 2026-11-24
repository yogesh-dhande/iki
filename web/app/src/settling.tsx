/**
 * A part of a page that's drawn once every card in it has what decides its lines: a
 * project's Overview.
 *
 * Each card reads for itself, and answers come in any order. Drawn as they came, a card
 * that grew (a list in place of "Finding…", a line it hadn't had) moved every card below
 * it, and a press on one of their links ended on whatever had moved under the pointer. So
 * the cards are drawn hidden, each says with `useSettled` whether its reads are in, and the
 * part shows once they all are, every card where it stays. Until then it says it's loading,
 * in the place the cards will take. What may come later, a card puts after its links.
 *
 * As TabStrip's tabs (tab-strip.tsx), though for a part of the page, not a row.
 */

import {
  createContext,
  useContext,
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
}

const Context = createContext<Waits | null>(null);

/** In a card: whether what decides its lines has come (or failed). Outside a `Settling`,
 * as on a person's home, it does nothing. */
export function useSettled(settled: boolean): void {
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
}

/** The part of the page, `label` saying what it's doing till it shows. Key it by what it's
 * about (the project), so another's is waited for in turn. */
export function Settling({
  label,
  className,
  children,
}: {
  label: string;
  className: string;
  children: ReactNode;
}) {
  const waiting = useRef(new Set<string>());
  // Each report is a new check, after the cards' own.
  const [checks, recheck] = useReducer((n: number) => n + 1, 0);
  const [shown, show] = useState(false);
  const waits = useMemo<Waits>(
    () => ({
      report: (id, settled) => {
        if (settled) waiting.current.delete(id);
        else waiting.current.add(id);
        recheck();
      },
      leave: (id) => {
        waiting.current.delete(id);
        recheck();
      },
    }),
    [],
  );
  // Before it's painted: it shows the frame its last card comes in, and stays shown (a
  // refetch doesn't hide it again).
  useLayoutEffect(() => {
    if (!shown && waiting.current.size === 0) show(true);
  }, [checks, shown]);
  return (
    <Context.Provider value={waits}>
      <div className="settling">
        {shown ? null : (
          <div className="settling-wait">
            <Loading label={label} />
          </div>
        )}
        {/* Laid out while hidden, so it's drawn as it'll stay; nothing in it can be pressed
            or reached till then. */}
        <div
          className={className}
          aria-busy={shown ? undefined : true}
          style={shown ? undefined : { visibility: "hidden" }}
        >
          {children}
        </div>
      </div>
    </Context.Provider>
  );
}
