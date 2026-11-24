// The REST API's routes, from the OpenAPI documents `make client` and `make protocol`
// write. Each route is traced to the function that serves it through its operationId,
// which ninja builds from the module and function name.
import { readRepo, filesUnder } from "./repo.ts";

export interface Route {
  method: string;
  path: string;
  tag: string;
  summary: string;
  description: string;
  operationId: string;
  /** The module and function serving it, when the operationId names one that exists. */
  source?: { file: string; func: string };
}

interface Operation {
  tags?: string[];
  summary?: string;
  description?: string;
  operationId?: string;
}

const METHODS = ["get", "post", "put", "patch", "delete"];

function sourceOf(operationId: string, pyFiles: Map<string, string>): Route["source"] {
  // ninja names an operation <module path>_<function>, all joined by underscores:
  // iki_server_api_service_accounts_create_service_account. Try each split of the rest.
  const prefix = "iki_server_api_";
  if (!operationId.startsWith(prefix)) return undefined;
  const parts = operationId.slice(prefix.length).split("_");
  for (let j = 1; j < parts.length; j++) {
    const file = `server/src/iki_server/api/${parts.slice(0, j).join("_")}.py`;
    const func = parts.slice(j).join("_");
    const text = pyFiles.get(file);
    if (text && new RegExp(`def ${func}\\(`).test(text)) return { file, func };
  }
  return undefined;
}

export function routes(document: "api" | "worker"): Route[] {
  const file =
    document === "api"
      ? "web/packages/sdk/openapi.json"
      : "worker/crates/iki-protocol/worker-openapi.json";
  const doc = JSON.parse(readRepo(file)) as {
    paths: Record<string, Partial<Record<string, Operation>>>;
  };
  const pyFiles = new Map<string, string>();
  for (const f of filesUnder("server/src/iki_server/api")) {
    if (f.endsWith(".py")) pyFiles.set(f, readRepo(f));
  }
  const out: Route[] = [];
  for (const [path, ops] of Object.entries(doc.paths)) {
    for (const method of METHODS) {
      const op = ops[method];
      if (!op) continue;
      const operationId = op.operationId ?? "";
      const route: Route = {
        method: method.toUpperCase(),
        path,
        tag: op.tags?.[0] ?? "other",
        summary: op.summary ?? "",
        description: (op.description ?? "").split("\n")[0] ?? "",
        operationId,
      };
      const source = operationId ? sourceOf(operationId, pyFiles) : undefined;
      if (source) route.source = source;
      out.push(route);
    }
  }
  return out;
}
