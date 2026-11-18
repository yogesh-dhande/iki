/**
 * What a plugin's component is given, and what it may ask for (ADR 0084, ADR 0117).
 *
 * A component is a custom element in one JavaScript module. Iki runs it in a sandboxed
 * frame with no network and no session, and sets properties on its element: `place`,
 * `entity` and `entities`, as {@link IkiElement} describes. The element asks Iki for
 * things with events, which the helpers here send: {@link select}, {@link more},
 * {@link refine} and {@link open}. Iki checks each one, and does what it asks itself.
 */

/** Where the component is shown: its slot, or an app's panel, and the name it's shown
 * under there. */
export interface Place {
  slot: "entity.tab" | "search.results" | "project.page" | "person.home" | "app.panel";
  label: string;
}

/** The entity whose page a tab is on, or that an app's panel names, as the person viewing
 * it may read it: the API's entity, of which these are the fields most components read. */
export interface Entity {
  id: string;
  hrid: string;
  title: string;
  type: string;
  revision: number;
  values: Record<string, unknown>;
  owner_project: { id: string; name: string; slug: string } | null;
}

/** An entity another one's field names. */
export interface Ref {
  hrid: string;
  title: string;
  type: string;
  archived: boolean;
  purged: boolean;
}

/** One of the many entities a component that declares `entities` is given: only the
 * fields it reads, and the entities those fields name, by ID. */
export interface ShownEntity {
  id: string;
  hrid: string;
  title: string;
  type: string;
  revision: number;
  /** The project it's in, if the person can see it. */
  project: { id: string; name: string; slug: string } | null;
  values: Record<string, unknown>;
  refs: Record<string, Ref>;
}

/** A search's condition, as the API takes it. */
export type Condition =
  | { field: string; op: Operator; value?: unknown }
  | { all: Condition[] }
  | { any: Condition[] }
  | { not: Condition };

export type Operator =
  | "is_set"
  | "eq"
  | "ne"
  | "in"
  | "lt"
  | "lte"
  | "gt"
  | "gte"
  | "contains"
  | "starts_with"
  | "matches"
  | "has";

/** What a component may ask for, to narrow what its place shows: conditions on the fields
 * it reads (and `hrid`), at most {@link MAX_CONDITIONS} of them, {@link MAX_DEPTH} deep, a
 * field it reads to sort by, and text in HRIDs and titles. It never widens the place. */
export interface Refinement {
  where?: Condition;
  sort?: { field: string; descending?: boolean };
  q?: string;
}

/** The most conditions a refinement may have, and how deep they may nest. */
export const MAX_CONDITIONS = 20;
export const MAX_DEPTH = 4;

/** What fills a component's place, a page at a time. */
export interface Entities {
  items: ShownEntity[];
  /** There are more it can ask for, with {@link more}. */
  more: boolean;
  /** It holds as many as it declared it may (`entities.limit`), so it's given no more. */
  limited: boolean;
  /** A page, or a refinement's first, is on its way. */
  loading: boolean;
  /** Why it has none, if its search was refused. */
  error: string | null;
  /** The refinement in force: what it last asked for. */
  refinement: Refinement | null;
}

/** The properties Iki sets on a component's element. Each is set again whenever it
 * changes, so a setter that draws is all a component needs. */
export interface IkiElement {
  place?: Place;
  entity?: Entity;
  entities?: Entities;
}

/** What a component's element sends: these, and nothing else, reach Iki. */
export const EVENTS = {
  select: "iki-select",
  more: "iki-more",
  refine: "iki-refine",
  open: "iki-open",
} as const;

function send(element: EventTarget, name: string, detail?: unknown): void {
  element.dispatchEvent(new CustomEvent(name, { detail, bubbles: true }));
}

/** The entities a person picked, by HRID, of the type the component declares in
 * `events.select`. Iki reads them back as the person may read them, and offers to run an
 * action on them. An empty list clears what's picked. */
export function select(element: EventTarget, hrids: string[]): void {
  send(element, EVENTS.select, { entities: hrids });
}

/** The next page of what fills its place, while {@link Entities.more} says there is one. */
export function more(element: EventTarget): void {
  send(element, EVENTS.more);
}

/** Narrows what its place shows, starting again from the first page; `null` clears what
 * it asked for before. Iki refuses one that names a field the component doesn't read. */
export function refine(element: EventTarget, refinement: Refinement | null): void {
  send(element, EVENTS.refine, refinement);
}

/** Opens the page of an entity it was given, by HRID: Iki navigates, not the frame. */
export function open(element: EventTarget, hrid: string): void {
  send(element, EVENTS.open, { entity: hrid });
}
