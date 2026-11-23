import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import * as download from "./download";
import { PluginPreflight } from "./plugin-preflight";
import { answer, calls, problem } from "./test-server";

const plugin = "acme/dna";
const path = "/api/v1/plugins/acme/dna/preflight";
const manifest = { apiVersion: "iki/v1", plugin: { name: plugin, version: "2.0.0" } };
const backfill = "Backfill lab_id before making it required.";
const report = {
  plugin,
  version: "2.0.0",
  configuration_revision: "a".repeat(64),
  candidate_revision: "b".repeat(64),
  blocking: true,
  types: [
    {
      type: "acme/dna.DNASequence",
      total: 200000,
      checked: 500,
      sampled_checks: ["rule sequence_ok", "rule still_valid"],
      sampled: true,
      findings: [
        {
          kind: "required",
          subject: "lab_id",
          message: backfill,
          blocking: true,
          sampled: false,
          count: 2000,
          examples: ["DNA-001", "DNA-002"],
        },
        {
          kind: "rule",
          subject: "sequence_ok",
          message: "The proposed rule would hold these records for review.",
          blocking: true,
          sampled: true,
          count: 3,
          examples: ["DNA-003"],
        },
      ],
    },
  ],
};

function file(contents = JSON.stringify(manifest)) {
  return new File([contents], "manifest.json", { type: "application/json" });
}

describe("plugin schema preflight", () => {
  it("checks the chosen build and identifies the revision, partial coverage and affected records", async () => {
    const user = userEvent.setup();
    answer("POST " + path, report);
    render(<PluginPreflight plugin={plugin} />);
    const run = screen.getByRole("button", { name: "Run preflight" });
    expect(run).toBeDisabled();
    await user.upload(screen.getByLabelText("Proposed plugin manifest"), file());
    await user.click(run);
    expect(await screen.findByText(report.configuration_revision)).toBeVisible();
    expect(screen.getByText(report.candidate_revision)).toBeVisible();
    expect(calls("POST", path)[0]?.body).toEqual(manifest);
    expect(calls("POST", path)[0]?.headers.get("X-CSRFToken")).toBe("test-csrf-token");
    expect(screen.getByRole("heading", { name: "acme/dna.DNASequence" })).toBeVisible();
    expect(screen.getByText(/Sampled 500 of 200,000 records/)).toBeVisible();
    expect(screen.getByText(/rule still_valid/)).toBeVisible();
    expect(screen.getByText(/Exactly 2,000 affected records/)).toBeVisible();
    expect(screen.getByText(/At least 3 affected records/)).toBeVisible();
    expect(screen.getByText("DNA-002")).toBeVisible();
    expect(screen.getByText(backfill)).toBeVisible();
    const save = vi.spyOn(download, "saveFile").mockImplementation(() => undefined);
    try {
      await user.click(screen.getByRole("button", { name: "Download report (JSON)" }));
      const blob = save.mock.calls[0]?.[0];
      expect(await blob?.text()).toBe(JSON.stringify(report, null, 2) + "\n");
    } finally {
      save.mockRestore();
    }
    await user.upload(screen.getByLabelText("Proposed plugin manifest"), file("!"));
    expect(screen.queryByText(report.candidate_revision)).toBeNull();
    await user.click(run);
    expect(await screen.findByRole("alert")).toHaveTextContent(/valid JSON/i);
    expect(calls("POST", path)).toHaveLength(1);
    expect(screen.queryByRole("button", { name: /Download report/ })).toBeNull();
  });

  it("checks a new plugin from the plugins index using the name in its manifest", async () => {
    const user = userEvent.setup();
    const name = "acme/measurements";
    const endpoint = "/api/v1/plugins/acme/measurements/preflight";
    answer("POST " + endpoint, { ...report, plugin: name, blocking: false, types: [] });
    render(<PluginPreflight />);
    await user.upload(
      screen.getByLabelText("Proposed plugin manifest"),
      file(JSON.stringify({ ...manifest, plugin: { name, version: "2.0.0" } })),
    );
    await user.click(screen.getByRole("button", { name: "Run preflight" }));
    expect(await screen.findByText(name)).toBeVisible();
    expect(calls("POST", endpoint)).toHaveLength(1);
  });

  it("keeps a sample without matches distinct from a complete check", async () => {
    const user = userEvent.setup();
    answer("POST " + path, {
      ...report,
      types: [{ ...report.types[0], findings: [] }],
    });
    render(<PluginPreflight plugin={plugin} />);
    await user.upload(screen.getByLabelText("Proposed plugin manifest"), file());
    await user.click(screen.getByRole("button", { name: "Run preflight" }));
    expect(await screen.findByText(/Check incomplete/)).toBeVisible();
    expect(screen.getByText(/No findings among the records checked/)).toBeVisible();
    expect(screen.queryByText(/No blocking findings/)).toBeNull();
  });

  it("shows an API refusal and allows retrying the same file", async () => {
    const user = userEvent.setup();
    answer(
      "POST " + path,
      problem(422, "invalid_manifest", "This manifest is for another plugin."),
    );
    render(<PluginPreflight plugin={plugin} />);
    await user.upload(screen.getByLabelText("Proposed plugin manifest"), file());
    const run = screen.getByRole("button", { name: "Run preflight" });
    await user.click(run);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This manifest is for another plugin.",
    );
    answer("POST " + path, { ...report, blocking: false, types: [] });
    await user.click(run);
    expect(await screen.findByText(/No blocking findings/)).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
