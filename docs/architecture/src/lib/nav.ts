// The site's pages, in reading order. A page's title and breadcrumb come from here, and its
// URL is its label's slug, under its group's slug when the group has `nested`: so a page
// has one name everywhere it's shown.
export interface NavItem {
  label: string;
  href: string;
}
export interface NavGroup {
  heading: string;
  /** Pages live under /<heading's slug>/. */
  nested?: boolean;
  items: NavItem[];
}

export const slug = (s: string): string =>
  s
    .toLowerCase()
    .replace(/['’]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");

/** A group whose pages nest under its own slug, built from their labels. */
function nested(heading: string, labels: string[]): NavGroup {
  return {
    heading,
    nested: true,
    items: labels.map((label) => ({ label, href: `/${slug(heading)}/${slug(label)}/` })),
  };
}

export const NAV: NavGroup[] = [
  {
    heading: "Overview",
    items: [
      { label: "System map", href: "/" },
      { label: "Modules", href: "/modules/" },
    ],
  },
  nested("Platform", ["Server and APIs", "Tasks and live updates", "WebAssembly"]),
  nested("Records", [
    "A record's life",
    "Types and tables",
    "Identity and HRIDs",
    "Saving and validation",
    "Revisions and history",
    "States and guards",
    "Locks and corrections",
    "Electronic signatures",
    "Links between records",
    "Audit trail",
  ]),
  nested("Data", [
    "How Iki handles data",
    "Formats",
    "Where data is stored",
    "How data links to records",
    "Finding data",
    "Versioning",
    "Exports",
  ]),
  nested("Runs", [
    "A run's life",
    "Launching a run",
    "The worker and its leases",
    "Compute tiers",
    "Pinning and provenance",
    "Network and secrets",
    "Quotas and cost",
    "Workflows and triggers",
  ]),
  nested("Permissions", [
    "How access is checked",
    "Sign-in and identity",
    "Projects and roles",
    "Tokens and delegation",
    "Sharing across projects",
    "Unreadable references",
    "What events disclose",
  ]),
  nested("Plugins", [
    "A plugin's life",
    "What a plugin declares",
    "Builds and deploys",
    "Migrations and versions",
  ]),
  nested("Plugin browser", ["How the pieces fit", "All plugins", "Core types", "Schemas"]),
  nested("Requests", [
    "A request's life",
    "Asking and serving",
    "Request and deliverable types",
    "Release and completion",
  ]),
  nested("Material", [
    "How material is tracked",
    "Amounts and the ledger",
    "Draws and samples",
    "Places and moves",
    "Receipts and counts",
  ]),
  nested("Web app", [
    "The app's parts",
    "Pages and routing",
    "Reading data",
    "Pages that hold still",
    "Plugin frames",
    "Packages and the SDK",
    "Serving and security policy",
  ]),
  nested("Testing", [
    "Where a change is tested",
    "Gates and CI",
    "Browser tests",
    "Plugin tests",
    "Live clouds and scale",
  ]),
  nested("Deployment", [
    "From release to cluster",
    "Releases and images",
    "The Helm chart",
    "Cloud infrastructure",
    "Licensed images",
    "Dependencies",
    "The dev cluster",
  ]),
  {
    heading: "Reference",
    items: [
      { label: "ADRs", href: "/adrs/" },
      { label: "Routes", href: "/routes/" },
    ],
  },
];

export const PAGES: (NavItem & { group: string })[] = NAV.flatMap((g) =>
  g.items.map((i) => ({ ...i, group: g.heading })),
);

for (const g of NAV) {
  for (const item of g.items) {
    const want = g.nested ? `/${slug(g.heading)}/${slug(item.label)}/` : `/${slug(item.label)}/`;
    if (item.href !== "/" && item.href !== want) {
      throw new Error(`The page "${item.label}" is at ${item.href}: its URL should be ${want}.`);
    }
  }
}
const labels = new Set<string>();
for (const p of PAGES) {
  if (labels.has(p.label)) throw new Error(`Two pages are called "${p.label}".`);
  labels.add(p.label);
}

/** A page by its label, for links that name a page: the build fails on an unknown one. */
export function page(label: string): NavItem & { group: string } {
  const p = PAGES.find((x) => x.label === label);
  if (!p) throw new Error(`There's no page called "${label}" in the navigation.`);
  return p;
}

/** The page a path belongs to, with its group: its one name and its breadcrumb. A path
 * under a page, such as one ADR under /adrs/, belongs to the longest page it starts with. */
export function section(pathname: string): (NavItem & { group: string }) | undefined {
  const exact = PAGES.find((p) => p.href === pathname);
  if (exact) return exact;
  return PAGES.filter((p) => p.href !== "/" && pathname.startsWith(p.href)).sort(
    (a, b) => b.href.length - a.href.length,
  )[0];
}
