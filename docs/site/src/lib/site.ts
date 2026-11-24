// The site's map: who reads it (the three doors), what each audience can do and
// understand, and where each item is. An item is a page of the site, or, until its page
// is written, the document in the repository that covers it today, or neither yet.
import { github } from "./repo.ts";

export type Audience = "bench" | "run" | "build";

export interface AudienceInfo {
  id: Audience;
  /** The door's name, which every page for this audience wears. */
  label: string;
  who: string;
  does: string;
  /** Where "All guides" goes. */
  all: string;
}

export const AUDIENCES: Record<Audience, AudienceInfo> = {
  bench: {
    id: "bench",
    label: "At the bench",
    who: "Scientists, QA and lab managers",
    does: "Register what you make, run analyses on it, review and sign results.",
    all: "/guides/#bench",
  },
  run: {
    id: "run",
    label: "Running Iki",
    who: "Admins who deploy and operate it",
    does: "Install on Kubernetes, sign people in, add plugins and compute.",
    all: "/guides/#run",
  },
  build: {
    id: "build",
    label: "Building on Iki",
    who: "Plugin authors and coding agents",
    does: "Add types, actions and views as a plugin: YAML manifests in a git repo.",
    all: "/guides/#build",
  },
};

/** An item of a list: a site page (`href` from `/`), a repository document on GitHub
 * (`source`), or planned (neither). */
export interface Item {
  label: string;
  href?: string;
  /** A file in the repository that covers it until its page is written. */
  source?: string;
  /** A short qualifier after it: "from OpenAPI". */
  note?: string;
}

/** A link as a page's frontmatter writes it: a site path, or `repo:<path>` for a file in the
 * repository, which links to GitHub. */
export function itemOf(link: { label: string; href: string; note?: string | undefined }): Item {
  const { label, href, note } = link;
  const item: Item = href.startsWith("repo:")
    ? { label, source: href.slice("repo:".length) }
    : { label, href };
  return note ? { ...item, note } : item;
}

export function hrefOf(item: Item): string | null {
  if (item.href) return item.href;
  if (item.source) return github(item.source);
  return null;
}

export const TOP_NAV = [
  { label: "Guides", href: "/guides/" },
  { label: "Use cases", href: "/use-cases/" },
  { label: "Concepts", href: "/concepts/" },
  { label: "Reference", href: "/reference/" },
  { label: "Decisions", href: "/decisions/" },
] as const;

export type Section = (typeof TOP_NAV)[number]["label"];

export const PAGES = {
  doseResponse: "/use-cases/first-dose-response-assay/",
  prepVerification: "/concepts/prep-verification/",
  api: "/reference/api/",
  signatures: "/reference/api/get-entities-ref-signatures/",
} as const;

/** What each door lists on the home page: tasks first. */
export const DOORS: Record<Audience, Item[]> = {
  bench: [
    { label: "Run a first dose-response plate assay", href: PAGES.doseResponse },
    { label: "Check a plasmid prep against its sequence", href: PAGES.prepVerification },
    { label: "Sign a result, and check who signed it" },
    { label: "Import a spreadsheet of records" },
    { label: "Receive tubes against a request", source: "plugins/example_lims/README.md" },
  ],
  run: [
    { label: "Run Iki on a laptop with kind", source: "deploy/dev/README.md" },
    { label: "Deploy on Google Cloud", source: "deploy/gcp/README.md" },
    { label: "Deploy on AWS", source: "deploy/aws/README.md" },
    {
      label: "Add a plugin through the config repository",
      source: "deploy/config-template/README.md",
    },
    { label: "Restore from a backup", source: "docs/disaster-recovery.md" },
  ],
  build: [
    { label: "Start a plugin from the template", source: "plugin-template/README.md" },
    {
      label: "Validate and test a plugin in CI",
      source: "plugin-template/.agents/skills/iki-test-plugin/SKILL.md",
    },
    {
      label: "Upgrade a plugin without losing history",
      source: "plugin-template/.agents/skills/iki-upgrade-plugin/SKILL.md",
    },
    { label: "Give your coding agent the plugin skills", source: "plugin-template/README.md" },
    { label: "Look up a REST API call", href: PAGES.api },
  ],
};

/** The ideas behind Iki: each concept's page, or the decision that sets it out today. */
export const CONCEPTS: Item[] = [
  {
    label: "Records, revisions and provenance",
    href: "/decisions/0007-typed-table-per-type-entity-storage/",
    note: "ADR 0007",
  },
  {
    label: "Registries and project records",
    href: "/decisions/0115-registries-keep-identities-projects-keep-records/",
    note: "ADR 0115",
  },
  {
    label: "States, locks and signatures",
    href: "/decisions/0125-a-state-is-a-field-each-state-says-who-sets-it/",
    note: "ADR 0125",
  },
  { label: "Prep verification", href: PAGES.prepVerification },
  {
    label: "Plugins, types and actions",
    href: "/decisions/0006-deploy-time-plugins-as-django-apps/",
    note: "ADR 0006",
  },
  {
    label: "Workers and compute tiers",
    href: "/decisions/0009-compute-tiers-and-templates/",
    note: "ADR 0009",
  },
  {
    label: "Review flows: Research and Regulated QC",
    href: "/decisions/0198-a-project-applies-a-versioned-assay-starter/",
    note: "ADR 0198",
  },
  {
    label: "Agents through MCP",
    href: "/decisions/0013-agents-via-mcp-oauth-approvals/",
    note: "ADR 0013",
  },
];

/** The references, generated from what they describe. */
export const REFERENCES: Item[] = [
  { label: "REST API", href: PAGES.api, note: "from OpenAPI" },
  { label: "Plugin spec, iki/v1", source: "spec/README.md", note: "from the JSON Schemas" },
  { label: "The iki CLI", source: "cli/README.md", note: "plugin, bundle, deploy, starter" },
  { label: "Python SDK and MCP server", source: "sdk/python/README.md" },
];

/** The bench's left-hand navigation on its concept pages. */
export const BENCH_NAV: { heading: string; items: Item[] }[] = [
  {
    heading: "Do it",
    items: [
      { label: "Run a first dose-response assay", href: PAGES.doseResponse },
      { label: "Check a plasmid prep", source: "plugins/plasmid_sequencing/README.md" },
      { label: "Record a prep's verification", source: "plugins/lots/README.md" },
    ],
  },
  {
    heading: "Understand it",
    items: [
      {
        label: "Lots and preps",
        href: "/decisions/0162-a-lot-is-a-projects-record-of-an-entrys-material/",
      },
      { label: "Prep verification", href: PAGES.prepVerification },
      {
        label: "States, locks and signatures",
        href: "/decisions/0126-an-electronic-signature-is-bound-to-the-revision-it-signs/",
      },
      { label: "Corrections", href: "/decisions/0179-a-corrected-result-goes-back-for-review/" },
    ],
  },
];

export const FOOTER: Item[] = [
  { label: "Contributing", source: "CONTRIBUTING.md" },
  { label: "Security", source: "SECURITY.md" },
  { label: "Code of Conduct", source: "CODE_OF_CONDUCT.md" },
  { label: "Roadmap to M1", source: "docs/roadmap.md" },
];
