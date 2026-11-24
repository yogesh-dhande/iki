import { describe, expect, it } from "vitest";

import { api } from "./api.ts";
import {
  curl,
  endpoints,
  exampleOf,
  fieldsOf,
  schemaUses,
  slugOf,
  tags,
  typeOf,
  type Document,
} from "./openapi.ts";

const doc: Document = {
  openapi: "3.1.0",
  info: { title: "Test", version: "1" },
  paths: {
    "/api/v1/entities/{ref}/signatures": {
      get: {
        operationId: "list_signatures",
        summary: "A record's signatures",
        tags: ["signatures"],
        parameters: [
          { name: "ref", in: "path", required: true, schema: { type: "string" } },
          {
            name: "verify",
            in: "query",
            schema: { type: "boolean", default: false, description: "Check each again." },
          },
        ],
        responses: {
          "200": {
            description: "OK",
            content: { "application/json": { schema: { $ref: "#/components/schemas/Out" } } },
          },
        },
        security: [{ BearerAuth: [] }, { SessionAuth: [] }],
      },
    },
    "/api/v1/nodes": {
      post: {
        operationId: "make_node",
        tags: ["graph"],
        requestBody: {
          required: true,
          content: { "application/json": { schema: { $ref: "#/components/schemas/Node" } } },
        },
        responses: { "201": { description: "Made" } },
      },
    },
  },
  components: {
    schemas: {
      Out: {
        type: "object",
        description: "Every signature.",
        required: ["items"],
        properties: {
          items: { type: "array", items: { $ref: "#/components/schemas/Signature" } },
          missing: { type: "array", items: { type: "string" } },
        },
      },
      Signature: {
        type: "object",
        required: ["id", "status"],
        properties: {
          id: { type: "string", format: "uuid" },
          status: { type: "string", enum: ["carried", "pending"] },
          signer: { anyOf: [{ $ref: "#/components/schemas/Person" }, { type: "null" }] },
          tags: { type: "object", additionalProperties: { type: "integer" } },
          count: { type: "integer", minimum: 1, maximum: 9 },
        },
      },
      Person: {
        type: "object",
        description: "Someone.",
        properties: { name: { type: "string", maxLength: 80 } },
      },
      Node: {
        type: "object",
        properties: {
          name: { type: "string" },
          child: { anyOf: [{ $ref: "#/components/schemas/Node" }, { type: "null" }] },
        },
      },
    },
  },
};

describe("slugOf", () => {
  it("names a call's page by its method and its path's words", () => {
    expect(slugOf("get", "/api/v1/entities/{ref}/signatures")).toBe("get-entities-ref-signatures");
    expect(slugOf("post", "/api/v1/signatures/{id}/withdrawal")).toBe(
      "post-signatures-id-withdrawal",
    );
  });
});

describe("typeOf", () => {
  it("reads types as a reader would", () => {
    expect(typeOf({ type: "string", format: "uuid" }).text).toBe("uuid");
    expect(typeOf({ anyOf: [{ type: "integer" }, { type: "null" }] }).text).toBe("integer | null");
    expect(typeOf({ type: ["string", "null"] }).text).toBe("string | null");
    expect(typeOf({ type: "array", items: { $ref: "#/components/schemas/Signature" } })).toEqual({
      text: "Signature[]",
      schemas: ["Signature"],
    });
    expect(
      typeOf({
        type: "array",
        items: { anyOf: [{ type: "string" }, { $ref: "#/components/schemas/Person" }] },
      }).text,
    ).toBe("(string | Person)[]");
    expect(typeOf({ type: "object", additionalProperties: { type: "integer" } }).text).toBe(
      "map of integer",
    );
    expect(typeOf({ type: "string", enum: ["a"] }).text).toBe("enum");
  });

  it("refuses a reference outside the document's schemas", () => {
    expect(() => typeOf({ $ref: "https://example.com/schema.json" })).toThrow(/component/);
  });
});

describe("fieldsOf", () => {
  it("lists a schema's fields, and the fields of what each holds beneath it", () => {
    const fields = fieldsOf({ $ref: "#/components/schemas/Out" }, doc);
    expect(fields.map((f) => [f.name, f.depth, f.required])).toEqual([
      ["items", 0, true],
      ["id", 1, true],
      ["status", 1, true],
      ["signer", 1, false],
      ["tags", 1, false],
      ["count", 1, false],
      ["missing", 0, false],
    ]);
    const status = fields.find((f) => f.name === "status");
    expect(status?.values).toEqual(["carried", "pending"]);
    expect(fields.find((f) => f.name === "count")?.notes).toEqual(["at least 1", "at most 9"]);
    // A field's description falls back to its schema's.
    expect(fields.find((f) => f.name === "signer")?.description).toBe("Someone.");
  });

  it("stops at a schema it's already inside", () => {
    const fields = fieldsOf({ $ref: "#/components/schemas/Node" }, doc, 5);
    expect(fields.map((f) => `${f.name}@${String(f.depth)}`)).toEqual(["name@0", "child@0"]);
  });
});

