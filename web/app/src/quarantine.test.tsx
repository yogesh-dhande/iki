/**
 * Admin → Quarantine (ADR 0217), against a fake API: what's held and why, filtered by its
 * address; a finding's page with where its bytes are; releasing a false positive with a
 * reason; an incomplete scan, which can't be released; empty and refused lists; and
 * nobody but a Quarantine Admin opening it.
 */

import type { Schemas } from "@ikisci/sdk";
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { KINASE, renderApp } from "./entity-fixtures";
import { heldBecause, validateQuarantineSearch } from "./quarantine";
import { answer, calls, ME, page, problem, signedIn, type Sent } from "./test-server";

type Entry = Schemas["QuarantineOut"];

const REVIEWER = { ...ME.can, manage_quarantine: true };
const MACRO_ID = "00000000-0000-7000-8000-0000000d0001";
const TAR_ID = "00000000-0000-7000-8000-0000000d0002";
const FILE_ID = "00000000-0000-7000-8000-0000000f0001";

const GRACE = {
  id: "00000000-0000-7000-8000-0000000000u2",
  email: "grace@example.com",
  name: "Grace Hopper",
  kind: "person" as const,
  active: true,
  imported_from: "",
};

const ADA = {
  ...GRACE,
  id: "00000000-0000-7000-8000-0000000000u3",
  email: "ada@example.com",
  name: "Ada Lovelace",
};

function copy(overrides: Partial<Schemas["HeldCopyOut"]> = {}): Schemas["HeldCopyOut"] {
  return {
    kind: "file",
    file: null,
    name: "EnVision_plate-07.xlsm",
    media_type: "application/vnd.ms-excel.sheet.macroenabled.12",
    project: { id: KINASE.id, name: KINASE.name, slug: KINASE.slug },
    run: null,
    created_by: GRACE,
    agent: null,
    created_at: "2026-11-24T08:10:00Z",
    ...overrides,
  };
}

/** A macro-enabled plate-reader export that ClamAV's heuristics flag. */
function macro(overrides: Partial<Entry> = {}): Entry {
  return {
    id: MACRO_ID,
    sha256: "3f1c9a7e5b2d".padEnd(64, "0"),
    size: 48_213,
    state: "infected",
    scanner: "ClamAV 1.4.3",
    signature_version: "27453",
    scanned_at: "2026-11-24T08:12:00Z",
    reason: "Xls.Downloader.Agent-7372345-0",
    releasable: true,
    release: null,
    files: [copy()],
    file_count: 1,
    ...overrides,
  };
}

/** A sequencing run's archive, larger than the deployment scans. */
function tarball(): Entry {
  return macro({
    id: TAR_ID,
    sha256: "9".repeat(64),
    size: 412_000_000_000,
    state: "not_scanned",
    scanner: "ClamAV",
    signature_version: "",
    reason: "too_large",
    releasable: false,
    files: [copy({ name: "NovaSeq_221103_A01234.tar", media_type: "application/x-tar" })],
  });
}

function onList(items: Entry[] | Response) {
  signedIn({ can: REVIEWER });
  answer("GET /api/v1/quarantine-decisions", Array.isArray(items) ? page(items) : items);
}

describe("why a file is held, in words", () => {
  it("names a finding, and an inspection that couldn't finish by what stopped it", () => {
    expect(heldBecause({ state: "infected", reason: "Win.Test.EICAR_HDB-1" })).toMatchObject({
      label: "Malware found",
      finding: "Win.Test.EICAR_HDB-1",
    });
    expect(heldBecause({ state: "not_scanned", reason: "too_large" }).label).toBe(
      "Too large to scan",
    );
    expect(heldBecause({ state: "not_scanned", reason: "clamd_stream_limit" }).label).toBe(
      "Too large for the scanner",
    );
    expect(heldBecause({ state: "not_scanned", reason: "Heuristics.Encrypted.PDF" })).toMatchObject(
      { label: "Encrypted", finding: "Heuristics.Encrypted.PDF" },
    );
    expect(
      heldBecause({ state: "not_scanned", reason: "Heuristics.Limits.Exceeded.MaxFiles" }).label,
    ).toBe("Beyond the scanner’s limits");
    expect(heldBecause({ state: "not_scanned", reason: "adapter_timeout" })).toMatchObject({
      label: "Scan incomplete",
      finding: "adapter_timeout",
    });
  });

  it("keeps only the filters it knows in the address", () => {
    expect(
      validateQuarantineSearch({ status: "released", state: "infected", q: " plate " }),
    ).toEqual({ status: "released", state: "infected", q: "plate" });
    expect(validateQuarantineSearch({ status: "clean", state: "pending", q: "" })).toEqual({});
  });
});

