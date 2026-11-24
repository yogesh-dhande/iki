// The site's sections, in reading order. A page's title and breadcrumb come from here, and
// each section's URL is its name, so a section has one name everywhere it's shown.
export interface NavItem {
  label: string;
  href: string;
}
export interface NavGroup {
  heading: string;
  items: NavItem[];
}

export const NAV: NavGroup[] = [
  {
    heading: "Overview",
    items: [
      { label: "System map", href: "/" },
      { label: "Modules", href: "/modules/" },
    ],
  },
  {
    heading: "Core",
    items: [
      { label: "Records", href: "/records/" },
      { label: "Runs", href: "/runs/" },
      { label: "Plugins", href: "/plugins/" },
      { label: "Permissions", href: "/permissions/" },
    ],
  },
  {
    heading: "Lab data",
    items: [
      { label: "Requests", href: "/requests/" },
      { label: "Storage", href: "/storage/" },
      { label: "Search and exports", href: "/search-and-exports/" },
    ],
  },
  {
    heading: "Engineering",
    items: [
      { label: "Web app", href: "/web-app/" },
      { label: "Testing", href: "/testing/" },
      { label: "Deployment", href: "/deployment/" },
    ],
  },
  {
    heading: "Reference",
    items: [
      { label: "ADRs", href: "/adrs/" },
      { label: "Routes", href: "/routes/" },
      { label: "Core types", href: "/core-types/" },
      { label: "First-party plugins", href: "/first-party-plugins/" },
    ],
  },
];

const slug = (s: string): string => s.toLowerCase().replace(/[^a-z0-9]+/g, "-");
for (const item of NAV.flatMap((g) => g.items)) {
  if (item.href !== "/" && item.href !== `/${slug(item.label)}/`) {
    throw new Error(`The section "${item.label}" is at ${item.href}: its URL should be its name.`);
  }
}

/** The section a path belongs to, with its group: its one name and its breadcrumb. */
export function section(
  pathname: string,
): { label: string; group: string; href: string } | undefined {
  for (const g of NAV) {
    for (const item of g.items) {
      if (pathname === item.href || (item.href !== "/" && pathname.startsWith(item.href))) {
        return { ...item, group: g.heading };
      }
    }
  }
  return undefined;
}
