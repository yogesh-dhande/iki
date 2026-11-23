/**
 * A state field's flow (./flow.ts) as a diagram has it: its states as nodes, the moves
 * between them as edges, where each goes on a page, and the same in words.
 *
 * Plain data in, plain data out, and nothing of React or the app's API: the diagram's
 * custom element in @ikisci/ui (ADR 0191) is to draw from this as it is.
 */

import type { FlowField } from "./flow";

/** A signature setting a state needs (ADR 0126), as people read it. */
export interface FlowSignature {
  /** "QA release", or its meaning's label when the plugin gives it no name. */
  label: string;
  /** "Approval". */
  meaning: string;
  /** What its signer attests to. */
  statement: string;
  /** Who may give it: lab roles' labels. */
  roles: string[];
}

export interface FlowCheck {
  /** What whoever's refused is told, with its `{{ }}` values as "…". */
  message: string;
  description: string | null;
}

export interface FlowNode {
  name: string;
  label: string;
  description: string | null;
  /** New records start in it. */
  initial: boolean;
  /** It locks the record against edits. */
  locks: boolean;
  /** None of a record's amount can be used while it's in it (ADR 0139). */
  holds: boolean;
  /** Who sets it: lab roles' labels, the signers' where it's signed for. Empty: no one. */
  setters: string[];
  signatures: FlowSignature[];
  /** Whether setting it asks why. */
  reason: "required" | "optional" | null;
  /** A record may come to it from any other state. */
  fromAny: boolean;
  /** Where it may come from, when not from any: states by name, in the field's order. */
  from: string[];
  /** Where an edit returns a record in it (ADR 0179). */
  onEdit: string | null;
  checks: FlowCheck[];
}

export interface FlowEdge {
  id: string;
  from: string;
  to: string;
  /** A move someone makes, or where an edit returns it. */
  kind: "move" | "correction";
  conditional: boolean;
}

export interface FlowModel {
  name: string;
  label: string;
  nodes: FlowNode[];
  /** The moves drawn as arrows: those into a state that's reached from any other aren't,
   * as that state says so. */
  edges: FlowEdge[];
}

/** What people call a lab role: its label, or its name made readable. */
export function roleLabel(roles: Readonly<Record<string, string>>, role: string): string {
  const label = roles[role];
  if (label) return label;
  const name = role.slice(role.lastIndexOf(".") + 1).replaceAll("_", " ");
  return name.length <= 3 ? name.toUpperCase() : name.charAt(0).toUpperCase() + name.slice(1);
}

function unique(items: string[]): string[] {
  return [...new Set(items)];
}

/** A guard's message for reading away from a record: its values, which only a record
 * has, as "…". */
export function plainMessage(message: string): string {
  return message.replace(/\{\{[^}]*\}\}/g, "…");
}

export function flowModel(field: FlowField, roles: Readonly<Record<string, string>>): FlowModel {
  const names = field.states.map((s) => s.name);
  const nodes = field.states.map((state): FlowNode => {
    const into = field.transitions.filter((t) => t.to === state.name);
    const others = names.filter((n) => n !== state.name);
    const fromAny = others.length >= 2 && others.every((n) => into.some((t) => t.from === n));
    const signatures = state.signatures.map((s) => ({
      label: s.label || s.meaning_label,
      meaning: s.meaning_label,
      statement: s.statement,
      roles: unique(s.roles.map((r) => roleLabel(roles, r))),
    }));
    const setters = signatures.length
      ? unique(signatures.flatMap((s) => s.roles))
      : unique(state.set_by.map((r) => roleLabel(roles, r)));
    return {
      name: state.name,
      label: state.label,
      description: state.description ?? null,
      initial: state.name === field.initial,
      locks: state.locks,
      holds: state.holds_amount,
      setters,
      signatures,
      reason: state.reason,
      fromAny,
      from: names.filter((n) => into.some((t) => t.from === n)),
      onEdit: state.on_edit ?? null,
      checks: (state.guards ?? []).map((g) => ({
        message: plainMessage(g.message),
        description: g.description ?? null,
      })),
    };
  });
  const any = new Set(nodes.filter((n) => n.fromAny).map((n) => n.name));
  const edges: FlowEdge[] = [
    ...field.transitions
      .filter((t) => !any.has(t.to) && names.includes(t.from))
      .map((t) => ({
        id: `${t.from}>${t.to}`,
        from: t.from,
        to: t.to,
        kind: "move" as const,
        conditional: t.conditional,
      })),
    ...nodes
      .filter((n) => n.onEdit && names.includes(n.onEdit))
      .map((n) => ({
        id: `${n.name}~${n.onEdit ?? ""}`,
        from: n.name,
        to: n.onEdit ?? "",
        kind: "correction" as const,
        conditional: false,
      })),
  ];
  return { name: field.name, label: field.label, nodes, edges };
}

