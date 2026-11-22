/**
 * The bar across the top of the page's column (ADR 0176): where the page is, its
 * breadcrumb, on the left, and what can be done there, its actions, on the right. The
 * frame draws the bar, empty; a page fills it with `<TopBar>`, or with `PageHeader`, which
 * uses it. Either stays in the page's own React tree, so its state and context are the
 * page's, and only its DOM is the bar's.
 *
 * A page can be in a frame that fills the bar too: a project's tabs give its crumbs and its
 * New menu, then the tab's own page may give its own. The deepest route's wins, slot by
 * slot, so a tab without actions keeps the project's; a breadcrumb that's only the page's
 * title gives way to a frame's real one.
 *
 * On a phone a page's title comes first: the bar keeps its breadcrumb, and the actions of a
 * page with a title row (`PageHeader`, a record's) go under that title, in `PhoneActions`.
 */

import { useMatch } from "@tanstack/react-router";
import {
  createContext,
  useContext,
  useId,
  useLayoutEffect,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

/** How sure a breadcrumb is of itself: a page's title alone gives way to any other. */
type Strength = 0 | 1 | 2;
const NONE: Strength = 0;
const WEAK: Strength = 1;
const STRONG: Strength = 2;

interface Claim {
  depth: number;
  crumbs: Strength;
  actions: boolean;
  /** When it claimed: the later of two alike wins, as the page drawn last is the one shown. */
  order: number;
}

/** The bars' claims, and which claim holds each slot. */
class Claims {
  private claims = new Map<string, Claim>();
  private listeners = new Set<() => void>();
  private next = 0;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  claim(id: string, claim: Omit<Claim, "order">): void {
    const order = this.claims.get(id)?.order ?? (this.next += 1);
    this.claims.set(id, { ...claim, order });
    this.changed();
  }

  release(id: string): void {
    this.claims.delete(id);
    this.changed();
  }

  /** The claim holding the crumbs: the surest, then the deepest, then the latest. */
  crumbs = (): string | undefined =>
    this.best(
      (c) => c.crumbs !== NONE,
      (a, b) => a.crumbs - b.crumbs || a.depth - b.depth || a.order - b.order,
    );

  /** The claim holding the actions: the deepest, then the latest. */
  actions = (): string | undefined =>
    this.best(
      (c) => c.actions,
      (a, b) => a.depth - b.depth || a.order - b.order,
    );

  /** Whether anything fills the bar: an empty one isn't drawn. */
  filled = (): boolean => this.claims.size > 0;

  private best(
    has: (c: Claim) => boolean,
    compare: (a: Claim, b: Claim) => number,
  ): string | undefined {
    let found: [string, Claim] | undefined;
    for (const entry of this.claims) {
      if (has(entry[1]) && (!found || compare(entry[1], found[1]) > 0)) found = entry;
    }
    return found?.[0];
  }

  private changed(): void {
    for (const listener of this.listeners) listener();
  }
}

interface Slots {
  claims: Claims;
  /** Where the crumbs go; null where the frame draws its own (the admin area). */
  crumbs: HTMLElement | null;
  actions: HTMLElement | null;
}

const SlotsContext = createContext<Slots | null>(null);

/**
 * The bar, for a frame to draw above its page: its crumbs on the left, its actions on the
 * right. `children` is the page, which fills it. Hidden while nothing does.
 */
export function TopBarFrame({ children }: { children: ReactNode }) {
  const [claims] = useState(() => new Claims());
  const [crumbs, setCrumbs] = useState<HTMLDivElement | null>(null);
  const [actions, setActions] = useState<HTMLDivElement | null>(null);
  const filled = useSyncExternalStore(claims.subscribe, claims.filled);
  return (
    <SlotsContext.Provider value={{ claims, crumbs, actions }}>
      <header className="page-top" hidden={!filled}>
        <div className="page-top-crumbs" ref={setCrumbs} />
        <div className="page-top-actions" ref={setActions} />
      </header>
      {children}
    </SlotsContext.Provider>
  );
}

/**
 * Where a frame whose bar is its own (the admin area's) puts the actions of the pages in
 * it: those pages' crumbs are the frame's.
 */
export function ActionsSlot({ slot, children }: { slot: HTMLElement | null; children: ReactNode }) {
  const [claims] = useState(() => new Claims());
  return (
    <SlotsContext.Provider value={{ claims, crumbs: null, actions: slot }}>
      {children}
    </SlotsContext.Provider>
  );
}

/** A breadcrumb: links to the pages above this one, then this one, a string, last. */
export function Crumbs({ items }: { items: ReactNode[] }) {
  return (
    <nav className="crumbs" aria-label="Breadcrumb">
      <ol>
        {items.map((item, i) => (
          // The trail is fixed for the page: its items have no identity beyond their place.
          <li key={i}>
            {i === items.length - 1 && (typeof item === "string" || typeof item === "number") ? (
              <span aria-current="page">{item}</span>
            ) : (
              item
            )}
          </li>
        ))}
      </ol>
    </nav>
  );
}

/**
 * Fills the bar above the page: `crumbs`, links to the pages above it then its own name;
 * `actions`, its buttons, primary last. `weak` crumbs (a page's title alone) give way to
 * any a frame around the page gives. Outside a frame, both are drawn where it is.
 */
export function TopBar({
  crumbs,
  actions,
  weak = false,
}: {
  crumbs?: ReactNode[] | undefined;
  actions?: ReactNode;
  weak?: boolean;
}) {
  const slots = useContext(SlotsContext);
  const id = useId();
  // The route it's drawn by: a tab's page is deeper than the frame of tabs around it.
  const depth = useMatch({ strict: false, shouldThrow: false, select: (m) => m.index }) ?? 0;
  const strength = !crumbs?.length ? NONE : weak ? WEAK : STRONG;
  const acts = actions !== undefined && actions !== null && actions !== false;
  const claims = slots?.claims;
  useLayoutEffect(() => {
    if (!claims) return;
    claims.claim(id, { depth, crumbs: strength, actions: acts });
  }, [claims, id, depth, strength, acts]);
  useLayoutEffect(() => {
    if (!claims) return;
    return () => {
      claims.release(id);
    };
  }, [claims, id]);
  const crumbsOwner = useSyncExternalStore(
    claims?.subscribe ?? noSubscribe,
    claims?.crumbs ?? noOwner,
  );
  const actionsOwner = useSyncExternalStore(
    claims?.subscribe ?? noSubscribe,
    claims?.actions ?? noOwner,
  );
  const trail = crumbs?.length ? <Crumbs items={crumbs} /> : null;
  const buttons = acts ? <div className="page-actions">{actions}</div> : null;
  if (!slots) {
    return trail || buttons ? (
      <div className="page-top page-top-inline">
        {trail}
        {buttons}
      </div>
    ) : null;
  }
  return (
    <>
      {trail && slots.crumbs && crumbsOwner === id ? createPortal(trail, slots.crumbs) : null}
      {buttons && slots.actions && actionsOwner === id
        ? createPortal(buttons, slots.actions)
        : null}
    </>
  );
}

const noSubscribe = () => () => undefined;
const noOwner = () => undefined;

/** A phone's screen (iki.css): a page's actions go under its title there. */
export const PHONE = "(max-width: 36rem)";

/** Whether the screen is a phone's. jsdom, which the tests run in, has no `matchMedia`. */
export function usePhone(): boolean {
  return useSyncExternalStore(subscribePhone, phoneNow, () => false);
}

function subscribePhone(changed: () => void): () => void {
  if (typeof window.matchMedia !== "function") return () => undefined;
  const query = window.matchMedia(PHONE);
  query.addEventListener("change", changed);
  return () => {
    query.removeEventListener("change", changed);
  };
}

function phoneNow(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia(PHONE).matches;
}

/** A page's actions under its title, on a phone. */
export function PhoneActions({ children }: { children: ReactNode }) {
  return <div className="page-actions page-actions-below">{children}</div>;
}
