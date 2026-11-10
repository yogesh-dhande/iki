import { describe, expect, it } from "vitest";

import { ApiError, createIkiClient, isProblem, unwrap } from "./index.js";

function recorder(body: unknown, status = 200) {
  const requests: Request[] = [];
  const fetch = (input: Request): Promise<Response> => {
    requests.push(input);
    const type = status >= 400 ? "application/problem+json" : "application/json";
    return Promise.resolve(
      new Response(status === 204 ? null : JSON.stringify(body), {
        status,
        headers: { "Content-Type": type },
      }),
    );
  };
  return { requests, fetch: fetch as typeof globalThis.fetch };
}

// Node has no page origin to resolve relative URLs against.
const BASE = "https://iki.test";

const PROBLEM = {
  type: "about:blank",
  title: "Conflict",
  status: 409,
  detail: "A project already has that name.",
  code: "refused",
};

describe("createIkiClient", () => {
  it("sends the CSRF token on unsafe methods only, with the session", async () => {
    const { requests, fetch } = recorder({ items: [], next_cursor: null });
    const iki = createIkiClient({ baseUrl: "https://iki.test", csrfToken: () => "tok", fetch });
    await iki.GET("/api/v1/projects");
    await iki.POST("/api/v1/projects", {
      body: { team: "01a1c33f-ee39-74dc-a04f-4b57bb940194", name: "Atlas", description: "" },
    });
    expect(requests.map((r) => [r.method, r.headers.get("X-CSRFToken")])).toEqual([
      ["GET", null],
      ["POST", "tok"],
    ]);
    expect(requests[0]?.credentials).toBe("same-origin");
    expect(requests[0]?.url).toBe("https://iki.test/api/v1/projects");
  });

  it("sends an access token, and no cookies, for programs", async () => {
    const { requests, fetch } = recorder({});
    const iki = createIkiClient({
      baseUrl: BASE,
      accessToken: () => Promise.resolve("at-123"),
      fetch,
    });
    await iki.GET("/api/v1/me");
    expect(requests[0]?.headers.get("Authorization")).toBe("Bearer at-123");
    expect(requests[0]?.credentials).toBe("omit");
  });
});

describe("unwrap", () => {
  it("returns the data of a successful response", async () => {
    const { fetch } = recorder({ id: "x" });
    const iki = createIkiClient({ baseUrl: BASE, fetch });
    const me = unwrap(await iki.GET("/api/v1/me"));
    expect(me).toEqual({ id: "x" });
  });

  it("throws the problem as an ApiError", async () => {
    const { fetch } = recorder(PROBLEM, 409);
    const iki = createIkiClient({ baseUrl: BASE, fetch });
    const call = iki.GET("/api/v1/me").then(unwrap);
    await expect(call).rejects.toBeInstanceOf(ApiError);
    await expect(call).rejects.toMatchObject({ status: 409, code: "refused" });
  });

  it("describes responses that aren't problems", () => {
    const response = new Response("<html>", { status: 502, statusText: "Bad Gateway" });
    expect(() => unwrap({ error: "<html>", response })).toThrow("HTTP 502");
  });
});

describe("isProblem", () => {
  it("recognises problems", () => {
    expect(isProblem(PROBLEM)).toBe(true);
    expect(isProblem({ status: 500 })).toBe(false);
    expect(isProblem(null)).toBe(false);
  });
});