function joined(words: readonly string[], last: string): string {
  if (words.length <= 1) return words.join("");
  return `${words.slice(0, -1).join(", ")} ${last} ${words[words.length - 1] ?? ""}`;
}

/** "A", "A or B", "A, B or C". */
export function either(words: readonly string[]): string {
  return joined(words, "or");
}

/** "A", "A and B", "A, B and C". */
export function and(words: readonly string[]): string {
  return joined(words, "and");
}

/** A signature's name as a node shows it: "Authorship", "QA release (Approval)". */
export function signatureName(signature: FlowSignature): string {
  return signature.label === signature.meaning
    ? signature.label
    : `${signature.label} (${signature.meaning})`;
}

/** Who sets a state, in a few words: "Analyst", "Reviewer or QA", "No one sets it". */
export function whoSets(node: FlowNode): string {
  return node.setters.length ? either(node.setters) : "No one sets it";
}

/** How a record comes to a state, in a sentence: who moves it there, from where, how. */
export function wayIn(node: FlowNode, model: FlowModel): string {
  const label = (name: string) => model.nodes.find((n) => n.name === name)?.label ?? name;
  if (!node.setters.length) {
    return node.initial ? "No one moves a record back to it." : "No one moves a record to it.";
  }
  const where = node.fromAny
    ? "from any other state"
    : node.from.length
      ? `from ${either(node.from.map(label))}`
      : "from no other state";
  const why =
    node.reason === "required"
      ? ", saying why"
      : node.reason === "optional"
        ? ", saying why if they like"
        : "";
  const [only] = node.signatures;
  if (only && node.signatures.length === 1) {
    const sets = only.roles.length === 1 ? "sets" : "set";
    return `${either(only.roles)} ${sets} it ${where} by signing it: ${signatureName(only)}${why}.`;
  }
  if (node.signatures.length) {
    const signs = node.signatures.map((s) => `${signatureName(s)}, by ${either(s.roles)}`);
    return `Set ${where} once it’s signed: ${and(signs)}${why}.`;
  }
  return `${either(node.setters)} ${node.setters.length === 1 ? "sets" : "set"} it ${where}${why}.`;
}

/** What else a state means, in a sentence, or "". */
export function whatItMeans(node: FlowNode, model: FlowModel): string {
  const label = (name: string) => model.nodes.find((n) => n.name === name)?.label ?? name;
  const parts = [
    node.initial ? "New records start here." : "",
    node.locks ? "It locks the record: its values can’t change." : "",
    node.holds ? "None of its amount can be used." : "",
    node.onEdit ? `Correcting a value returns it to ${label(node.onEdit)}, with a reason.` : "",
  ];
  return parts.filter(Boolean).join(" ");
}

/** The moves out of a state: where a record in it can go next. */
export function nextFrom(model: FlowModel, state: string): FlowNode[] {
  return model.nodes.filter(
    (n) => n.name !== state && n.setters.length && (n.fromAny || n.from.includes(state)),
  );
}

// Where it all goes. Positions are in px; the diagram draws nodes as HTML boxes and edges
// as SVG paths over them.

export type Orientation = "row" | "column";

