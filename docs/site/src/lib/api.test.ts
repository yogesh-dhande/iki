import { describe, expect, it } from "vitest";

import { endpointPath, schemaPath, shortPath } from "./api.ts";
import { callKey } from "./calls.ts";

describe("the reference's paths", () => {
  it("drops the version every path starts with, and names pages", () => {
    expect(shortPath("/api/v1/entities/{ref}")).toBe("/entities/{ref}");
    expect(endpointPath("get-entities")).toBe("/reference/api/get-entities/");
    expect(schemaPath("SignaturesOut")).toBe("/reference/api/schemas/SignaturesOut/");
  });

  it("keys a call as docs/sitegen/calls.py does", () => {
    expect(callKey("get", "/api/v1/entities/{ref}/signatures")).toBe("GET /entities/{}/signatures");
  });
});
