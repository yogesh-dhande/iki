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
    approval: null,
    reason: "",
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
    metrics: {},
    messages: [],
    waiting: null,
    can: { cancel: true },
    ...overrides,
  };
}

/** A request an agent made for `run`, waiting for the person who signed it in. */
export function approval(
  overrides: Partial<Schemas["RunApprovalOut"]> = {},
): Schemas["RunApprovalOut"] {
  return {
    id: "00000000-0000-7000-8000-0000000000a1",
    state: "pending",
    client: "Claude",
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
    queued_at: "2026-03-14T09:00:00Z",
    started_at: "2026-03-14T09:00:05Z",
    finished_at: null,
    progress: 0.25,
    progress_message: "Weighing",
    log_size: 6,
    error: null,
    network: [],
    secrets: [],
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
  available: true,
  unavailable_reason: null,
  needs: "editor",
} satisfies Schemas["ActionOut"];
