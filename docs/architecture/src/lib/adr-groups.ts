// Every ADR in exactly one subsystem, by what it decides. This is the one hand-kept list on
// the ADR pages: the build fails if an ADR is missing from it, listed twice, or unknown,
// so a new ADR has to be placed before the site builds.
import { adrs } from "./adrs.ts";

export interface AdrGroup {
  id: string;
  name: string;
  /** The page, or group of pages, that covers it, by its navigation name, when there is one. */
  section?: string;
  numbers: number[];
}

export const ADR_GROUPS: AdrGroup[] = [
  {
    id: "foundations",
    name: "Foundations",
    section: "System map",
    numbers: [1, 2, 4, 5, 18, 19, 20, 21, 111],
  },
  {
    id: "records",
    name: "Records",
    section: "Records",
    numbers: [
      7, 8, 25, 35, 38, 82, 104, 106, 118, 119, 120, 122, 124, 125, 126, 127, 128, 131, 136, 137,
      148, 164, 165, 179,
    ],
  },
  {
    id: "registries",
    name: "Registries",
    section: "Identity and HRIDs",
    numbers: [115, 138, 143, 144, 145, 154],
  },
  {
    id: "access",
    name: "Permissions and sign-in",
    section: "Permissions",
    numbers: [3, 13, 23, 24, 26, 28, 67, 72, 73, 101, 116, 151, 152, 153, 210],
  },
  {
    id: "api",
    name: "API and clients",
    section: "Server and APIs",
    numbers: [27, 49, 51],
  },
  {
    id: "runs",
    name: "Runs and compute",
    section: "Runs",
    numbers: [
      9, 10, 40, 41, 42, 43, 44, 45, 46, 47, 48, 50, 53, 54, 55, 57, 58, 59, 60, 61, 62, 64, 66, 68,
      69, 70, 71, 74, 75, 76, 77, 78, 83, 105, 108, 112, 130, 133, 141, 155, 156, 160, 167, 169,
    ],
  },
  {
    id: "workflows",
    name: "Workflows and triggers",
    section: "Workflows and triggers",
    numbers: [11, 79, 80, 81],
  },
  {
    id: "plugins",
    name: "Plugins",
    section: "Plugins",
    numbers: [6, 16, 32, 33, 34, 39, 65, 91, 92, 93, 94, 95, 113, 114, 121, 134, 158, 181, 211],
  },
  {
    id: "events",
    name: "Events and notifications",
    section: "What events disclose",
    numbers: [30, 31, 129, 159],
  },
  {
    id: "files",
    name: "Files and imports",
    section: "Data",
    numbers: [36, 52, 56, 89, 177, 178],
  },
  {
    id: "requests",
    name: "Requests and release",
    section: "Requests",
    numbers: [132, 142, 149, 195, 198],
  },
  {
    id: "storage",
    name: "Storage and amounts",
    section: "Material",
    numbers: [139, 180, 190, 193, 207],
  },
  {
    id: "search",
    name: "Search and exports",
    section: "Finding data",
    numbers: [37, 96, 123, 135, 171, 175],
  },
  {
    id: "web",
    name: "Web app and viewers",
    section: "Web app",
    numbers: [12, 29, 84, 85, 86, 87, 90, 102, 103, 110, 117, 170, 174, 176, 191, 199, 202],
  },
  {
    id: "deployment",
    name: "Deployment and supply chain",
    section: "Deployment",
    numbers: [14, 15, 17, 22, 63, 97, 98, 99, 100, 107, 109, 209],
  },
  {
    id: "science",
    name: "Scientific models",
    numbers: [
      88, 140, 146, 147, 157, 161, 162, 163, 166, 168, 172, 173, 182, 183, 184, 185, 186, 187, 188,
      189, 192, 194, 196, 197, 200, 201, 203, 204, 205, 206, 208,
    ],
  },
];

const pad = (n: number): string => String(n).padStart(4, "0");

/** The group each ADR is in, checked to cover every ADR exactly once. */
export function groupOf(): Map<string, AdrGroup> {
  const all = adrs();
  const seen = new Map<string, AdrGroup>();
  for (const g of ADR_GROUPS) {
    for (const n of g.numbers) {
      const key = pad(n);
      if (!all.has(key)) throw new Error(`The ADR groups list ADR ${key}, which doesn't exist.`);
      const other = seen.get(key);
      if (other) throw new Error(`ADR ${key} is in both "${other.name}" and "${g.name}".`);
      seen.set(key, g);
    }
  }
  const missing = [...all.keys()].filter((k) => !seen.has(k));
  if (missing.length) {
    throw new Error(
      `ADRs ${missing.join(", ")} aren't in a subsystem: add them to src/lib/adr-groups.ts.`,
    );
  }
  return seen;
}
