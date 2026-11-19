// Reads a script: YAML, checked against the schema and the rules. Used by the runtime
// (in the browser, from the file's text) and by the tools (in Node).
import { parse } from "yaml";
import { checkRules } from "./rules.ts";
import { Video } from "./schema.ts";

export class ScriptError extends Error {
  readonly file: string;
  readonly problems: string[];

  constructor(file: string, problems: string[]) {
    super(`${file}:\n${problems.map((problem) => `  - ${problem}`).join("\n")}`);
    this.file = file;
    this.problems = problems;
  }
}

/** The script, or a ScriptError listing every problem with it. */
export function loadScript(source: string, file: string): Video {
  const parsed = Video.safeParse(parse(source));
  if (!parsed.success) {
    throw new ScriptError(
      file,
      parsed.error.issues.map((issue) => `${issue.path.join(".") || "(script)"}: ${issue.message}`),
    );
  }
  const problems = checkRules(parsed.data);
  if (problems.length > 0) throw new ScriptError(file, problems);
  return parsed.data;
}
