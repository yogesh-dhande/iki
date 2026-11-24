// The values the API reference's examples show, from one lab's work: the QC dose-response
// result QSF-000014 from the dose-response plate assay, which its analyst, Amara Okafor,
// has signed as its author. A field's own documented example comes
// first; these fill in the rest, so an example reads like a deployment's answer rather
// than a list of types. The same field gets the same value on every page.

/** A UUID that's the same for the same seed: an example's IDs agree from page to page. */
export function uuidFor(seed: string): string {
  // Four rounds of FNV-1a over the seed, each from a different start, make 128 bits.
  const words = [0x811c9dc5, 0x01000193, 0x9e3779b9, 0x85ebca6b].map((start) => {
    let hash = start >>> 0;
    for (const char of seed) {
      hash = Math.imul(hash ^ (char.codePointAt(0) ?? 0), 0x01000193) >>> 0;
    }
    return hash.toString(16).padStart(8, "0");
  });
  const hex = words.join("");
  const variant = ((parseInt(hex.charAt(16), 16) & 0x3) | 0x8).toString(16);
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    `4${hex.slice(13, 16)}`,
    `${variant}${hex.slice(17, 20)}`,
    hex.slice(20, 32),
  ].join("-");
}

const PERSON = "Amara Okafor";
const AT = "2026-11-18T14:03:22Z";

/** Values by format, where the field's name says nothing more. */
const BY_FORMAT: Record<string, (seed: string) => string> = {
  uuid: (seed) => uuidFor(seed),
  "date-time": () => AT,
  date: () => "2026-11-18",
  time: () => "14:03:22",
  email: () => "amara.okafor@lab.example.org",
  uri: () => "https://iki.lab.example.org/records/QSF-000014/",
  "uri-reference": () => "/records/QSF-000014/",
  duration: () => "PT15M",
  binary: () => "(the file's bytes)",
  byte: () => "aWtp",
  decimal: () => "12.34",
};

