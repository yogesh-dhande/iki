// The script's JSON Schema, from the Zod schema, for editors' autocompletion and checks.
import { z } from "zod";
import { Video } from "./schema.ts";

export function scriptJsonSchema(): string {
  const schema = z.toJSONSchema(Video, { io: "input", target: "draft-2020-12" });
  return `${JSON.stringify({ ...schema, title: "An Iki video's script" }, null, 2)}\n`;
}
