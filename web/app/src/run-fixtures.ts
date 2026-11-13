/** Actions, runs and attempts for the run pages' tests. */

import type { Schemas } from "@ikisci/sdk";

import { field, KINASE } from "./entity-fixtures";
import { ME } from "./test-server";

export const RUN_ID = "00000000-0000-7000-8000-0000000000r1";

export function run(overrides: Partial<Schemas["RunOut"]> = {}): Schemas["RunOut"] {
  return {
    id: RUN_ID,
    hrid: "RUN-000001",
    action: "protein.compute_properties",
    plugin_version: "1.2.0",
    plugin_sha: "",
    project: KINASE.id,
    requested_by: { id: ME.id, name: ME.name },
    via: "web",
    client: "",
    agent: null,
    approval: null,
    reason: "",
    batch: null,
    workflow: null,
    tier: "backend",
    environment: "sequence-tools",
    params: { ph: 7.4 },
    settings: {},
    network: [],
    files: {},
    inputs: [
      {
        name: "protein",
        entity: "00000000-0000-7000-8000-00000000e001",
        hrid: "PRT-000001",
        revision: 2,
      },
    ],
    status: "running",
    attempts: 1,
    created_at: "2026-03-14T09:00:00Z",
    started_at: "2026-03-14T09:00:05Z",
    finished_at: null,
    cancel_requested_at: null,
    error: null,
    effects: [],
    effects_next_cursor: null,
    effect_counts: { created: 0, updated: 0, reused: 0 },
    metrics: {},
    messages: [],
    waiting: null,
    needs_worker: null,
    seed: 1234567,
    reused: false,
    reused_from: null,
    reproduces: null,
    reproduction: null,
    can: { cancel: true, reproduce: false, run_again: false },
    cost: null,
    ...overrides,
  };
}

/** An agent acting for the person, through their grant, in an MCP session. */
export const AGENT: Schemas["AgentOut"] = {
  client_id: "c-81f3",
  name: "Claude",
  grant: "00000000-0000-7000-8000-0000000000g1",
  mcp_session: "5e55",
  reported: { software: "claude-code 2.1", model: "claude-opus-5-5" },
};

/** A request an agent made for `run`, waiting for the person who signed it in. */
export function approval(
  overrides: Partial<Schemas["RunApprovalOut"]> = {},
): Schemas["RunApprovalOut"] {
  return {
    id: "00000000-0000-7000-8000-0000000000a1",
    state: "pending",
    client: "Claude",
    agent: AGENT,
    requested_by: { id: ME.id, name: ME.name },
    created_at: "2026-03-14T09:00:00Z",
    expires_at: "2026-03-21T09:00:00Z",
    decided_by: null,
    decided_at: null,
    note: "",
    runs: [{ id: RUN_ID, hrid: "RUN-000001" }],
    can: { decide: true },
    ...overrides,
  };
}

export function attempt(
  overrides: Partial<Schemas["RunAttemptOut"]> = {},
): Schemas["RunAttemptOut"] {
  return {
    attempt: 1,
    state: "running",
    worker: "pod-7",
    environment_build: "sha256:4f2a9c1e7b3d5a6f8e9d0c1b2a3f4e5d",
    template: "",
    queued_at: "2026-03-14T09:00:00Z",
    started_at: "2026-03-14T09:00:05Z",
    finished_at: null,
    progress: 0.25,
    progress_message: "Weighing",
    log_size: 6,
    error: null,
    network: [],
    secrets: [],
    volumes: {},
    pack: null,
    bulk_import: null,
    checkpoint: null,
    resumed_from: null,
    ...overrides,
  };
}

export const BATCH_ID = "00000000-0000-7000-8000-0000000000b1";

/** BAT-000001: protein.compute_properties on three proteins, one done, one going. */
export function batch(overrides: Partial<Schemas["BatchOut"]> = {}): Schemas["BatchOut"] {
  return {
    id: BATCH_ID,
    hrid: "BAT-000001",
    action: "protein.compute_properties",
    project: KINASE.id,
    requested_by: { id: ME.id, name: ME.name },
    via: "web",
    client: "",
    agent: null,
    reason: "Weights for the kinase panel",
    each: "protein",
    size: 3,
    statuses: { succeeded: 1, running: 2 },
    created_at: "2026-03-14T09:00:00Z",
    finished_at: null,
    can: { cancel: true },
    ...overrides,
  };
}

export const ACTION = {
  name: "protein.compute_properties",
  plugin: "protein",
  label: "Compute properties",
  description: null,
  tier: "backend",
  environment: "sequence-tools",
  inputs: [
    {
      name: "protein",
      label: "Protein",
      description: null,
      type: "protein.Protein",
      cardinality: "one",
      max: null,
      optional: false,
    },
  ],
  params: [
    {
      ...field({ name: "ph", label: "pH", type: "float" }),
      default: 7,
      min: 0,
      max: 14,
      min_length: null,
      max_length: null,
      pattern: null,
    },
  ],
  params_schema: {
    type: "object",
    properties: {
      ph: {
        anyOf: [{ type: "number", minimum: 0, maximum: 14 }, { type: "null" }],
        title: "pH",
        default: 7,
      },
    },
    required: [],
    additionalProperties: false,
  },
  creates: [],
  updates: [],
  deterministic: true,
  network: [],
  secrets: [],
  timeout: 600,
  batchable: 1,
  available: true,
  unavailable_reason: null,
  needs: "editor",
  estimate: null,
} satisfies Schemas["ActionOut"];

export const WORKFLOW_ID = "00000000-0000-7000-8000-0000000000w1";

/** WF-000001: protein.translate_and_characterize, translated, characterizing. */
export function workflowRun(
  overrides: Partial<Schemas["WorkflowRunOut"]> = {},
): Schemas["WorkflowRunOut"] {
  return {
    id: WORKFLOW_ID,
    hrid: "WF-000001",
    workflow: "protein.translate_and_characterize",
    label: "Translate and characterize",
    plugin_version: "1.2.0",
    project: KINASE.id,
    requested_by: { id: ME.id, name: ME.name },
    via: "web",
    client: "",
    agent: null,
    reason: "The kinase panel",
    inputs: [
      {
        name: "sequence",
        entity: "00000000-0000-7000-8000-00000000d001",
        hrid: "DNA-000001",
        revision: 1,
      },
    ],
    params: { frame: 1 },
    status: "running",
    steps: [
      {
        id: "translate",
        label: "Translate",
        action: "protein.translate",
        after: [],
        state: "succeeded",
        started_at: "2026-03-14T09:00:00Z",
        finished_at: "2026-03-14T09:00:30Z",
        error: null,
        run: { id: RUN_ID, hrid: "RUN-000001", status: "succeeded" },
      },
      {
        id: "properties",
        label: "Properties",
        action: "protein.compute_properties",
        after: ["translate"],
        state: "running",
        started_at: "2026-03-14T09:00:31Z",
        finished_at: null,
        error: null,
        run: { id: "00000000-0000-7000-8000-0000000000r2", hrid: "RUN-000002", status: "queued" },
      },
    ],
    created_at: "2026-03-14T09:00:00Z",
    finished_at: null,
    cancel_requested_at: null,
    error: null,
    can: { cancel: true },
    ...overrides,
  };
}
