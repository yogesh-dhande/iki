/**
 * A type's flow as a diagram has it (ADR 0199), from the real plugins' flows as
 * `GET /entity-types/{type}/flow` serves them (ADR 0198): QC deliverables,
 * samples and requests, lots, and plate assays' research and regulated QC results
 * (flow-fixtures.json, which a server test keeps the plugins' own).
 */

import { describe, expect, it } from "vitest";

import type { FlowField } from "./flow";
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

function flowOf(type: string, field: string): FlowField {
  const found = fixtures.types.find((t) => t.type === type)?.fields.find((f) => f.name === field);
  if (!found) throw new Error(`No ${type} ${field} in the fixtures`);
  return found as FlowField;
}

/** Its moves as "from > to", a "?" after those that depend on more than the states. */
function moves(flow: FlowField): string[] {
  return flow.transitions.map((t) => `${t.from} > ${t.to}${t.conditional ? " ?" : ""}`);
}

function modelOf(type: string, field: string): FlowModel {
  return flowModel(flowOf(type, field), ROLES);
}

const RESULT = "iki/requests.QCDeliverable";
const SAMPLE = "iki/samples.Sample";
const REQUEST = "iki/requests.Request";
const LOT = "iki/lots.Lot";
const RESEARCH = "iki/plate_assays.ResearchAssayResult";
const QC = "iki/plate_assays.QCAssayResult";

describe("the plugins' flows", () => {
  it("draws an analysis result's regulated review: submitted, reviewed, released", () => {
    expect(moves(flowOf(RESULT, "review"))).toEqual([
      // Returned for correction only while it waits for review or release.
      "submitted > recorded",
      "reviewed > recorded",
      // Submitted from Recorded, once its measurement is recorded.
      "recorded > submitted ?",
      // Reviewed independently of who made and submitted it.
      "submitted > reviewed ?",
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

  it("has a flow for each of a type's state fields", () => {
    const request = fixtures.types.find((t) => t.type === REQUEST);
    expect(request?.fields.map((f) => [f.name, f.label, f.initial])).toEqual([
      ["standing", "Standing", "open"],
      ["status", "Status", "submitted"],
    ]);
    expect(moves(flowOf(REQUEST, "standing"))).toEqual(["withdrawn > open", "open > withdrawn"]);
    // Without guards, each state the core sets can follow any other.
    expect(flowOf(REQUEST, "status").transitions).toHaveLength(5 * 4);
  });
});

describe("a flow as a diagram has it", () => {
  it("says who sets each signed state and what they sign", () => {
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
          statement: "I release the independently reviewed deliverable to the requesting project.",
          roles: ["Sample and result QA"],
        },
      ],
    });
    expect(model.edges.filter((e) => e.kind === "correction")).toEqual([]);
    expect(model.nodes.find((n) => n.name === "recorded")).toMatchObject({
      initial: true,
      setters: ["Analyst", "Reviewer", "Sample and result QA"],
      reason: "required",
    });
  });

  it("draws an unsigned lock's return on edit", () => {
    const review = flowOf(RESULT, "review");
    const states = review.states.map((s) =>
      s.name === "submitted" ? { ...s, signatures: [], on_edit: "recorded" } : s,
    );
    const model = flowModel({ ...review, states }, ROLES);
    const submitted = model.nodes.find((n) => n.name === "submitted");
    if (!submitted) throw new Error("Missing Submitted state");
    expect(model.edges.filter((e) => e.kind === "correction").map((e) => e.id)).toEqual([
      "submitted~recorded",
    ]);
    expect(whatItMeans(submitted, model)).toContain(
      "Correcting a value returns it to Recorded, with a reason.",
    );
  });

  it("draws a state reached from every other once, not an arrow from each", () => {
    const model = modelOf(REQUEST, "status");
    expect(model.nodes.filter((n) => n.fromAny).map((n) => n.name)).toEqual([
      "submitted",
      "accepted",
      "in_progress",
      "delivered",
      "declined",
    ]);
    expect(model.edges.filter((e) => e.kind === "move")).toEqual([]);
  });

  it("draws a reviewed sample's signed return to testing and its next review", () => {
    const model = modelOf(SAMPLE, "status");
    expect(nextFrom(model, "reviewed").map((n) => n.name)).toEqual(["retesting"]);
    expect(nextFrom(model, "retesting").map((n) => n.name)).toEqual(["in_review"]);
    expect(model.nodes.find((n) => n.name === "retesting")).toMatchObject({
      from: ["in_review", "reviewed"],
      setters: ["Reviewer"],
      reason: "required",
      locks: false,
      signatures: [{ label: "Responsibility", meaning: "Responsibility" }],
    });
    expect(model.edges.filter((e) => e.kind === "correction").map((e) => e.id)).toEqual([]);
  });

  it("says each state in words, with its guards' messages", () => {
    const model = modelOf(RESULT, "review");
    const node = (name: string) => {
      const found = model.nodes.find((n) => n.name === name);
      if (!found) throw new Error(name);
      return found;
    };
    expect(wayIn(node("recorded"), model)).toBe(
      "Analyst, Reviewer or Sample and result QA set it from Submitted or Reviewed, saying why.",
    );
    expect(wayIn(node("released"), model)).toBe(
      "Sample and result QA sets it from Reviewed by signing it: QA release (Approval).",
    );
    expect(whatItMeans(node("submitted"), model)).toBe(
      "It locks the record: its values can’t change.",
    );
    expect(node("released").checks.map((c) => c.message)).toContain(
      "Have someone in QA who did not create, submit, edit or review the deliverable release it.",
    );
    const research = modelOf(RESEARCH, "review");
    expect(wayIn(research.nodes[0] as never, research)).toBe("No one moves a record back to it.");
    expect(plainMessage("You submitted {{ record.name }} for review.")).toBe(
      "You submitted … for review.",
    );
  });

  it("knows where a record can go next", () => {
    const model = modelOf(RESULT, "review");
    expect(nextFrom(model, "submitted").map((n) => n.name)).toEqual(["recorded", "reviewed"]);
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
    const back = ["submitted>recorded", "reviewed>recorded"];
    for (const id of back) {
      expect(Math.max(...ys(id))).toBeGreaterThan(nodeBottom);
    }
    // Each back arc has a lane of its own: none at the same depth.
    const depths = back.map((id) => Math.max(...ys(id)));
    expect(new Set(depths).size).toBe(back.length);
    // The next state's a straight arrow, level with the nodes.
    expect(new Set(ys("recorded>submitted")).size).toBe(1);
    expect(ys("recorded>submitted")[0]).toBeGreaterThan(nodeTop);
  });

  it("starts a state reached from any other at a dot of its own", () => {
    const placed = layout(modelOf(REQUEST, "status"), 400);
    expect(placed.stubs.map((s) => s.to)).toEqual([
      "submitted",
      "accepted",
      "in_progress",
      "delivered",
      "declined",
    ]);
    for (const stub of placed.stubs) {
      const node = placed.nodes.find((n) => n.name === stub.to);
      expect(stub.cx).toBeGreaterThan((node?.x ?? 0) + (node?.width ?? 0));
    }
  });
});
