import { assertType, expectTypeOf, it } from "vitest";

import type { Schemas } from "./index.js";

it("accepts receipt and count requests with omitted or explicit defaults", () => {
  const plan = { plan_revision: 1 };
  assertType<Schemas["ReceiptIn"]>(plan);
  assertType<Schemas["ReceiptIn"]>({ ...plan, note: "", reason: "" });
  assertType<Schemas["ReceiptIn"]>({ ...plan, note: null, reason: null });
  assertType<Schemas["ReceiptLineIn"]>({ outcome: "matched" });
  assertType<Schemas["ReceiptLineIn"]>({ outcome: "matched", note: "", reason: "" });
  assertType<Schemas["ReceiptLineIn"]>({ outcome: "matched", note: null, reason: null });
  assertType<Schemas["CompleteReceiptIn"]>(plan);

  const scans = [{ position: "A1", code: "tube-1" }];
  assertType<Schemas["ScansIn"]>({ scans });
  assertType<Schemas["ScansIn"]>({ scans, reason: "" });
  assertType<Schemas["ScansIn"]>({ scans, reason: null });
  const decision = { line_revision: 1, resolution: null };
  assertType<Schemas["ResolveIn"]>(decision);
  assertType<Schemas["ResolveIn"]>({ ...decision, note: "", reason: "" });
  assertType<Schemas["ResolveIn"]>({ ...decision, note: null, reason: null });
  assertType<Schemas["CompleteReconciliationIn"]>(plan);
  assertType<Schemas["CompleteReconciliationIn"]>({ ...plan, note: "" });
  assertType<Schemas["CompleteReconciliationIn"]>({ ...plan, note: null });
});

it("requires a reason to cancel a receipt or count, or remove or restore a receipt extra", () => {
  const plan = { plan_revision: 1 };
  assertType<Schemas["CancelReceiptIn"]>({ ...plan, reason: "Wrong shipment" });
  assertType<Schemas["CancelReconciliationIn"]>({ ...plan, reason: "Wrong box" });
  expectTypeOf(plan).not.toExtend<Schemas["CancelReceiptIn"]>();
  expectTypeOf(plan).not.toExtend<Schemas["CancelReconciliationIn"]>();
  expectTypeOf({ ...plan, reason: null }).not.toExtend<Schemas["CancelReceiptIn"]>();
  expectTypeOf({ ...plan, reason: null }).not.toExtend<Schemas["CancelReconciliationIn"]>();

  const line = { line_revision: 1 };
  assertType<Schemas["ReviewedExtraIn"]>({ ...line, reason: "Correcting the scan" });
  expectTypeOf(line).not.toExtend<Schemas["ReviewedExtraIn"]>();
  expectTypeOf({ ...line, reason: null }).not.toExtend<Schemas["ReviewedExtraIn"]>();
});