export interface PlacedNode {
  name: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface PlacedEdge extends FlowEdge {
  /** The SVG path of its line. */
  path: string;
  /** Its arrowhead, a closed SVG path. */
  head: string;
}

export interface PlacedStub {
  /** The state reached from any other. */
  to: string;
  path: string;
  head: string;
  /** Where it starts: the "any state" dot. */
  cx: number;
  cy: number;
}

export interface FlowLayout {
  orientation: Orientation;
  width: number;
  height: number;
  nodes: PlacedNode[];
  edges: PlacedEdge[];
  stubs: PlacedStub[];
}

/** A move drawn as an arc beside the nodes: on which side, in which lane, and the span of
 * nodes it passes. */
interface Arc {
  edge: FlowEdge;
  side: "a" | "b";
  lane: number;
  lo: number;
  hi: number;
}

/** Each lane of arcs beside the nodes. */
const LANE = 14;
const PAD = 6;
const ROW = { minWidth: 148, maxWidth: 240, gap: 36 };
const COLUMN = { maxWidth: 380, gap: 26 };
/** A node's padding and border, top and bottom, and the height of a line of its text. */
const NODE = { chrome: 16, head: 18, line: 16 };
/** About how wide a character of a node's small text is. */
const CHAR = 6.6;

/** How many lines a node's text takes in a width: its label, who sets it, and what they
 * sign, the last two side by side where they fit (as a column's wide nodes have them). */
function lines(node: FlowNode, width: number, sideBySide: boolean): number {
  const room = width - 20;
  const who = (whoSets(node) + (node.fromAny ? " · from any state" : "")).length * CHAR;
  const sign = node.signatures.length
    ? (node.signatures.map(signatureName).join(" + ").length + 3) * CHAR
    : 0;
  if (sideBySide && who + sign + 8 <= room) return 2;
  const whoLines = Math.min(2, Math.ceil(who / room));
  return 1 + whoLines + (sign ? 1 : 0);
}

function nodeHeight(count: number): number {
  return NODE.chrome + NODE.head + (count - 1) * NODE.line;
}

/** Whether a field's states fit side by side in a width. */
export function fitsInRow(count: number, width: number): boolean {
  return count * ROW.minWidth + (count - 1) * ROW.gap + 2 * PAD <= width;
}

/**
 * Lays a flow out: its states in order, left to right where they fit side by side and top
 * to bottom where they don't. A move to the next state is a straight arrow between the
 * two. Other moves are arcs: those forward above the row (or right of the column), those
 * back below it (or left of it), each in a lane of its own where it would cross another.
 */
export function layout(model: FlowModel, width: number, wanted?: Orientation): FlowLayout {
  const count = model.nodes.length;
  const orientation: Orientation = wanted ?? (fitsInRow(count, width) ? "row" : "column");
  const index = new Map(model.nodes.map((n, i) => [n.name, i]));
  const at = (name: string) => index.get(name) ?? 0;

  // Arcs: every edge but a move to the very next state.
  const arcs: Arc[] = [];
  const straight: FlowEdge[] = [];
  for (const edge of model.edges) {
    const [i, j] = [at(edge.from), at(edge.to)];
    if (edge.kind === "move" && j === i + 1) {
      straight.push(edge);
      continue;
    }
    arcs.push({ edge, side: j > i ? "a" : "b", lane: 0, lo: Math.min(i, j), hi: Math.max(i, j) });
  }
  // Shortest first, each in the first lane where it overlaps none already there.
  for (const side of ["a", "b"] as const) {
    const lanes: Arc[][] = [];
    for (const arc of arcs
      .filter((a) => a.side === side)
      .sort((p, q) => p.hi - p.lo - (q.hi - q.lo) || p.lo - q.lo)) {
      let lane = lanes.findIndex((l) => l.every((o) => arc.hi <= o.lo || o.hi <= arc.lo));
      if (lane < 0) lane = lanes.push([]) - 1;
      lanes[lane]?.push(arc);
      arc.lane = lane;
    }
  }
  const stubbed = model.nodes.filter((n) => n.fromAny);
  const lanesA = Math.max(0, ...arcs.filter((a) => a.side === "a").map((a) => a.lane + 1));
  const lanesB = Math.max(0, ...arcs.filter((a) => a.side === "b").map((a) => a.lane + 1));
  // The "any state" stubs start beyond the arcs.
  const stubLanes = stubbed.length ? lanesA + 1 : 0;
  const sideA = PAD + Math.max(lanesA, stubLanes) * LANE;
  const sideB = PAD + lanesB * LANE;

  // Along the main axis (x in a row, y in a column) and across it.
  // In a row, every node is as wide and as tall; in a column, as wide, each as tall as its
  // text needs.
  let across: number;
  let gap: number;
  let sizes: number[];
  if (orientation === "row") {
    gap = ROW.gap;
    const wide = Math.min(
      ROW.maxWidth,
      Math.max(ROW.minWidth, (width - 2 * PAD - (count - 1) * gap) / count),
    );
    sizes = model.nodes.map(() => wide);
    across = nodeHeight(Math.max(2, ...model.nodes.map((n) => lines(n, wide, false))));
  } else {
    gap = COLUMN.gap;
    across = Math.max(160, Math.min(COLUMN.maxWidth, width - sideA - sideB));
    sizes = model.nodes.map((n) => nodeHeight(lines(n, across, true)));
  }
  const size = (i: number) => sizes[i] ?? 0;
  // Across: in a row, side A (forward arcs) is above; in a column, it's to the right.
  const nodeAcross = orientation === "row" ? sideA : sideB;
  const edgeA = orientation === "row" ? nodeAcross : nodeAcross + across;
  const edgeB = orientation === "row" ? nodeAcross + across : nodeAcross;
  const outward = (side: "a" | "b") =>
    orientation === "row" ? (side === "a" ? -1 : 1) : side === "a" ? 1 : -1;
  const offsets = sizes.map((_, i) => PAD + sizes.slice(0, i).reduce((a, b) => a + b, 0) + i * gap);
  const start = (i: number) => offsets[i] ?? PAD;
  const point = (m: number, c: number): [number, number] =>
    orientation === "row" ? [m, c] : [c, m];

  // Where each arc meets its nodes: along the node's side, ordered by where the arc goes,
  // so arcs leaving to the left meet it left of those leaving to the right.
  const ports = new Map<string, number>();
  for (let i = 0; i < count; i += 1) {
    for (const side of ["a", "b"] as const) {
      const ends = arcs
        .filter((a) => a.side === side && (at(a.edge.from) === i || at(a.edge.to) === i))
        .map((a) => {
          const other = at(a.edge.from) === i ? at(a.edge.to) : at(a.edge.from);
          return { key: `${a.edge.id}@${String(i)}`, other, lane: a.lane };
        });
      if (side === "a" && stubbed.some((n) => at(n.name) === i)) {
        ends.push({ key: `stub@${String(i)}`, other: i, lane: Infinity });
      }
      // Left of the node: outer lanes outermost; right of it: inner lanes first.
      ends.sort((p, q) =>
        p.other !== q.other ? p.other - q.other : p.other < i ? q.lane - p.lane : p.lane - q.lane,
      );
      ends.forEach((e, k) => {
        ports.set(e.key, start(i) + (size(i) * (k + 1)) / (ends.length + 1));
      });
    }
  }

  const head = (tip: [number, number], direction: [number, number]): string => {
    const [dx, dy] = direction;
    const [x, y] = tip;
    const size = 6;
    const back: [number, number] = [x - dx * size, y - dy * size];
    const side: [number, number] = [-dy * (size * 0.6), dx * (size * 0.6)];
    return `M${fmt(x)} ${fmt(y)}L${fmt(back[0] + side[0])} ${fmt(back[1] + side[1])}L${fmt(back[0] - side[0])} ${fmt(back[1] - side[1])}Z`;
  };
  const unit = (from: [number, number], to: [number, number]): [number, number] => {
    const dx = to[0] - from[0];
    const dy = to[1] - from[1];
    const length = Math.hypot(dx, dy) || 1;
    return [dx / length, dy / length];
  };

  const edges: PlacedEdge[] = [];
  const middle = nodeAcross + across / 2;
  for (const edge of straight) {
    const i = at(edge.from);
    const a = point(start(i) + size(i), middle);
    const b = point(start(i + 1), middle);
    edges.push({
      ...edge,
      path: `M${fmt(a[0])} ${fmt(a[1])}L${fmt(b[0])} ${fmt(b[1])}`,
      head: head(b, unit(a, b)),
    });
  }
  for (const arc of arcs) {
    const [i, j] = [at(arc.edge.from), at(arc.edge.to)];
    const edgeAt = arc.side === "a" ? edgeA : edgeB;
    const out = outward(arc.side);
    const lane = edgeAt + out * (arc.lane + 1) * LANE;
    const from = ports.get(`${arc.edge.id}@${String(i)}`) ?? start(i) + size(i) / 2;
    const to = ports.get(`${arc.edge.id}@${String(j)}`) ?? start(j) + size(j) / 2;
    const p0 = point(from, edgeAt);
    const p1 = point(from, lane);
    const p2 = point(to, lane);
    const p3 = point(to, edgeAt);
    edges.push({ ...arc.edge, path: rounded([p0, p1, p2, p3]), head: head(p3, unit(p2, p3)) });
  }
  const stubs: PlacedStub[] = stubbed.map((n) => {
    const i = at(n.name);
    const m = ports.get(`stub@${String(i)}`) ?? start(i) + size(i) / 2;
    const out = outward("a");
    const from = point(m, edgeA + out * stubLanes * LANE);
    const to = point(m, edgeA);
    return {
      to: n.name,
      path: `M${fmt(from[0])} ${fmt(from[1])}L${fmt(to[0])} ${fmt(to[1])}`,
      head: head(to, unit(from, to)),
      cx: from[0],
      cy: from[1],
    };
  });

  const length = start(count - 1) + size(count - 1) + PAD;
  const breadth = sideA + across + sideB;
  const nodes = model.nodes.map((n, i) => {
    const [x, y] = point(start(i), nodeAcross);
    return orientation === "row"
      ? { name: n.name, x, y, width: size(i), height: across }
      : { name: n.name, x, y, width: across, height: size(i) };
  });
  return {
    orientation,
    width: orientation === "row" ? length : breadth,
    height: orientation === "row" ? breadth : length,
    nodes,
    edges,
    stubs,
  };
}

function fmt(n: number): string {
  return String(Math.round(n * 10) / 10);
}

/** A path through points, its corners rounded. */
function rounded(points: [number, number][]): string {
  const radius = 6;
  const [first, ...rest] = points;
  if (!first) return "";
  let d = `M${fmt(first[0])} ${fmt(first[1])}`;
  for (let k = 0; k < rest.length; k += 1) {
    const corner = rest[k];
    const next = rest[k + 1];
    const prev = points[k];
    if (!corner || !prev) continue;
    if (!next) {
      d += `L${fmt(corner[0])} ${fmt(corner[1])}`;
      continue;
    }
    const inLength = Math.hypot(corner[0] - prev[0], corner[1] - prev[1]);
    const outLength = Math.hypot(next[0] - corner[0], next[1] - corner[1]);
    const r = Math.min(radius, inLength / 2, outLength / 2);
    const before: [number, number] = [
      corner[0] - ((corner[0] - prev[0]) / (inLength || 1)) * r,
      corner[1] - ((corner[1] - prev[1]) / (inLength || 1)) * r,
    ];
    const after: [number, number] = [
      corner[0] + ((next[0] - corner[0]) / (outLength || 1)) * r,
      corner[1] + ((next[1] - corner[1]) / (outLength || 1)) * r,
    ];
    d += `L${fmt(before[0])} ${fmt(before[1])}Q${fmt(corner[0])} ${fmt(corner[1])} ${fmt(after[0])} ${fmt(after[1])}`;
  }
  return d;
}
