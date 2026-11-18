/** The structure viewer: the page reads the file, checks it, and hands it to Mol*'s frame. */

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { entity, field, PROTEIN, renderApp, STRUCTURE, withTypes } from "./entity-fixtures";
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
      type: "iki/protein.ProteinStructure",
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
    const message = (await given) as {
      structures: { structure: ArrayBuffer; format: string; label: string; colour?: string }[];
    };
    const [one] = message.structures;
    expect(message.structures).toHaveLength(1);
    expect(one?.format).toBe("mmcif");
    expect(one?.label).toBe("1crn.cif");
    // One structure is drawn in Mol*'s own colours, by chain.
    expect(one?.colour).toBeUndefined();
    expect(new TextDecoder().decode(one?.structure)).toBe("data_1CRN\n_entry.id 1CRN\n");
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

  it("says when the browser can't draw 3D, as on many virtual desktops", async () => {
    await onStructure();
    await renderApp("/entities/PST-000001/viewers/structure/");
    const frame = await screen.findByTitle("Structure of Crambin");
    const post = vi.fn();
    Object.defineProperty(frame, "contentWindow", { value: { postMessage: post } });
    fireEvent.load(frame);
    const port = (post.mock.calls[0] as [unknown, string, MessagePort[]])[2][0];
    port?.postMessage({ unsupported: "webgl" });
    expect(await screen.findByText(/This browser can’t draw 3D structures/)).toHaveTextContent(
      "Download 1crn.cif to open it in a desktop viewer",
    );
    expect(screen.queryByTitle("Structure of Crambin")).not.toBeInTheDocument();
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

const VIEW = {
  ...PROTEIN,
  name: "iki/core.View",
  plugin: "iki/core",
  label: "View",
  label_plural: "Views",
  hrid_prefix: "VIEW",
  fields: [
    field({ name: "name", label: "Name", type: "string", plugin: "iki/core" }),
    field({ name: "state", label: "State", type: "json", plugin: "iki/core" }),
  ],
  viewers: [],
};
const VIEW_ID = "0190f5c2-0000-7000-8000-00000000f1e2";
const APO = "0190f5c2-0000-7000-8000-0000000000a1";
const HOLO = "0190f5c2-0000-7000-8000-0000000000a2";
const HOLO_FILE = "0b6f1c1e-5d2c-4a51-9a63-2c1f6a1b9e02";
const CAMERA = { position: [0, 0, 50], target: [0, 0, 0], up: [0, 1, 0] };
const MOVED = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 5, 0, 0, 1];

describe("a view of several structures", () => {
  async function onScene() {
    withTypes();
    answer("GET /api/v1/entity-types", [PROTEIN, WITH_VIEWER, VIEW]);
    const ref = (hrid: string, title: string) => ({
      hrid,
      title,
      type: "iki/protein.ProteinStructure",
      archived: false,
      purged: false,
    });
    answer(
      "GET /api/v1/entities/VIEW-000001",
      entity({
        id: VIEW_ID,
        hrid: "VIEW-000001",
        type: "iki/core.View",
        revision: 1,
        values: {
          name: "Apo and holo",
          targets: [APO, HOLO],
          target_revisions: [1, 1],
          viewers: ["structure", "structure"],
          state: { version: 2, camera: CAMERA, targets: [{ label: "apo" }, { colour: "#009e73" }] },
        },
        refs: { [APO]: ref("PST-000001", "Apo"), [HOLO]: ref("PST-000002", "Holo") },
      }),
    );
    const files = (id: string, name: string) => ({
      [id]: { name, media_type: "chemical/x-mmcif", size: CIF.byteLength },
    });
    for (const [hrid, id, file, name] of [
      ["PST-000001", APO, FILE, "apo.cif"],
      ["PST-000002", HOLO, HOLO_FILE, "holo.cif"],
    ] as const) {
      // By ID, as the view holds them.
      answer(`GET /api/v1/entities/${id}/revisions/1`, {
        number: 1,
        kind: "created",
        values: { structure: file },
        changes: {},
        refs: {},
        files: files(file, name),
      });
      answer(
        `GET /api/v1/entities/${id}`,
        entity({
          id,
          hrid,
          type: "iki/protein.ProteinStructure",
          revision: 1,
          values: { structure: file },
        }),
      );
      answer(`GET /api/v1/files/${file}/content-url`, {
        url: `https://store.example.com/iki-files/blobs/${name}?X-Amz-Signature=x`,
        expires_at: "2026-11-14T12:05:00Z",
        sha256: await sha256(CIF),
      });
      answer(`GET /iki-files/blobs/${name}`, () => new Response(CIF.slice()));
    }
    answer(`PATCH /api/v1/entities/${VIEW_ID}`, entity({ hrid: "VIEW-000001", revision: 2 }));
  }

  it("draws them in one scene, superposes them, and saves how", async () => {
    await onScene();
    const { user } = await renderApp("/entities/VIEW-000001/");
    const legend = await screen.findByRole("list", { name: "Structures" });
    expect(legend).toHaveTextContent("apo PST-000001");
    expect(legend).toHaveTextContent("Holo PST-000002");
    const frame = await screen.findByTitle("Structures of Apo and holo");
    expect(screen.getByText("Drawing apo.cif and holo.cif…")).toBeInTheDocument();
    const post = vi.fn();
    Object.defineProperty(frame, "contentWindow", { value: { postMessage: post } });
    fireEvent.load(frame);
    const port = (post.mock.calls[0] as [unknown, string, MessagePort[]])[2][0];
    if (!port) throw new Error("No port");
    const asked: unknown[] = [];
    let next: ((m: unknown) => void) | null = null;
    port.onmessage = (m) => {
      asked.push(m.data);
      next?.(m.data);
    };
    await waitFor(() => {
      expect(asked).toHaveLength(1);
    });
    const scene = asked[0] as { structures: { label: string; colour?: string }[]; camera: unknown };
    // Each is its own component, in a colour of its own: the first the palette's first.
    expect(scene.structures.map((s) => [s.label, s.colour])).toEqual([
      ["apo", "#0072b2"],
      ["Holo", "#009e73"],
    ]);
    expect(scene.camera).toEqual(CAMERA);
    port.postMessage({ shown: true });

    const superposed = new Promise<unknown>((resolve) => {
      next = resolve;
    });
    await user.click(await screen.findByRole("button", { name: "Superpose on apo" }));
    expect(await superposed).toEqual({ superpose: { onto: 0 } });
    port.postMessage({ superposed: [{ index: 1, rmsd: 0.42, residues: 46 }] });
    expect(await screen.findByText(/Holo: RMSD 0\.42 Å over 46 residues\./)).toBeInTheDocument();

    const apo = screen.getByRole("group", { name: "apo" });
    await user.selectOptions(within(apo).getByRole("combobox", { name: "Colour" }), "");
    await user.click(screen.getByRole("button", { name: "Save the view" }));
    await waitFor(() => {
      expect(asked.at(-1)).toEqual({ get: "state" });
    });
    port.postMessage({
      state: {
        camera: CAMERA,
        structures: [{ selection: [{ chain: "A", start: 10, end: 20 }] }, { transform: MOVED }],
      },
    });
    await waitFor(() => {
      expect(calls("PATCH", `/api/v1/entities/${VIEW_ID}`)).toHaveLength(1);
    });
    const [saved] = calls("PATCH", `/api/v1/entities/${VIEW_ID}`);
    expect(saved?.body).toMatchObject({
      values: {
        target_revisions: [1, 1],
        state: {
          version: 2,
          camera: CAMERA,
          targets: [
            { label: "apo", selection: [{ chain: "A", start: 10, end: 20 }] },
            {
              colour: "#009e73",
              transform: MOVED,
              superposed: { onto: 0, rmsd: 0.42, residues: 46 },
            },
          ],
        },
      },
    });
    const first = (saved?.body as { values: { state: { targets: { colour?: string }[] } } }).values
      .state.targets[0];
    // Coloured by chain, as it was set to before saving.
    expect(first?.colour).toBeUndefined();
  });
});
