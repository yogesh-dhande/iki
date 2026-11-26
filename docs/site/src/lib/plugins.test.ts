import { describe, expect, it } from "vitest";

import { anchors, extensionId, fieldRows, prepared, typeHref, type Plugin } from "./plugins.ts";

const lims: Plugin = {
  name: "acme/lims",
  short: "lims",
  slug: "acme-lims",
  title: "Acme LIMS",
  description: "Tracks samples (ADR 0125).",
  version: "0.8.0",
  path: "plugins/lims/plugin.yaml",
  roles: [{ name: "qa", label: "QA", description: "Releases samples." }],
  dependencies: ["iki/protein"],
  types: [
    {
      name: "Sample",
      label: "Sample",
      description: "A sample.",
      abstract: false,
      extends: [],
      path: "plugins/lims/types/sample.yaml",
      fields: [
        { key: "status", type: "state", label: "Status", description: "", flags: [] },
        {
          key: "readings",
          type: "list",
          items: "row",
          label: "Readings",
          description: "",
          flags: [],
          fields: [{ key: "value", type: "float", label: "Value", description: "", flags: [] }],
        },
      ],
    },
  ],
  extensions: [
    {
      extends: "iki/protein.Protein",
      path: "plugins/lims/types/protein.yaml",
      fields: [{ key: "lims_id", type: "string", label: "LIMS ID", description: "", flags: [] }],
    },
  ],
  actions: [
    {
      name: "import_from_lims",
      label: "Import",
      description: "",
      path: "plugins/lims/actions/import_from_lims.yaml",
      inputs: [],
      params: [],
      creates: [],
      updates: [],
    },
  ],
};

describe("the Plugins reference", () => {
  it("gives each type, field, extension and action its anchor", () => {
    expect(extensionId(lims.extensions[0] ?? { extends: "" })).toBe("extension-Protein");
    expect(
      fieldRows("Sample", lims.types[0]?.fields ?? []).map((row) => [row.id, row.depth]),
    ).toEqual([
      ["Sample.status", 0],
      ["Sample.readings", 0],
      ["Sample.readings.value", 1],
    ]);
    expect(anchors(lims)).toEqual([
      "roles",
      "role.qa",
      "Sample",
      "Sample.status",
      "Sample.readings",
      "Sample.readings.value",
      "extension-Protein",
      "extension-Protein.lims_id",
      "import_from_lims",
    ]);
  });

  it("links a type named in full to its plugin's page, when a first-party plugin has it", () => {
    expect(typeHref([lims], "acme/lims.Sample")).toBe("/reference/plugins/acme-lims/#Sample");
    expect(typeHref([lims], "acme/lims.Nothing")).toBeNull();
    expect(typeHref([lims], "acme/assays.Plate")).toBeNull();
  });

  it("drops design-record citations, and refuses an anchor twice", () => {
    expect(prepared([lims])[0]?.description).toBe("Tracks samples.");
    const twice: Plugin = { ...lims, actions: [...lims.actions, ...lims.actions] };
    expect(() => prepared([twice])).toThrow("acme/lims's page has #import_from_lims twice");
  });
});
