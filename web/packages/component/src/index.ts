/**
 * Iki's component frame protocol (ADR 0084, ADR 0117, ADR 0170, ADR 0286): what a plugin's
 * component is given, what it may ask for, and the messages Iki's page and its bridge in
 * the frame exchange about it. This package is the protocol's one source: a plugin's
 * component imports its helpers, Iki's page imports its validators from
 * `@ikisci/component/host`, and a test holds Iki's bridge to {@link EVENTS} and
 * {@link PROTOCOL}.
 *
 * A component is a custom element in one JavaScript module. Iki runs it in a sandboxed
 * frame with no network and no session, and sets properties on its element: `place`,
 * `entity` and `entities`, as {@link IkiElement} describes. The element asks Iki for
 * things with events, which the helpers here send: {@link select}, {@link more},
 * {@link refine}, {@link open} and {@link run}. Iki checks each one, and does what it asks
 * itself.
 */

/** The version of the protocol this package speaks. Iki's page names it when it connects
 * to the frame, and the bridge in the frame names it when it's ready; each refuses a
 * version it doesn't speak, so a change to a message is a new version, never a silent
 * one. */
export const PROTOCOL = 1;

/** Where the component is shown: its slot, or an app's panel, and the name it's shown
 * under there. */
export interface Place {
  slot: "entity.tab" | "search.results" | "project.page" | "person.home" | "app.panel";
  label: string;
  /** On an entity's tab of a component that declares `events.run`: the actions it offers
   * that the person viewing the tab may run on its entity now, qualified. Empty until Iki
   * knows; show an action's button only when it's here. */
  runs?: string[];
}

/** An entity another one's field names, as the person viewing it may read it. */
export interface Ref {
  hrid: string;
  title: string;
  type: string;
  archived: boolean;
  purged: boolean;
}

/** A person a field names: only their display name. */
export interface Person {
  name: string;
}

/** The entity whose page a tab is on, or that an app's panel names, as the person viewing
 * it may read it: the API's entity, of which these are the fields components read. */
export interface Entity {
  id: string;
  hrid: string;
  title: string;
  type: string;
  revision: number;
  values: Record<string, unknown>;
  owner_project: { id: string; name: string; slug: string } | null;
  /** The entities its values name that the person may read, by ID. */
  refs: Record<string, Ref>;
  /** The people its values name, by ID. */
  people?: Record<string, Person>;
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
  /** Display names of people named by the declared fields, keyed by person ID. */
  people?: Record<string, Person>;
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
  | "has"
  | "of"
  | "motif";

/** What a component may ask for, to narrow what its place shows: conditions on the fields
 * it reads (and {@link REFINABLE_COLUMNS}), at most {@link MAX_CONDITIONS} of them,
 * {@link MAX_DEPTH} deep, a field it reads to sort by, and text in HRIDs and titles. It
 * never widens the place. */
export interface Refinement {
  where?: Condition;
  sort?: { field: string; descending?: boolean };
  q?: string;
}

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

// What a component may ask for, and how much: Iki's page refuses more, and so does its
// server (`iki_spec.conditions`, `iki_server.components.places`).

/** The most conditions a refinement may have, and how deep they may nest. */
export const MAX_CONDITIONS = 20;
export const MAX_DEPTH = 4;
/** The most characters a refinement may have, as JSON. */
export const MAX_REFINEMENT_SIZE = 10_000;
/** The most characters of text a refinement may search for. */
export const MAX_QUERY_LENGTH = 200;
/** What a refinement can name besides the fields the component reads. */
export const REFINABLE_COLUMNS: readonly string[] = ["hrid"];
/** The most entities one selection may hold. */
export const MAX_SELECTED = 1000;
/** The most a frame may grow to, and what it starts at, in CSS pixels. */
export const MAX_FRAME_HEIGHT = 20_000;
export const MIN_FRAME_HEIGHT = 160;
/** What a selection and {@link open} name entities by: their HRIDs, such as `PRT-000001`. */
export const HRID = /^[A-Za-z][A-Za-z0-9]{1,7}-[0-9]{1,20}$/;
/** What {@link open} names a stored revision's record by: its UUID. */
export const RECORD_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** What a component's element sends: these, and nothing else, reach Iki. */
export const EVENTS = {
  select: "iki-select",
  more: "iki-more",
  refine: "iki-refine",
  open: "iki-open",
  run: "iki-run",
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

/** Opens an entity's page by HRID, one the component was given; or, by record or registry
 * entry UUID and revision number, Iki's read-only page of it as it was at a revision that
 * a stored row of the entity pins (`record_id` and `record_revision`, or `entry_id` and
 * `entry_revision`). Iki checks the target before opening it, and never falls back to the
 * record as it is now. */
export function open(element: EventTarget, entity: string, revision?: number): void {
  send(element, EVENTS.open, revision === undefined ? { entity } : { entity, revision });
}

/** Asks Iki to open its run page for one of the actions the component declares under
 * `events.run`, with the tab's entity chosen. Iki does so only for an action in
 * {@link Place.runs}; the person checks the run there and starts it. */
export function run(element: EventTarget, action: string): void {
  send(element, EVENTS.run, { action });
}

/** What Iki's page sends the frame first: the one port everything else goes through, and
 * the protocol it speaks. */
export interface Connect {
  type: "iki-connect";
  protocol: number;
}

/** What Iki's page sends through the port: what the bridge sets on the element, each
 * property by the message of its name. */
export type HostMessage =
  | { type: "place"; place: Place }
  | { type: "entity"; entity: Entity }
  | { type: "entities"; entities: Entities };

/** What the bridge sends Iki's page through the port: that the element is ready, how tall
 * it is, and what the element asked for. */
export type FrameMessage =
  | { type: "ready"; protocol: number }
  | { type: "resize"; height: number }
  | { type: "select"; entities: string[] }
  | { type: "more" }
  | { type: "refine"; refinement: Refinement | null }
  | { type: "open"; entity: string; revision?: number }
  | { type: "run"; action: string };