/** Values by a string field's name, read in the example lab. */
const BY_NAME: Record<string, string> = {
  name: "QC plate assays",
  title: "IC50 of the reference standard, plate APL-000007",
  description: "Dose-response plates read at 484 nm and fitted to a four-parameter logistic.",
  reason: "A4: dispensing error noted at acquisition",
  note: "A4 excluded; the other seven sample wells fit within range.",
  notes: "A4 excluded; the other seven sample wells fit within range.",
  message: "Return a submitted or reviewed result for correction.",
  error: "The reader file has no Raw Data (484) block.",
  detail: "The reader file has no Raw Data (484) block.",
  hrid: "QSF-000014",
  ref: "QSF-000014",
  record: "QSF-000014",
  type: "iki/plate_assays.RequestedQCAssayResult",
  entity_type: "iki/plate_assays.RequestedQCAssayResult",
  next_cursor: "eyJhZnRlciI6IjIwMjYtMTEtMThUMTQ6MDM6MjJaIn0",
  cursor: "eyJhZnRlciI6IjIwMjYtMTEtMThUMTQ6MDM6MjJaIn0",
  field: "review",
  state: "submitted",
  status: "submitted",
  from: "recorded",
  to: "submitted",
  field_label: "Review",
  from_label: "Recorded",
  to_label: "Submitted",
  meaning_label: "Authorship",
  role: "iki/plate_assays.qa",
  role_label: "QA",
  action: "iki/plate_assays.fit_curve",
  action_label: "Fit a dose-response curve",
  workflow: "iki/protein.translate_and_characterize",
  plugin: "iki/plate_assays",
  plugin_version: "1.4.0",
  plugin_sha: "3f9c2e71a4b05d6c8e1f2a3b4c5d6e7f8a9b0c1d",
  version: "1.4.0",
  data_version: "1",
  sha256: "9b2f6c1d7e0a4f3b8c5d2e9a1f6b7c3d0e4a8b5c2f9d6e1a7b3c0d4e8f5a2b6c",
  media_type: "text/csv",
  unit: "nM",
  unit_symbol: "nM",
  display_unit: "nM",
  display_unit_symbol: "nM",
  q: "IC50",
  id: "QSF-000014",
  host: "fold.example.org",
  url: "https://iki.lab.example.org/records/QSF-000014/",
  href: "/records/QSF-000014/",
  uri: "https://iki.lab.example.org/records/QSF-000014/",
  client_id: "iki-cli",
  environment: "iki/plate_assays.assay-tools",
  currency: "USD",
  slug: "qc-plate-assays",
  project_slug: "qc-plate-assays",
  email: "amara.okafor@lab.example.org",
  why_not: "QA releases an independently reviewed submission.",
  why: "QA releases an independently reviewed submission.",
  disabled_reason: "QA releases an independently reviewed submission.",
  unavailable_reason: "QA releases an independently reviewed submission.",
  statement: "I made this record, and what it says is accurate and complete.",
  key: "ed25519:7Jq2mV9xL4pR8sT1wY6zB3nC5dF0gH2k",
  run: "RUN-000231",
  batch: "BAT-000003",
  purpose: "Predict the structure of each new antibody Fv",
  deployment: "iki.lab.example.org",
  collection: "COL-000002",
  slurm_cluster: "hpc1",
  cluster: "hpc1",
  partition: "standard",
  qos: "normal",
  account: "assay-dev",
  text: "A4 excluded; the other seven sample wells fit within range.",
  sort: "-updated_at",
  by: PERSON,
  handed_over_by: "Courier, World Courier",
  initial: "recorded",
  when: AT,
  imported_from: "mars-96-series.csv",
  hint: "Choose IC50 for an inhibitor, EC50 for an agonist.",
  model: "ESMFold v1",
  software: "minimap2 2.31",
  reference: "L09137.2",
  api: "iki/example_models.foldcloud",
  manifestation: "Authored by Amara Okafor on 2026-11-18 at 14:03:22 UTC. Meaning: Authorship.",
  subject: "amara.okafor@lab.example.org",
  sample: "SMP-000021",
  schedule: "0 6 * * 1-5",
  timezone: "Europe/London",
  time_zone: "Europe/London",
  cancel_reason: "Superseded by a new fit with A4 excluded",
  progress_message: "Fitting 7 sample wells",
  value: "12.34",
  sequence: "MKTAYIAKQRQISFVKSHFSRQ",
  secret: "(shown once, when it's made)",
  timestamp: AT,
  issuer: "https://login.lab.example.org",
  context: "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport",
  document: '{"format":"iki/signature/v1","record":"QSF-000014","revision":7}',
  person: PERSON,
  signer: PERSON,
  author: PERSON,
  display_name: PERSON,
  full_name: PERSON,
  username: "aokafor",
  acquired_at: AT,
  message_id: "<20261118140322.QSF-000014@iki.lab.example.org>",
  software_version: "MARS 4.01",
  lot: "RS-2026-031",
  wavelength: "484 nm",
};

/** Numbers by a field's name. */
const NUMBERS: Record<string, number> = {
  revision: 7,
  entry_revision: 3,
  carried_by: 7,
  index: 0,
  number: 7,
  count: 8,
  total: 8,
  limit: 50,
  replicate: 1,
  wells: 8,
  concentration: 8,
  endpoint_value: 12.34,
  ci_low: 10.65,
  ci_high: 14.3,
  hill_slope: -1.0,
  size: 1843,
  bytes: 1843,
  depth: 3,
  attempt: 1,
};

/** The text a field's description quotes as its example: “QA approval”, or `ed25519:...`. */
export function quotedExample(description: string | undefined): string | null {
  const quoted = /“([^”]+)”/.exec(description ?? "");
  return quoted?.[1] ?? null;
}

