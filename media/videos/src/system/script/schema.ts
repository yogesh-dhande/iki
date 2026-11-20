// What a video's script may say. Every video is one YAML file, src/<video>/script.yaml,
// checked against this schema (and the rules in rules.ts) before anything renders. The
// JSON Schema in script.schema.json is generated from this, for editors' autocompletion.
import { z } from "zod";
import { durationNames, entranceNames, exitNames, holdNames, toneNames } from "./names.ts";

const id = z
  .string()
  .regex(/^[a-z][a-z0-9-]*$/, "Use lowercase letters, digits and hyphens, starting with a letter.");
const text = z.string().trim().min(1);

/** A field on a record card, as Iki's entity page shows it. */
export const Field = z.strictObject({
  label: text,
  value: text,
  /** `sequence` and `id` are set in monospace; long sequences are cut to an excerpt. */
  kind: z.enum(["text", "sequence", "id"]).default("text"),
  /** A second, quieter line: a referenced entry's sequence, say. Sequences are cut short. */
  detail: text.optional(),
  detailKind: z.enum(["text", "sequence"]).default("text"),
});

/** A record: an entity (with its HRID) or a draft that hasn't been registered. */
export const Record = z.strictObject({
  /** The type's name, as the app shows it: "Antibody". */
  type: text,
  hrid: z
    .string()
    .regex(/^[A-Z]{2,6}-\d{6}$/, "HRIDs look like ABY-000001.")
    .optional(),
  name: text,
  fields: z.array(Field).max(6).default([]),
  /** A line under the fields: the project, the state. */
  footer: text.optional(),
});

/** A message as the app shows it: an alert with a tone. */
export const Notice = z.strictObject({
  tone: z.enum(toneNames),
  title: text.optional(),
  text,
  /** An HRID or name the notice points to, set as a link. */
  link: text.optional(),
});

/** A spreadsheet or notebook page, cut down to a few rows. */
export const Fragment = z.strictObject({
  kind: z.enum(["spreadsheet", "notebook"]),
  file: text,
  columns: z.array(text).max(3).default([]),
  rows: z.array(z.array(z.string())).min(1).max(4),
  /** Cells to mark as disagreeing with the others: [row, column], from 0. */
  highlight: z.array(z.tuple([z.number().int().min(0), z.number().int().min(0)])).default([]),
});

/** The plugin that defines a type, and the fields it defines. */
export const Plugin = z.strictObject({
  /** The plugin's name, as installed: "antibody". */
  name: text,
  /** What it provides: "Antibody type". */
  provides: text,
  fields: z
    .array(z.strictObject({ name: text, type: text }))
    .min(1)
    .max(6),
});

/** An electronic signature (ADR 0126), worded as the record's page words it. */
export const Signature = z.strictObject({
  /** The verb for its meaning: approval is "Approved", review "Reviewed". */
  verb: z.enum(["Authored", "Reviewed", "Approved", "Verified", "Signed off", "Witnessed"]),
  /** The meaning's label, from the plugin's state: "QA release". */
  meaning: text,
  by: text,
  /** As the app gives it: "on 2026-11-18 at 14:03:22 UTC". */
  when: text,
});

/** A revision, worded as the History tab words it (ADR 0035). */
export const Revision = z.strictObject({
  number: z.number().int().min(1),
  what: z.enum([
    "Created",
    "Edited",
    "Archived",
    "Restored",
    "Facts changed",
    "Superseded",
    "Moved",
    "State set",
  ]),
  who: text,
  when: text,
  /** The reason, when one was given: it's optional in Iki. */
  why: text.optional(),
  signature: Signature.optional(),
});

export const GraphNode = z.strictObject({
  id,
  /** What kind of thing it is: "Antibody", "Lot", "Run". */
  kind: text,
  title: text,
  hrid: z.string().optional(),
  /** The node the graph is about: drawn with the accent. */
  focus: z.boolean().default(false),
  /** A signature on the record, shown under the node. */
  signature: Signature.optional(),
});

export const GraphEdge = z.strictObject({
  from: id,
  to: id,
  label: text.optional(),
});

/** Something a focus or split layout shows: one of these. */
export const Content = z.union([
  z.strictObject({ record: Record }),
  z.strictObject({ notice: Notice }),
  z.strictObject({ fragment: Fragment }),
]);

const Panel = z.strictObject({ label: text.optional(), content: Content });

/** Something a beat does to a part of its scene. Parts are named by the layout: see parts.ts. */
export const Step = z.union([
  z.strictObject({ show: text, as: z.enum(entranceNames).default("fade") }),
  z.strictObject({ hide: text, as: z.enum(exitNames).default("fade") }),
  z.strictObject({ emphasize: text }),
  z.strictObject({ dim: text }),
  z.strictObject({
    /** A function in src/<video>/custom.ts, given the scene's parts and `length` seconds. */
    custom: id,
    length: z.enum(durationNames),
    args: z.record(z.string(), z.unknown()).default({}),
  }),
]);

/** One step, or several that start together (each a stagger after the last). */
export const StepGroup = z.union([Step, z.strictObject({ together: z.array(Step).min(2) })]);

