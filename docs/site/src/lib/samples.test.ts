import { describe, expect, it } from "vitest";

import { pathValue, quotedExample, sampleString, uuidFor } from "./samples.ts";

describe("the example lab's values", () => {
  it("makes a UUID per seed, the same each time", () => {
    expect(uuidFor("a")).toBe(uuidFor("a"));
    expect(uuidFor("a")).not.toBe(uuidFor("b"));
    expect(uuidFor("a")).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });

  it("names a thing by what it is", () => {
    expect(sampleString("name", undefined, undefined, "s", "PersonOut")).toBe("Amara Okafor");
    expect(sampleString("name", undefined, undefined, "s", "PluginOut")).toBe("iki/plate_assays");
    expect(sampleString("hrid", undefined, undefined, "s")).toBe("QSF-000014");
    expect(sampleString("created_at", undefined, undefined, "s")).toBe("2026-11-18T14:03:22Z");
    expect(sampleString("anything", "email", undefined, "s")).toBe("amara.okafor@lab.example.org");
  });

  it("uses the example a field's description quotes", () => {
    expect(quotedExample("How the plugin names it: “QA approval”.")).toBe("QA approval");
    expect(sampleString("caption", undefined, "Like “Plate 7”.", "s")).toBe("Plate 7");
  });

  it("fills a path with the record or plugin it names", () => {
    expect(pathValue("/api/v1/entities/{ref}/signatures", "ref")).toBe("QSF-000014");
    expect(pathValue("/api/v1/batches/{ref}", "ref")).toBe("BAT-000003");
    expect(pathValue("/api/v1/actions/{name}", "name")).toBe("iki/plate_assays.fit_curve");
    expect(pathValue("/api/v1/projects/{project_id}", "project_id")).toBe(uuidFor("project_id"));
  });
});