/** What a `name` is, by the schema it's in: a person's, a project's, a plugin's… */
const NAMES: [RegExp, string][] = [
  [/Person|User|Member|Signer|Author|Actor|Contributor|Reviewer|Me(Out|In)?$/, PERSON],
  [/Plugin/, "iki/plate_assays"],
  [/EntityType|TypeOut|TypeIn/, "iki/plate_assays.RequestedQCAssayResult"],
  [/Action/, "iki/plate_assays.fit_curve"],
  [/Workflow/, "iki/protein.translate_and_characterize"],
  [/Team/, "Assay development"],
  [/Registry/, "Biologics registry"],
  [/Webhook/, "LIMS results feed"],
  [/Token/, "LIMS sync"],
  [/File|Upload|Import/, "mars-96-series.csv"],
  [/Environment/, "iki/plate_assays.assay-tools"],
  [/Slurm|Cluster/, "hpc1"],
  [/Collection/, "November QC plates"],
  [/Saved|View/, "Released IC50s this month"],
  [/Trigger/, "iki/small_molecule.compute_new_compound_descriptors"],
  [/Component/, "iki/plate_assays.plate_map"],
  [/Organism|Taxon/, "Homo sapiens"],
];

/** A string field's example: by its format, its name (in the schema `owner`), or the
 * example its description quotes. */
export function sampleString(
  field: string | undefined,
  format: string | undefined,
  description: string | undefined,
  seed: string,
  owner = "",
): string {
  if (format && format !== "decimal" && BY_FORMAT[format]) {
    return BY_FORMAT[format](seed);
  }
  if (field === "name") {
    return NAMES.find(([pattern]) => pattern.test(owner))?.[1] ?? "QC plate assays";
  }
  if (field && field in BY_NAME) {
    return BY_NAME[field] ?? "";
  }
  if (field?.endsWith("_id") || field === "id") {
    return uuidFor(seed);
  }
  if (field?.endsWith("_at")) {
    return AT;
  }
  const quoted = quotedExample(description);
  if (quoted) {
    return quoted;
  }
  if (format && BY_FORMAT[format]) {
    return BY_FORMAT[format](seed);
  }
  if (field === "label" || field?.endsWith("_label")) {
    return NAMES.find(([pattern]) => pattern.test(owner))?.[1] ?? "Submitted";
  }
  if (field?.endsWith("_name")) {
    return /^(signer|author|requested|decided|created|updated|actor|user|person|reviewer)/.test(
      field,
    )
      ? PERSON
      : (NAMES.find(([pattern]) => pattern.test(owner))?.[1] ?? "QC plate assays");
  }
  if (field?.endsWith("_by")) {
    return PERSON;
  }
  if (field?.endsWith("_reason")) {
    return BY_NAME["reason"] ?? "";
  }
  // A field this lab has no value for: its name, in words.
  return (field ?? "value").replace(/_/g, " ");
}

export function sampleNumber(field: string | undefined, minimum: number | undefined): number {
  if (field && field in NUMBERS) {
    return NUMBERS[field] ?? 0;
  }
  return Math.max(minimum ?? 1, 1);
}

/** A path parameter's value in the example lab, by the path it's in. */
export function pathValue(path: string, name: string): string {
  if (name === "ref") {
    if (path.startsWith("/api/v1/batches/")) return "BAT-000003";
    if (path.startsWith("/api/v1/collections/")) return "COL-000002";
    if (path.startsWith("/api/v1/compute/setups/")) return "SETUP-000004";
    if (path.startsWith("/api/v1/runs/")) return "RUN-000231";
    if (path.startsWith("/api/v1/workflow-runs/")) return "WF-000012";
    return "QSF-000014";
  }
  if (name === "name") {
    if (path.startsWith("/api/v1/actions/")) return "iki/plate_assays.fit_curve";
    if (path.startsWith("/api/v1/entity-types/")) return "iki/plate_assays.RequestedQCAssayResult";
    if (path.startsWith("/api/v1/workflows/")) return "iki/protein.translate_and_characterize";
    if (path.startsWith("/api/v1/components/")) return "iki/plate_assays.plate_map";
    if (path.startsWith("/api/v1/outside-apis/")) return "iki/example_models.foldcloud";
    if (path.startsWith("/api/v1/plugins/")) return "iki/plate_assays";
    if (path.includes("/triggers/")) return "iki/small_molecule.compute_new_compound_descriptors";
  }
  if (name === "cluster") return "hpc1";
  if (name === "field") return "review";
  if (name === "number") return "7";
  if (name.endsWith("_id") || name === "id") return uuidFor(name);
  return sampleString(name, undefined, undefined, `${path}#${name}`);
}