export const Beat = z.strictObject({
  /** Stable across edits: the voice-over's audio for this line is filed under it. */
  id,
  /** The line on screen, and in the .vtt file. */
  caption: text.optional(),
  /** What the voice-over says, when it differs from the caption (an HRID read aloud). */
  say: text.optional(),
  /** What happens while the line is on screen, in order. */
  do: z.array(StepGroup).default([]),
  /** Extra time after the line, before the next. */
  hold: z.enum(holdNames).default("none"),
});

/**
 * Several things in the scene before become one thing in this one: copies collapsing into
 * a record, files into a registry entry. Each `from` part flies to the `into` part's place
 * and shrinks away as `into` appears there; the rest of the scene before fades.
 */
const Merge = z.strictObject({
  /** The scene before's parts that merge: every gallery item (`item:<n>`) if left out. */
  from: z.array(text).min(1).optional(),
  /** This scene's part they merge into. */
  into: text,
});

const Transition = z.union([
  z.enum(["fade", "slide", "cut"]),
  z.strictObject({ merge: Merge, length: z.enum(durationNames).default("deliberate") }),
  z.strictObject({ custom: id, length: z.enum(durationNames) }),
]);

/** The layouts, each with its named slots. */
const slots = {
  title: z.strictObject({ eyebrow: text, title: text, subtitle: text.optional() }),
  focus: z.strictObject({ heading: text.optional(), object: Content }),
  gallery: z.strictObject({ heading: text.optional(), items: z.array(Content).min(2).max(6) }),
  split: z.strictObject({
    heading: text.optional(),
    left: Panel,
    right: Panel,
    /** An arrow from the left panel to the right, with a label. */
    link: text.optional(),
    /** A notice under both panels. */
    note: Notice.optional(),
  }),
  "record-inset": z.strictObject({
    record: Record,
    inset: Plugin,
    /** The connector's label, from the inset to the record. */
    link: text.optional(),
  }),
  timeline: z.strictObject({
    heading: text.optional(),
    subject: z.strictObject({ type: text, hrid: z.string(), name: text }),
    revisions: z.array(Revision).min(2).max(5),
  }),
  graph: z.strictObject({
    heading: text.optional(),
    direction: z.enum(["right", "down"]).default("right"),
    nodes: z.array(GraphNode).min(2).max(6),
    edges: z.array(GraphEdge).min(1),
  }),
  closing: z.strictObject({ statement: text, next: text.optional() }),
};

export const layoutNames = Object.keys(slots) as (keyof typeof slots)[];

function scene<L extends keyof typeof slots>(layout: L) {
  return z.strictObject({
    id,
    layout: z.literal(layout),
    slots: slots[layout],
    /**
     * Starts a chapter: its title in the website's and YouTube's chapter list. The first
     * scene starts one; a scene without a chapter belongs to the one before.
     */
    chapter: text.optional(),
    /** How this scene arrives (and the previous one leaves). */
    transition: Transition.default("fade"),
    beats: z.array(Beat).min(1),
  });
}

export const Scene = z.discriminatedUnion("layout", [
  scene("title"),
  scene("focus"),
  scene("gallery"),
  scene("split"),
  scene("record-inset"),
  scene("timeline"),
  scene("graph"),
  scene("closing"),
]);

/** How the video is published, on the website and YouTube (out/<video>.publish.json). */
const Publish = z.strictObject({
  /** A paragraph or two of plain text: what the viewer learns. The chapters follow it. */
  description: z.string().min(1),
  /** Search terms, for YouTube: 500 characters at most, all together. */
  tags: z.array(text).min(1),
  /** The beat whose end is the thumbnail: the first beat's, the title card, if left out. */
  thumbnail: id.optional(),
});

/**
 * Words the script quotes from the product, which must still be there: a check fails when
 * a quote no longer appears in the product's source, so a renamed label or a reworded
 * message flags the videos that show it.
 */
const Quote = z.strictObject({
  /** Exactly as the product has it, typographic apostrophes and all. */
  text: z.string().min(1),
  /** Where the product says it, from the repository's root. */
  in: z.string().default("web/app/src"),
});

export const Video = z.strictObject({
  id,
  title: text,
  /** The series' label, shown on the title card. */
  series: text,
  /** About how long the video should run, in seconds: the check warns when it's off by 15%. */
  target: z.number().positive(),
  /**
   * A specimen shows every layout and component for reference: it's checked and its stills
   * are rendered, but it's never published, so it has no committed render or captions.
   */
  specimen: z.boolean().default(false),
  /** Every published video has this. */
  publish: Publish.optional(),
  quotes: z.array(Quote).default([]),
  scenes: z.array(Scene).min(1),
});

export type Video = z.infer<typeof Video>;
export type Scene = z.infer<typeof Scene>;
export type Beat = z.infer<typeof Beat>;
export type Step = z.infer<typeof Step>;
export type StepGroup = z.infer<typeof StepGroup>;
export type Content = z.infer<typeof Content>;
export type RecordData = z.infer<typeof Record>;
export type FieldData = z.infer<typeof Field>;
export type NoticeData = z.infer<typeof Notice>;
export type FragmentData = z.infer<typeof Fragment>;
export type PluginData = z.infer<typeof Plugin>;
export type RevisionData = z.infer<typeof Revision>;
export type SignatureData = z.infer<typeof Signature>;
export type GraphNodeData = z.infer<typeof GraphNode>;
export type GraphEdgeData = z.infer<typeof GraphEdge>;
export type LayoutName = Scene["layout"];
export type SceneOf<L extends LayoutName> = Extract<Scene, { layout: L }>;
