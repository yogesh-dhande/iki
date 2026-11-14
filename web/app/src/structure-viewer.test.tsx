/** The structure viewer: the page reads the file, checks it, and hands it to Mol*'s frame. */

import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { entity, PROTEIN, renderApp, STRUCTURE, withTypes } from "./entity-fixtures";
import { MAX_STRUCTURE_BYTES } from "./pages/structure-viewer";
import { answer, calls } from "./test-server";

const FILE = "0b6f1c1e-5d2c-4a51-9a63-2c1f6a1b9e01";
const CIF = new TextEncoder().encode("data_1CRN\n_entry.id 1CRN\n");
/** SHA-256 of CIF, as Iki would have recorded it. */
const CIF_SHA256 = "1e0b3b1d5d2b0f5f8f0f6e0b0a3c3f2b6c5d8e1f9a2b3c4d5e6f708192a3b4c5";

const WITH_VIEWER = {
  ...STRUCTURE,
  viewers: [{ viewer: "structure" as const, label: "Structure", structure: "structure" }],
};

async function sha256(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

async function onStructure(
  file: { name?: string; media_type?: string; size?: number } = {},
  sha?: string,
) {
  withTypes();
  answer("GET /api/v1/entity-types", [PROTEIN, WITH_VIEWER]);
  answer(
    "GET /api/v1/entities/PST-000001",
    entity({
      hrid: "PST-000001",
      type: "protein.ProteinStructure",
      values: { name: "Crambin", structure: FILE },
      files: {
        [FILE]: {
          name: "1crn.cif",
          media_type: "chemical/x-mmcif",
          size: CIF.byteLength,
          ...file,
        },
      },
    }),
  );
  answer(`GET /api/v1/files/${FILE}/content-url`, {
    url: "https://store.example.com/iki-files/blobs/ab/cd?X-Amz-Signature=x",
    expires_at: "2026-11-14T12:05:00Z",
    sha256: sha ?? (await sha256(CIF)),
  });
  answer("GET /iki-files/blobs/ab/cd", () => new Response(CIF.slice()));
}

describe("the structure viewer", () => {
  it("reads the file, checks it, and gives Mol*'s frame the bytes", async () => {
    await onStructure();
    await renderApp("/entities/PST-000001/viewers/structure/");
    expect(await screen.findByText("1crn.cif")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download 1crn.cif" })).toHaveAttribute(
      "href",
      `/api/v1/files/${FILE}/content`,
    );
    const frame = await screen.findByTitle("Structure of Crambin");
    expect(frame).toHaveAttribute("src", "/viewers/structure/");
    expect(frame).toHaveAttribute("sandbox", "allow-scripts");
    expect(screen.getByText("Drawing 1crn.cif…")).toBeInTheDocument();

    const post = vi.fn();
    Object.defineProperty(frame, "contentWindow", { value: { postMessage: post } });
    fireEvent.load(frame);
    expect(post).toHaveBeenCalledWith({ type: "iki-connect" }, "*", [expect.any(MessagePort)]);
    const port = (post.mock.calls[0] as [unknown, string, MessagePort[]])[2][0];
    const given = new Promise<unknown>((resolve) => {
      if (port)
        port.onmessage = (m) => {
          resolve(m.data);
        };
    });
    const message = (await given) as { structure: ArrayBuffer; format: string; label: string };
    expect(message.format).toBe("mmcif");
    expect(message.label).toBe("1crn.cif");
    expect(new TextDecoder().decode(message.structure)).toBe("data_1CRN\n_entry.id 1CRN\n");
    port?.postMessage({ shown: true });
    await waitFor(() => {
      expect(screen.queryByText("Drawing 1crn.cif…")).not.toBeInTheDocument();
    });
  });

  it("says what Mol* couldn't read", async () => {
    await onStructure();
    await renderApp("/entities/PST-000001/viewers/structure/");
    const frame = await screen.findByTitle("Structure of Crambin");
    const post = vi.fn();
    Object.defineProperty(frame, "contentWindow", { value: { postMessage: post } });
    fireEvent.load(frame);
    const port = (post.mock.calls[0] as [unknown, string, MessagePort[]])[2][0];
    port?.postMessage({ failed: "Unexpected token at line 2" });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Mol* couldn’t read 1crn.cif: Unexpected token at line 2",
    );
  });

  it("doesn't show bytes that aren't the file Iki stored", async () => {
    await onStructure({}, CIF_SHA256);
    await renderApp("/entities/PST-000001/viewers/structure/");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "What the file store sent isn’t the file Iki stored",
    );
    expect(screen.queryByTitle("Structure of Crambin")).not.toBeInTheDocument();
  });

  it("leaves files too big for it to desktop viewers", async () => {
    await onStructure({ size: MAX_STRUCTURE_BYTES + 1 });
    await renderApp("/entities/PST-000001/viewers/structure/");
    expect(
      await screen.findByText(/bigger than the structure viewer opens \(256 MiB\)/),
    ).toBeInTheDocument();
    expect(calls("GET", `/api/v1/files/${FILE}/content-url`)).toEqual([]);
  });

  it("says when a file isn't in a format it reads", async () => {
    await onStructure({ media_type: "application/octet-stream" });
    await renderApp("/entities/PST-000001/viewers/structure/");
    expect(
      await screen.findByText("The structure viewer doesn’t read application/octet-stream files."),
    ).toBeInTheDocument();
  });
});
