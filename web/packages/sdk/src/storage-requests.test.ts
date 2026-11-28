import { assertType, expectTypeOf, it } from "vitest";

import type { Schemas } from "./index.js";

it("accepts ordinary storage labels without an explicit scan mode or a vendor symbol", () => {
  assertType<Schemas["ReceiptScanIn"]>({ code: "tube-1" });
  assertType<Schemas["ScanResolveIn"]>({ scope: "destination", code: "box-1" });
  assertType<Schemas["PutAwayIn"]>({ line: "line-1", to: "box-1" });
  assertType<Schemas["MoveItemIn"]>({ record: "tube-1", to: "box-1" });
  assertType<Schemas["CheckInItemIn"]>({ record: "tube-1" });
  assertType<Schemas["LabelsIn"]>({
    records: ["tube-1"],
    size: "cryo",
    format: "print",
    symbology: "datamatrix",
    dpi: 203,
    copies: 1,
    preview: true,
  });
});

it("accepts receipt and count requests with omitted or explicit defaults", () => {
  const plan = {};
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
  const decision = { resolution: null };
  assertType<Schemas["ResolveIn"]>(decision);
  assertType<Schemas["ResolveIn"]>({ ...decision, note: "", reason: "" });
  assertType<Schemas["ResolveIn"]>({ ...decision, note: null, reason: null });
  assertType<Schemas["CompleteReconciliationIn"]>(plan);
  assertType<Schemas["CompleteReconciliationIn"]>({ ...plan, note: "" });
  assertType<Schemas["CompleteReconciliationIn"]>({ ...plan, note: null });
});

it("requires a reason to cancel a receipt or count, or remove or restore a receipt extra", () => {
  // The revisions they're made from are If-Match: the body is only why.
  const plan = {};
  assertType<Schemas["CancelReceiptIn"]>({ ...plan, reason: "Wrong shipment" });
  assertType<Schemas["CancelReconciliationIn"]>({ ...plan, reason: "Wrong box" });
  expectTypeOf(plan).not.toExtend<Schemas["CancelReceiptIn"]>();
  expectTypeOf(plan).not.toExtend<Schemas["CancelReconciliationIn"]>();
  expectTypeOf({ ...plan, reason: null }).not.toExtend<Schemas["CancelReceiptIn"]>();
  expectTypeOf({ ...plan, reason: null }).not.toExtend<Schemas["CancelReconciliationIn"]>();

  const line = {};
  assertType<Schemas["ExtraReasonIn"]>({ ...line, reason: "Correcting the scan" });
  expectTypeOf(line).not.toExtend<Schemas["ExtraReasonIn"]>();
  expectTypeOf({ ...line, reason: null }).not.toExtend<Schemas["ExtraReasonIn"]>();
});