describe("endpoints", () => {
  it("reads each call: parameters, body, responses and who may call it", () => {
    const [list, make] = endpoints(doc);
    expect(list?.slug).toBe("get-entities-ref-signatures");
    expect(list?.parameters.map((p) => [p.name, p.in, p.required, p.notes])).toEqual([
      ["ref", "path", true, []],
      ["verify", "query", false, ["default false"]],
    ]);
    expect(list?.parameters[1]?.description).toBe("Check each again.");
    expect(list?.responses[0]?.schema).toBe("Out");
    expect(list?.security).toEqual(["Bearer token", "Web app session"]);
    expect(make?.summary).toBe("POST /api/v1/nodes");
    expect(make?.body?.required).toBe(true);
    expect(make?.responses[0]?.type).toBeNull();
  });

  it("refuses two calls that would share a page", () => {
    const twice: Document = {
      ...doc,
      paths: {
        "/api/v1/a/b": { get: { operationId: "one", responses: {} } },
        "/api/v1/a-b": { get: { operationId: "two", responses: {} } },
      },
    };
    expect(() => endpoints(twice)).toThrow(/would share a page/);
  });

  it("groups calls by tag, the tags by name", () => {
    expect(tags(doc).map((t) => [t.label, t.slug, t.endpoints.length])).toEqual([
      ["Graph", "graph", 1],
      ["Signatures", "signatures", 1],
    ]);
  });
});

describe("examples", () => {
  it("fills a schema with one lab's values, and never loops", () => {
    const out = exampleOf({ $ref: "#/components/schemas/Out" }, doc) as {
      items: { id: string; status: string; signer: { name: string }; count: number }[];
    };
    const [first] = out.items;
    expect(first?.id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
    expect(first?.status).toBe("carried");
    // A person's name where the schema is a person's.
    expect(first?.signer.name).toBe("Amara Okafor");
    expect(first?.count).toBe(8);
    // The same schema gives the same values, page after page.
    expect(exampleOf({ $ref: "#/components/schemas/Out" }, doc)).toEqual(out);
    expect(exampleOf({ $ref: "#/components/schemas/Node" }, doc)).toEqual({
      name: "QC plate assays",
      child: "<Node>",
    });
  });

  it("writes a curl call with the token, and a body when there is one", () => {
    const [list, make] = endpoints(doc);
    if (!list || !make) throw new Error("no endpoints");
    expect(curl(list)).toBe(
      'curl -H "Authorization: Bearer $IKI_TOKEN" \\\n  "$IKI_URL/api/v1/entities/QSF-000014/signatures"',
    );
    expect(curl(list, { ref: "VER-1", verify: "true" })).toContain(
      "/api/v1/entities/VER-1/signatures?verify=true",
    );
    expect(curl(make)).toContain('curl -X POST -H "Authorization: Bearer $IKI_TOKEN"');
    expect(curl(make)).toContain("-d @body.json");
  });
});

describe("schemaUses", () => {
  it("finds every call a schema reaches, however deep", () => {
    const uses = schemaUses(doc, endpoints(doc));
    expect(uses.get("Person")?.map((e) => e.slug)).toEqual(["get-entities-ref-signatures"]);
    expect(uses.get("Node")?.map((e) => e.slug)).toEqual(["post-nodes"]);
  });
});

describe("the server's own document", () => {
  it("gives every call a page, and reads the signatures call as the reference shows it", () => {
    const { all, doc: real } = api();
    expect(all.length).toBeGreaterThan(250);
    expect(new Set(all.map((e) => e.slug)).size).toBe(all.length);
    const signatures = all.find((e) => e.slug === "get-entities-ref-signatures");
    expect(signatures?.summary).toBe("A record's electronic signatures");
    expect(signatures?.responses[0]?.schema).toBe("SignaturesOut");
    expect(signatures?.responses[0]?.fields.map((f) => f.name)).toContain("missing");
    // Every named schema can be shown as an example without looping.
    for (const name of Object.keys(real.components.schemas)) {
      expect(() => exampleOf({ $ref: `#/components/schemas/${name}` }, real)).not.toThrow();
    }
  });
});
