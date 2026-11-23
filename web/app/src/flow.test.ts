/**
 * Working a type's flow out from its definition (ADR 0199), against the real plugins'
 * state fields: example_lims' analysis results, samples and requests, lots, and plate
 * assays' research and regulated QC results (flow-fixtures.json, which a server test
 * keeps the plugins' own).
 */

import { describe, expect, it } from "vitest";

import { fieldFlow, parse, typeFlow, type FlowField, type FlowSource } from "./flow";
import fixtures from "./flow-fixtures.json";
import {
  flowModel,
  layout,
  nextFrom,
  plainMessage,
  roleLabel,
  wayIn,
  whatItMeans,
  type FlowModel,
} from "./flow-model";

const ROLES: Record<string, string> = fixtures.roles;

function source(type: string, field: string): FlowSource {
  const found = fixtures.types.find((t) => t.type === type)?.fields.find((f) => f.name === field);
  if (!found) throw new Error(`No ${type} ${field} in the fixtures`);
  return found as FlowSource;
}

function flowOf(type: string, field: string): FlowField {
  return fieldFlow(source(type, field));
}

/** Its moves as "from > to", a "?" after those that depend on more than the states. */
function moves(flow: FlowField): string[] {
  return flow.transitions.map((t) => `${t.from} > ${t.to}${t.conditional ? " ?" : ""}`);
}

function modelOf(type: string, field: string): FlowModel {
  return flowModel(flowOf(type, field), ROLES);
}

const RESULT = "iki/example_lims.AnalysisResult";
const SAMPLE = "iki/example_lims.Sample";
const REQUEST = "iki/example_lims.AnalysisRequest";
const LOT = "iki/lots.Lot";
const RESEARCH = "iki/plate_assays.ResearchAssayResult";
const QC = "iki/plate_assays.QCAssayResult";

describe("a flow from a type's guards", () => {
  it("draws an analysis result's regulated review: submitted, reviewed, released", () => {
    expect(moves(flowOf(RESULT, "review"))).toEqual([
      // Returned for correction only while it waits for review or release.
      "in_review > draft",
      "reviewed > draft",
      // Submitted from Draft, once its measurement is recorded.
      "draft > in_review ?",
      // Reviewed independently of who made and submitted it.
      "in_review > reviewed ?",
      // Released by QA, once reviewed.
      "reviewed > released ?",
    ]);
  });

  it("reads a research result's one sign-off, and that no one sets where it starts", () => {
    const flow = flowOf(RESEARCH, "review");
    expect(flow.initial).toBe("recorded");
    // The guard also asks for a recorded fit: a record's, so it's conditional.
    expect(moves(flow)).toEqual(["recorded > signed ?"]);
  });

  it("reads a regulated QC result's submission, independent review and QA release", () => {
    expect(moves(flowOf(QC, "review"))).toEqual([
      "submitted > recorded",
      "reviewed > recorded",
      "recorded > submitted ?",
      "submitted > reviewed ?",
      "reviewed > released ?",
    ]);
  });

  it("keeps every origin a guard can't rule out, and none it can", () => {
    // A lot goes on hold from anything but Rejected or Expired (!=), and expires from any
    // state on a date only the record knows.
    const lot = moves(flowOf(LOT, "disposition"));
    expect(lot.filter((m) => m.endsWith("> on_hold"))).toEqual([
      "quarantined > on_hold",
      "released > on_hold",
    ]);
    expect(lot.filter((m) => m.includes("> expired"))).toEqual([
      "quarantined > expired ?",
      "released > expired ?",
      "on_hold > expired ?",
      "rejected > expired ?",
    ]);
    // Released from Quarantined or On hold; its other guards are the record's and the
    // person's, one of them only when the lot was made.
    expect(lot.filter((m) => m.endsWith("> released ?"))).toEqual([
      "quarantined > released ?",
      "on_hold > released ?",
    ]);
  });

  it("reads each of a type's state fields", () => {
    const request = fixtures.types.find((t) => t.type === REQUEST);
    if (!request) throw new Error("No analysis request");
    const flow = typeFlow({
      name: request.type,
      label: request.label,
      fields: request.fields as FlowSource[],
    });
    expect(flow.fields.map((f) => [f.name, f.label, f.initial])).toEqual([
      ["standing", "Standing", "open"],
      ["status", "Status", "submitted"],
    ]);
    const [standing, status] = flow.fields;
    if (!standing) throw new Error("No standing");
    expect(moves(standing)).toEqual(["withdrawn > open", "open > withdrawn"]);
    // Without guards, each state the core sets can follow any other.
    expect(status?.transitions).toHaveLength(5 * 4);
    expect(status?.transitions.every((t) => !t.conditional)).toBe(true);
  });

  it("leaves out a type's fields that aren't state fields", () => {
    const flow = typeFlow({
      name: "acme/x.Thing",
      label: "Thing",
      fields: [
        { ...source(SAMPLE, "status"), type: "state" },
        { name: "notes", label: "Notes", initial: null, states: [], type: "text" },
      ],
    });
    expect(flow.fields.map((f) => f.name)).toEqual(["status"]);
  });
});

