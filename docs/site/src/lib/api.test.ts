import { describe, expect, it } from "vitest";

import { endpointPath, mentions, schemaPath, shortPath } from "./api.ts";
import { callKey } from "./calls.ts";

describe("which decisions name a call", () => {
  const path = "/api/v1/entities/{ref}/signatures";

  it("finds the path with or without /api/v1, its parameters named anything", () => {
    expect(mentions("Read them with `GET /entities/{ref}/signatures`.", "get", path)).toBe(true);
    expect(mentions("GET /api/v1/entities/{id}/signatures lists them", "get", path)).toBe(true);
    expect(mentions("the list, /entities/{ref}/signatures, shows", "get", path)).toBe(true);
  });

  it("not another method's, nor a longer path's", () => {
    expect(mentions("Starting one (`POST\n/entities/{ref}/signatures`)", "get", path)).toBe(false);
    expect(mentions("GET /entities/{ref}/signatures/{n}", "get", path)).toBe(false);
    expect(mentions("GET /admin/entities/{ref}/signatures", "get", path)).toBe(false);
  });
});

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
