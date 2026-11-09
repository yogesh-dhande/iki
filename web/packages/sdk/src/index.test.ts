import { describe, expect, it } from "vitest";

import { API_VERSION, apiPath } from "./index.js";

describe("apiPath", () => {
  it("prefixes the resource with the API version", () => {
    expect(apiPath("projects")).toBe(`/api/${API_VERSION}/projects`);
  });

  it("strips leading and trailing slashes", () => {
    expect(apiPath("/entities/types/")).toBe("/api/v1/entities/types");
  });

  it("rejects an empty resource", () => {
    expect(() => apiPath("//")).toThrow("resource must not be empty");
  });
});