describe("reading guards", () => {
  const state = (check: string, when?: string) =>
    moves(
      fieldFlow({
        name: "review",
        label: "Review",
        initial: "a",
        states: [
          {
            name: "a",
            label: "A",
            set_by: ["x.r"],
            locks: false,
            holds_amount: false,
            reason: null,
            signatures: [],
          },
          {
            name: "b",
            label: "B",
            set_by: ["x.r"],
            locks: false,
            holds_amount: false,
            reason: null,
            signatures: [],
          },
          {
            name: "c",
            label: "C",
            set_by: ["x.r"],
            locks: false,
            holds_amount: false,
            reason: null,
            signatures: [],
            guards: [{ name: "g", check, message: "No.", ...(when ? { when } : {}) }],
          },
        ],
      }),
    ).filter((m) => m.includes("> c"));

  it("reads equality, inequality, lists, negation and the record's own state", () => {
    expect(state("state.from in ['a', 'b'] && !(state.from == 'b')")).toEqual(["a > c"]);
    expect(state("record.review != 'a'")).toEqual(["b > c"]);
    expect(state("state.field == 'review' && 'c' == state.to")).toEqual(["a > c", "b > c"]);
    expect(state('(state.from == "a" || state.from == "b") && record.fit != null')).toEqual([
      "a > c ?",
      "b > c ?",
    ]);
  });

  it("keeps a move it can't settle, as conditional", () => {
    expect(state("history.all(h, h.by != actor.person)")).toEqual(["a > c ?", "b > c ?"]);
    expect(state("size(record.files) > 0 ? true : false")).toEqual(["a > c ?", "b > c ?"]);
    // Not CEL at all: unknown, never a reason to leave it out.
    expect(state("state.from ==")).toEqual(["a > c ?", "b > c ?"]);
  });

  it("applies a guard only where its `when` holds", () => {
    expect(state("false", "state.from != 'a'")).toEqual(["a > c"]);
    expect(state("state.from == 'b'", "actor.waiver")).toEqual(["a > c ?", "b > c ?"]);
  });

  it("parses what it doesn't evaluate", () => {
    expect(parse("edits.review.draft.all(e, e.by != actor.person)")).toEqual({ kind: "other" });
    expect(parse("history.review[0].by != actor.person")?.kind).toBe("ne");
    expect(parse("{'a': 1}.size() == 1")?.kind).toBe("eq");
    expect(parse("r'raw\\d' == state.from")?.kind).toBe("eq");
    expect(parse("a b")).toBeNull();
  });
});

