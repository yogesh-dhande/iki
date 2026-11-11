import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { answer, calls, respond } from "./test-server";
import { sha256Of, timing, upload, uploadError, type Progress } from "./uploads";

const UPLOAD_ID = "00000000-0000-7000-8000-0000000b0001";
const PROJECT_ID = "00000000-0000-7000-8000-0000000c0001";

/** Storage, as the browser's XMLHttpRequest reaches it: answers each PUT with a status. */
class FakeStorage {
  static puts: { url: string; size: number }[] = [];
  static answers: number[] = [];
  upload: { onprogress: ((event: { loaded: number }) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onloadend: (() => void) | null = null;
  status = 0;
  private url = "";
  open(_method: string, url: string): void {
    this.url = url;
  }
  abort(): void {
    /* nothing in flight */
  }
  send(body: Blob): void {
    FakeStorage.puts.push({ url: this.url, size: body.size });
    this.status = FakeStorage.answers.shift() ?? 200;
    setTimeout(() => {
      this.upload.onprogress?.({ loaded: body.size });
      this.onload?.();
      this.onloadend?.();
    }, 0);
  }
}

function started(overrides: Record<string, unknown> = {}) {
  return {
    id: UPLOAD_ID,
    project: PROJECT_ID,
    name: "plate.csv",
    media_type: "text/csv",
    size: 10,
    sha256: "x".repeat(64),
    state: "pending",
    part_size: 6,
    part_count: 2,
    file: null,
    error: "",
    created_at: "2026-03-14T09:00:00Z",
    expires_at: "2026-03-15T09:00:00Z",
    parts: [
      { number: 1, size: 6, url: "https://storage.example/part-1" },
      { number: 2, size: 4, url: "https://storage.example/part-2" },
    ],
    ...overrides,
  };
}

const FILE = {
  id: "00000000-0000-7000-8000-0000000f0009",
  name: "plate.csv",
  media_type: "text/csv",
  size: 10,
  sha256: "x".repeat(64),
  project: null,
  created_by: null,
  created_at: "2026-03-14T09:00:00Z",
  external: false,
};

beforeEach(() => {
  FakeStorage.puts = [];
  FakeStorage.answers = [];
  vi.stubGlobal("XMLHttpRequest", FakeStorage);
  Object.assign(timing, { retry: 1, poll: 1, maxPoll: 1 });
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("hashing", () => {
  it("reads the file a slice at a time, to the same SHA-256", async () => {
    expect(await sha256Of(new Blob(["abc"]))).toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
    const big = new Blob([new Uint8Array(3 * 1024 * 1024 + 5)]);
    const seen: number[] = [];
    await sha256Of(big, (done) => seen.push(done));
    expect(seen).toEqual([1048576, 2097152, 3145728, 3145733]);
  });
});

describe("uploading", () => {
  it("sends each part, signs an expired one again, and waits for the check", async () => {
    answer("POST /api/v1/uploads", respond(201, started()));
    answer("POST /api/v1/uploads/" + UPLOAD_ID + "/parts", [
      { number: 2, size: 4, url: "https://storage.example/part-2-again" },
    ]);
    answer(
      "POST /api/v1/uploads/" + UPLOAD_ID + "/complete",
      respond(202, started({ state: "checking" })),
    );
    let checks = 0;
    answer("GET /api/v1/uploads/" + UPLOAD_ID, () => {
      checks += 1;
      return checks < 2 ? started({ state: "checking" }) : started({ state: "ready", file: FILE });
    });
    // Part 1 goes through; part 2's URL has expired.
    FakeStorage.answers = [200, 403, 200];
    const seen: Progress["phase"][] = [];
    const file = new File(["0123456789"], "plate.csv", { type: "text/csv" });
    const made = await upload(file, PROJECT_ID, (p) => seen.push(p.phase));

    expect(made).toEqual(FILE);
    expect(calls("POST", "/api/v1/uploads")[0]?.body).toEqual({
      project: PROJECT_ID,
      name: "plate.csv",
      media_type: "text/csv",
      size: 10,
      sha256: "84d89877f0d4041efb6bf91a16f0248f2fd573e6af05c19f96bedb9f882f7882",
    });
    expect(FakeStorage.puts).toEqual([
      { url: "https://storage.example/part-1", size: 6 },
      { url: "https://storage.example/part-2", size: 4 },
      { url: "https://storage.example/part-2-again", size: 4 },
    ]);
    expect(calls("POST", "/api/v1/uploads/" + UPLOAD_ID + "/parts")[0]?.body).toEqual({
      numbers: [2],
    });
    expect(new Set(seen)).toEqual(new Set(["hashing", "sending", "checking"]));
  });

  it("gives up on a part after a few tries, saying why", async () => {
    answer("POST /api/v1/uploads", respond(201, started()));
    FakeStorage.answers = [500, 500, 500, 500, 500, 500, 500, 500];
    const file = new File(["0123456789"], "plate.csv", { type: "text/csv" });
    const failure: unknown = await upload(file, PROJECT_ID, () => undefined).catch(
      (error: unknown) => error,
    );
    expect(uploadError(failure)).toMatch(/^Storage refused part \d \(HTTP 500\)\. Try again\.$/);
  });

  it("cancels the upload when people cancel, so storage keeps nothing", async () => {
    answer("POST /api/v1/uploads", respond(201, started()));
    answer("DELETE /api/v1/uploads/" + UPLOAD_ID, respond(204));
    const controller = new AbortController();
    const file = new File(["0123456789"], "plate.csv", { type: "text/csv" });
    const done = upload(
      file,
      PROJECT_ID,
      (p) => {
        if (p.phase === "sending") controller.abort();
      },
      controller.signal,
    );
    await expect(done).rejects.toThrow("The upload was cancelled.");
    await vi.waitFor(() => {
      expect(calls("DELETE")).toHaveLength(1);
    });
  });

  it("says what the server said when it refuses to start", async () => {
    answer(
      "POST /api/v1/uploads",
      respond(422, {
        type: "about:blank",
        title: "Error",
        status: 422,
        code: "validation_failed",
        detail: "Some fields are invalid.",
        errors: [{ pointer: "#/size", detail: "Files can be at most 5 GiB here." }],
      }),
    );
    const file = new File(["0123456789"], "plate.csv", { type: "text/csv" });
    const failure: unknown = await upload(file, PROJECT_ID, () => undefined).catch(
      (error: unknown) => error,
    );
    expect(uploadError(failure)).toBe("Files can be at most 5 GiB here.");
  });
});