describe("Admin → Quarantine", () => {
  it("lists each held file with its project, who added it and why it’s held", async () => {
    onList([
      macro({ files: [copy(), copy({ name: "EnVision_plate-08.xlsm" })], file_count: 4 }),
      tarball(),
    ]);
    await renderApp("/admin/quarantine/");
    const table = await screen.findByRole("table", { name: "Files held in quarantine" });
    const [, first, second] = within(table).getAllByRole("row");
    if (!first || !second) throw new Error("expected two rows");
    expect(first).toHaveTextContent("EnVision_plate-07.xlsm");
    expect(first).toHaveTextContent("47 KiB · and 3 more files");
    expect(first).toHaveTextContent("Kinase Atlas");
    expect(first).toHaveTextContent("Malware foundXls.Downloader.Agent-7372345-0");
    expect(first).toHaveTextContent("by Grace Hopper");
    expect(within(first).getByRole("link", { name: "EnVision_plate-07.xlsm" })).toHaveAttribute(
      "href",
      `/admin/quarantine/${MACRO_ID}/`,
    );
    expect(second).toHaveTextContent("NovaSeq_221103_A01234.tar");
    expect(second).toHaveTextContent("Too large to scan");
    // The sidebar has it, under Security, as the page it's on.
    const nav = screen.getByRole("navigation", { name: "Admin" });
    expect(within(nav).getByRole("link", { name: "Quarantine" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(calls("GET", "/api/v1/quarantine-decisions").at(-1)?.query.get("released")).toBe(
      "false",
    );
  });

  it("filters by status, what was found and name, and keeps them in the address", async () => {
    onList([macro()]);
    const { user, router } = await renderApp("/admin/quarantine/");
    await screen.findByRole("table", { name: "Files held in quarantine" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Why" }), "infected");
    await waitFor(() => {
      expect(calls("GET", "/api/v1/quarantine-decisions").at(-1)?.query.get("state")).toBe(
        "infected",
      );
    });
    answer("GET /api/v1/quarantine-decisions", page([]));
    await user.type(screen.getByRole("searchbox", { name: "Search" }), "plate-09");
    expect(
      await screen.findByText(
        "No held files match. Try another part of the name, or clear the filters.",
      ),
    ).toBeVisible();
    const asked = calls("GET", "/api/v1/quarantine-decisions").at(-1)?.query;
    expect([asked?.get("q"), asked?.get("state")]).toEqual(["plate-09", "infected"]);
    expect(router.state.location.search).toEqual({ state: "infected", q: "plate-09" });
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => {
      expect(router.state.location.search).toEqual({});
    });
  });

  it("lists released ones with who released them", async () => {
    onList([
      macro({
        releasable: false,
        release: { by: ADA, at: "2026-11-24T10:30:00Z", reason: "Matched the vendor's original." },
      }),
    ]);
    await renderApp("/admin/quarantine/?status=released");
    const table = await screen.findByRole("table", { name: "Files released from quarantine" });
    expect(within(table).getByRole("columnheader", { name: "Released" })).toBeVisible();
    expect(within(table).getAllByRole("row")[1]).toHaveTextContent("by Ada Lovelace");
    expect(calls("GET", "/api/v1/quarantine-decisions").at(-1)?.query.get("released")).toBe("true");
  });

  it("says when nothing is held, and when nothing was released", async () => {
    onList([]);
    await renderApp("/admin/quarantine/");
    expect(
      await screen.findByText(
        "Nothing is held. Files the scanner flags, or can’t inspect completely, wait here for review.",
      ),
    ).toBeVisible();
  });

  it("says why it couldn’t load the list", async () => {
    onList(problem(403, "forbidden", "Only Quarantine Admins can review quarantined files."));
    await renderApp("/admin/quarantine/");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Only Quarantine Admins can review quarantined files.",
    );
  });

  it("is only for Quarantine Admins", async () => {
    signedIn({ can: { ...ME.can, manage_compute: true } });
    await renderApp("/admin/quarantine/");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Not found");
    expect(calls("GET", "/api/v1/quarantine-decisions")).toHaveLength(0);
  });
});

describe("a quarantined file’s page", () => {
  it("shows the finding and where the bytes are, and releases it with a reason", async () => {
    signedIn({ can: REVIEWER });
    const held = macro({
      files: [
        copy({ file: FILE_ID }),
        copy({
          kind: "artifact",
          name: "plate-07-normalised.xlsm",
          run: "RUN-000042",
          created_by: ADA,
        }),
      ],
      file_count: 2,
    });
    answer(`GET /api/v1/quarantine-decisions/${MACRO_ID}`, held);
    const reason = "Compared byte for byte with the EnVision export on the instrument PC.";
    answer(`POST /api/v1/quarantine-decisions/${MACRO_ID}/release`, (sent: Sent) =>
      macro({
        ...held,
        releasable: false,
        release: {
          by: GRACE,
          at: "2026-11-24T10:30:00Z",
          reason: (sent.body as { reason: string }).reason,
        },
      }),
    );
    const { user } = await renderApp(`/admin/quarantine/${MACRO_ID}/`);
    expect(
      await screen.findByRole("heading", { level: 2, name: "EnVision_plate-07.xlsm Held" }),
    ).toBeVisible();
    const finding = screen.getByRole("region", { name: "Finding" });
    expect(finding).toHaveTextContent("Malware found");
    expect(finding).toHaveTextContent("SignatureXls.Downloader.Agent-7372345-0");
    expect(finding).toHaveTextContent("ClamAV 1.4.3, signatures 27453");
    const where = screen.getByRole("region", { name: "Where it is" });
    expect(where).toHaveTextContent("These bytes are in 2 places, which share this scan");
    // A file in a project the reviewer reads opens; a run's artifact names its run.
    expect(within(where).getByRole("link", { name: "EnVision_plate-07.xlsm" })).toHaveAttribute(
      "href",
      `/files/${FILE_ID}/`,
    );
    expect(within(where).getByRole("row", { name: /plate-07-normalised/ })).toHaveTextContent(
      "Run artifactof RUN-000042",
    );

    await user.click(screen.getByRole("button", { name: "Release…" }));
    const dialog = await screen.findByRole("dialog", { name: "Release EnVision_plate-07.xlsm?" });
    expect(dialog).toHaveTextContent("the projects of its 2 files");
    expect(dialog).toHaveTextContent("It can’t be undone.");
    await user.click(within(dialog).getByRole("button", { name: "Release" }));
    expect(await within(dialog).findByText("Say why it’s a false positive.")).toBeVisible();
    expect(calls("POST", `/api/v1/quarantine-decisions/${MACRO_ID}/release`)).toHaveLength(0);
    const field = within(dialog).getByRole("textbox", { name: "Why is it a false positive?" });
    expect(field).toHaveFocus();
    await user.type(field, reason);
    await user.click(within(dialog).getByRole("button", { name: "Release" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(calls("POST", `/api/v1/quarantine-decisions/${MACRO_ID}/release`).at(-1)?.body).toEqual({
      reason,
    });
    expect(
      await screen.findByText("Released EnVision_plate-07.xlsm from quarantine."),
    ).toBeVisible();
    const release = screen.getByRole("region", { name: "Release" });
    expect(release).toHaveTextContent("Released by Grace Hopper (grace@example.com)");
    expect(release).toHaveTextContent(reason);
    await waitFor(() => {
      expect(within(release).getByRole("heading", { name: "Release" })).toHaveFocus();
    });
    const history = screen.getByRole("region", { name: "History" });
    expect(history).toHaveTextContent("ClamAV 1.4.3 found Xls.Downloader.Agent-7372345-0.");
    expect(history).toHaveTextContent(`By Grace Hopper: ${reason}`);
  });

  it("says why the server refused a release", async () => {
    signedIn({ can: REVIEWER });
    answer(`GET /api/v1/quarantine-decisions/${MACRO_ID}`, macro());
    answer(
      `POST /api/v1/quarantine-decisions/${MACRO_ID}/release`,
      problem(409, "conflict", "This finding has already been released."),
    );
    const { user } = await renderApp(`/admin/quarantine/${MACRO_ID}/`);
    await user.click(await screen.findByRole("button", { name: "Release…" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(
      within(dialog).getByRole("textbox"),
      "Vendor confirmed the signature misfires.",
    );
    await user.click(within(dialog).getByRole("button", { name: "Release" }));
    expect(
      await within(dialog).findByText("This finding has already been released."),
    ).toBeVisible();
  });

  it("offers no release for a scan that couldn’t finish, and says why", async () => {
    signedIn({ can: { ...REVIEWER, read_audit: true } });
    answer(`GET /api/v1/quarantine-decisions/${TAR_ID}`, tarball());
    await renderApp(`/admin/quarantine/${TAR_ID}/`);
    const release = await screen.findByRole("region", { name: "Release" });
    expect(release).toHaveTextContent(
      "It can’t be released. The scanner didn’t finish inspecting it",
    );
    expect(screen.queryByRole("button", { name: "Release…" })).toBeNull();
    expect(screen.getByRole("region", { name: "Finding" })).toHaveTextContent(
      "It’s larger than the deployment’s maximum scan size",
    );
    // An auditor too finds its events in the audit log.
    expect(screen.getByRole("link", { name: "every incomplete scan" })).toHaveAttribute(
      "href",
      "/admin/audit/?action=file.not_scanned",
    );
    // A reviewer who can't read its project isn't sent to a page they can't open.
    const where = screen.getByRole("region", { name: "Where it is" });
    expect(within(where).queryByRole("link")).toBeNull();
    expect(where).toHaveTextContent("a file’s name opens its page only in a project you can read");
  });

  it("says a decision isn’t one it holds", async () => {
    signedIn({ can: REVIEWER });
    answer(
      `GET /api/v1/quarantine-decisions/${MACRO_ID}`,
      problem(404, "not_found", "No such quarantined file."),
    );
    await renderApp(`/admin/quarantine/${MACRO_ID}/`);
    expect(await screen.findByRole("alert")).toHaveTextContent("No such quarantined file.");
    expect(screen.getByRole("link", { name: "All quarantined files" })).toHaveAttribute(
      "href",
      "/admin/quarantine/",
    );
  });
});