describe("a flow as a diagram has it", () => {
  it("says who sets each state, what they sign, and where an edit returns it", () => {
    const model = modelOf(RESULT, "review");
    const released = model.nodes.find((n) => n.name === "released");
    expect(released).toMatchObject({
      setters: ["Sample and result QA"],
      locks: true,
      from: ["reviewed"],
      signatures: [
        {
          label: "QA release",
          meaning: "Approval",
          statement: "I release these results to the project that requested them.",
          roles: ["Sample and result QA"],
        },
      ],
    });
    expect(model.edges.filter((e) => e.kind === "correction").map((e) => e.id)).toEqual([
      "in_review~draft",
      "reviewed~draft",
    ]);
    expect(model.nodes.find((n) => n.name === "draft")).toMatchObject({
      initial: true,
      setters: ["Analyst", "Reviewer", "Sample and result QA"],
      reason: "required",
    });
  });

  it("draws a state reached from every other once, not an arrow from each", () => {
    const model = modelOf(SAMPLE, "status");
    expect(model.nodes.filter((n) => n.fromAny).map((n) => n.name)).toEqual([
      "registered",
      "received",
      "in_testing",
    ]);
    expect(model.edges.some((e) => e.to === "received")).toBe(false);
    expect(model.edges.filter((e) => e.kind === "move").map((e) => e.id)).toEqual([
      "in_testing>in_review",
      "in_review>approved",
      "approved>released",
      "in_review>rejected",
      "approved>rejected",
    ]);
  });

  it("says each state in words, with its guards' messages", () => {
    const model = modelOf(RESULT, "review");
    const node = (name: string) => {
      const found = model.nodes.find((n) => n.name === name);
      if (!found) throw new Error(name);
      return found;
    };
    expect(wayIn(node("draft"), model)).toBe(
      "Analyst, Reviewer or Sample and result QA set it from In review or Reviewed, saying why.",
    );
    expect(wayIn(node("released"), model)).toBe(
      "Sample and result QA sets it from Reviewed by signing it: QA release (Approval).",
    );
    expect(whatItMeans(node("in_review"), model)).toBe(
      "It locks the record: its values can’t change. Correcting a value returns it to Draft, with a reason.",
    );
    expect(node("released").checks.map((c) => c.message)).toContain(
      "You reviewed this result, so someone else in QA releases it.",
    );
    const research = modelOf(RESEARCH, "review");
    expect(wayIn(research.nodes[0] as never, research)).toBe("No one moves a record back to it.");
    expect(plainMessage("You submitted {{ record.name }} for review.")).toBe(
      "You submitted … for review.",
    );
  });

  it("knows where a record can go next", () => {
    const model = modelOf(RESULT, "review");
    expect(nextFrom(model, "in_review").map((n) => n.name)).toEqual(["draft", "reviewed"]);
    expect(nextFrom(model, "released")).toEqual([]);
    const lot = modelOf(LOT, "disposition");
    expect(nextFrom(lot, "released").map((n) => n.name)).toEqual([
      "quarantined",
      "on_hold",
      "rejected",
      "expired",
    ]);
  });

  it("names a lab role by its label, or by its name made readable", () => {
    expect(roleLabel(ROLES, "iki/lots.qa")).toBe("Lot QA");
    expect(roleLabel({}, "acme/qms.qa")).toBe("QA");
    expect(roleLabel({}, "acme/qms.lab_manager")).toBe("Lab manager");
  });
});

describe("laying a flow out", () => {
  it("puts states side by side where they fit, and one above another where they don't", () => {
    const model = modelOf(RESULT, "review");
    const wide = layout(model, 1100);
    expect(wide.orientation).toBe("row");
    expect(wide.width).toBeLessThanOrEqual(1100);
    const xs = wide.nodes.map((n) => n.x);
    expect(xs).toEqual([...xs].sort((a, b) => a - b));
    const narrow = layout(model, 358);
    expect(narrow.orientation).toBe("column");
    expect(narrow.width).toBeLessThanOrEqual(358);
    const ys = narrow.nodes.map((n) => n.y);
    expect(ys).toEqual([...ys].sort((a, b) => a - b));
  });

  it("keeps arcs that would cross in lanes of their own, forward on one side, back on the other", () => {
    const placed = layout(modelOf(RESULT, "review"), 1100);
    const nodeTop = Math.min(...placed.nodes.map((n) => n.y));
    const nodeBottom = Math.max(...placed.nodes.map((n) => n.y + n.height));
    const ys = (id: string) =>
      [...(placed.edges.find((e) => e.id === id)?.path.matchAll(/-?\d+(?:\.\d+)?/g) ?? [])]
        .map((m) => Number(m[0]))
        .filter((_, k) => k % 2 === 1);
    // Back: below the row.
    for (const id of ["in_review>draft", "reviewed>draft", "in_review~draft", "reviewed~draft"]) {
      expect(Math.max(...ys(id))).toBeGreaterThan(nodeBottom);
    }
    // Each back arc has a lane of its own: none at the same depth.
    const depths = ["in_review>draft", "reviewed>draft", "in_review~draft", "reviewed~draft"].map(
      (id) => Math.max(...ys(id)),
    );
    expect(new Set(depths).size).toBe(4);
    // The next state's a straight arrow, level with the nodes.
    expect(new Set(ys("draft>in_review")).size).toBe(1);
    expect(ys("draft>in_review")[0]).toBeGreaterThan(nodeTop);
  });

  it("starts a state reached from any other at a dot of its own", () => {
    const placed = layout(modelOf(SAMPLE, "status"), 400);
    expect(placed.stubs.map((s) => s.to)).toEqual(["registered", "received", "in_testing"]);
    for (const stub of placed.stubs) {
      const node = placed.nodes.find((n) => n.name === stub.to);
      expect(stub.cx).toBeGreaterThan((node?.x ?? 0) + (node?.width ?? 0));
    }
  });
});
