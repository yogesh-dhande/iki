import { describe, expect, it } from "vitest";

import { ApiError, createIkiClient, encodeName, isProblem, unwrap } from "./index.js";

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

  it("keeps the / of a name's namespace in its path, and escapes everything else", async () => {
    const { requests, fetch } = recorder({});
    const iki = createIkiClient({ baseUrl: BASE, fetch });
    await iki.GET("/api/v1/entity-types/{name}", {
      params: { path: { name: "iki/protein.Protein" } },
    });
    await iki.GET("/api/v1/actions/{name}", { params: { path: { name: "../runs" } } });
    await iki.GET("/api/v1/entities/{ref}", { params: { path: { ref: "a/b" } } });
    expect(requests.map((r) => r.url)).toEqual([
      `${BASE}/api/v1/entity-types/iki/protein.Protein`,
      `${BASE}/api/v1/actions/..%2Fruns`,
      `${BASE}/api/v1/entities/a%2Fb`,
    ]);
  });

  it("lists your work by project, type and state, each repeated", async () => {
    const { requests, fetch } = recorder({ items: [], next_cursor: null });
    const iki = createIkiClient({ baseUrl: BASE, fetch });
    const project = "01a1c33f-ee39-74dc-a04f-4b57bb940194";
    await iki.GET("/api/v1/me/work", {
      params: { query: { project: [project], type: ["a/b.S"], state: ["x", "y"], sort: "due" } },
    });
    expect(requests[0]?.url).toBe(
      `${BASE}/api/v1/me/work?project=${project}&type=a%2Fb.S&state=x&state=y&sort=due`,
    );
  });

  it("reads a record's signatures and verifies one (ADR 0126)", async () => {
    const { requests, fetch } = recorder({ valid: true, checks: [] });
    const iki = createIkiClient({ baseUrl: BASE, fetch });
    await iki.GET("/api/v1/entities/{ref}/signatures", {
      params: { path: { ref: "LOT-000001" }, query: { verify: true } },
    });
    const checked = unwrap(
      await iki.GET("/api/v1/signatures/{signature_id}/verification", {
        params: { path: { signature_id: "01a1ee10-0000-7000-8000-000000000001" } },
      }),
    );
    expect(checked.valid).toBe(true);
    expect(requests.map((r) => r.url)).toEqual([
      `${BASE}/api/v1/entities/LOT-000001/signatures?verify=true`,
      `${BASE}/api/v1/signatures/01a1ee10-0000-7000-8000-000000000001/verification`,
    ]);
  });
});

describe("encodeName", () => {
  it("keeps one / between non-empty segments", () => {
    expect(encodeName("iki/protein.Protein")).toBe("iki/protein.Protein");
    expect(encodeName("acme/lab.p q")).toBe("acme/lab.p%20q");
    expect(encodeName("a/b/c")).toBe("a%2Fb%2Fc");
    expect(encodeName("/x")).toBe("%2Fx");
    expect(encodeName("../x")).toBe("..%2Fx");
    expect(encodeName("iki/protein.Protein")).toBe("iki/protein.Protein");
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

  it("gives what a launch sends outside when it needs consent (ADR 0130)", async () => {
    const consent = {
      api: "iki/example_models.foldcloud",
      label: "FoldCloud",
      host: "fold.example.com",
      purpose: "Predicts protein structures.",
      inputs: { protein: ["sequence"] },
      params: [],
      statement: "This sends your data to FoldCloud (fold.example.com): each Protein's sequence.",
    };
    const { requests, fetch } = recorder(
      { ...PROBLEM, code: "consent_required", detail: consent.statement, consent: [consent] },
      409,
    );
    const iki = createIkiClient({ baseUrl: BASE, fetch });
    const body = {
      project: "01a1ee10-0000-7000-8000-000000000001",
      add_to_project: false,
      reason: "",
      reuse: true,
      consent: [],
    };
    const call = iki
      .POST("/api/v1/actions/{name}/runs", {
        params: { path: { name: "iki/example_models.predict_structure" } },
        body,
      })
      .then(unwrap);
    await expect(call).rejects.toMatchObject({
      code: "consent_required",
      problem: { consent: [consent] },
    });
    expect(await requests[0]?.json()).toEqual(body);
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
