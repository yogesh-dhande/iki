/**
 * Labels (ADR 0180): their codes drawn by bwip-js, a label at its size, and the dialog that
 * previews them without recording it, prints them, and downloads ZPL, against a fake API.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { queryClient } from "./api";
import { codeSvg, LabelDialog, LabelFace, svgSource } from "./labels";
import type { LabelSize, PreparedLabel } from "./storage";
import { answer, calls, problem } from "./test-server";

const STANDARD: LabelSize = {
  key: "standard",
  label: "Standard, 50.8 × 25.4 mm (2 × 1 in)",
  width_mm: 50.8,
  height_mm: 25.4,
  code128: true,
};
const CRYO: LabelSize = {
  key: "cryo",
  label: "Cryo tube, 32.5 × 12.7 mm",
  width_mm: 32.5,
  height_mm: 12.7,
  code128: false,
};
const TUBE: PreparedLabel = {
  id: "00000000-0000-7000-8000-0000000000a1",
  hrid: "TUBE-000012",
  title: "Lysate 3",
  line: "LOT-000002 · 2026-11-20",
  code: "IKI:000000000000700080000000000000A1",
  vendor_code: null,
};

describe("codes", () => {
  it("draws a DataMatrix, a QR code and a Code 128 as SVG", async () => {
    for (const kind of ["datamatrix", "qr", "code128"] as const) {
      const svg = await codeSvg(kind, "LB-0042");
      expect(svg.startsWith("<svg")).toBe(true);
      expect(svg).toContain("<path");
    }
    // A longer code needs more modules.
    const short = await codeSvg("code128", "A1");
    const long = await codeSvg("code128", "TUBE-000012-LONGER");
    const width = (svg: string) => Number(/viewBox="0 0 (\d+)/.exec(svg)?.[1]);
    expect(width(long)).toBeGreaterThan(width(short));
  });

  it("shows an SVG as an image's data: source, as the page's policy allows", () => {
    expect(svgSource("<svg/>")).toBe("data:image/svg+xml;charset=utf-8,%3Csvg%2F%3E");
  });
});

describe("a label", () => {
  it("has its unique Iki code and its HRID, title and line, and no vendor barcode unasked", async () => {
    const { container } = render(<LabelFace label={TUBE} size={STANDARD} symbology="datamatrix" />);
    const face = screen.getByRole("img", { name: "Label for TUBE-000012" });
    expect(face.className).toContain("label-standard");
    expect(within(face).getByText("TUBE-000012")).toBeTruthy();
    expect(within(face).getByText("Lysate 3")).toBeTruthy();
    expect(within(face).getByText("LOT-000002 · 2026-11-20")).toBeTruthy();
    // The code it carries is for scanners: the HRID is what a person reads.
    expect(within(face).queryByText(TUBE.code)).toBeNull();
    await waitFor(() => {
      expect(container.querySelectorAll("img[data-symbol=iki]")).toHaveLength(1);
    });
    expect(container.querySelectorAll("img")).toHaveLength(1);
  });

  it("adds the vendor’s Code 128 apart from the Iki code, and names it, when asked for", async () => {
    const { container } = render(
      <LabelFace
        label={{ ...TUBE, vendor_code: "LB-0042" }}
        size={STANDARD}
        symbology="datamatrix"
      />,
    );
    const face = screen.getByRole("img", {
      name: "Label for TUBE-000012, with vendor barcode LB-0042",
    });
    await waitFor(() => {
      expect(container.querySelectorAll("img")).toHaveLength(2);
    });
    const bar = container.querySelector(".label-bar");
    expect(bar?.querySelector("img[data-symbol=vendor]")).not.toBeNull();
    expect(within(face).getByText("Vendor LB-0042")).toBeTruthy();
  });

  it("prints the whole of a long HRID on a cryo tube’s, with only its 2D code", async () => {
    const long = { ...TUBE, hrid: "CRYOPRESERVED-ALIQUOT-0000012345" };
    const { container } = render(<LabelFace label={long} size={CRYO} symbology="qr" />);
    expect(screen.getByText(long.hrid)).toBeTruthy();
    await waitFor(() => {
      expect(container.querySelectorAll("img")).toHaveLength(1);
    });
  });

  it("draws the Iki code as a square 24 × 24 DataMatrix", async () => {
    const side = (svg: string) => {
      const box = /viewBox="0 0 (\d+) (\d+)"/.exec(svg);
      expect(box?.[1]).toBe(box?.[2]);
      return Number(box?.[1]);
    };
    // Fixed at 24 × 24, whatever it holds: a single character would fit 10 × 10.
    const iki = side(await codeSvg("datamatrix", TUBE.code));
    expect(side(await codeSvg("datamatrix", "A"))).toBe(iki);
    expect(iki % 24).toBe(0);
  });
});

function answerLabels() {
  answer("GET /api/v1/storage/labels", { sizes: [CRYO, STANDARD], dpis: [203, 300] });
  answer("POST /api/v1/storage/labels", (call: { body: unknown }) => {
    const body = call.body as { format: string; preview: boolean };
    return {
      labels: [TUBE],
      size: STANDARD,
      zpl: body.format === "zpl" && !body.preview ? "^XA^FDLB-0042^FS^XZ" : null,
    };
  });
}

describe("the label dialog", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("previews without recording, then downloads ZPL at the size and resolution chosen", async () => {
    answerLabels();
    const user = userEvent.setup();
    const made = vi.fn(() => "blob:labels");
    Object.assign(URL, { createObjectURL: made, revokeObjectURL: vi.fn() });
    const clicked = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    render(
      <QueryClientProvider client={queryClient}>
        <LabelDialog records={[{ id: TUBE.id, hrid: TUBE.hrid }]} onClose={() => undefined} />
      </QueryClientProvider>,
    );
    const dialog = await screen.findByRole("dialog", { name: "Print labels for TUBE-000012" });
    await within(dialog).findByRole("img", { name: "Label for TUBE-000012" });
    const previewed = calls("POST", "/api/v1/storage/labels");
    expect(previewed).toHaveLength(1);
    expect(previewed[0]?.body).toMatchObject({ records: [TUBE.id], preview: true });
    await user.selectOptions(
      within(dialog).getByRole("combobox", { name: "Zebra printer’s resolution" }),
      "300",
    );
    await user.click(within(dialog).getByRole("button", { name: "Download ZPL" }));
    await waitFor(() => {
      expect(clicked).toHaveBeenCalled();
    });
    expect(calls("POST", "/api/v1/storage/labels").at(-1)?.body).toMatchObject({
      format: "zpl",
      dpi: 300,
      size: "standard",
      symbology: "datamatrix",
      preview: false,
    });
    expect(made).toHaveBeenCalled();
  });

  it("previews a cryo tube’s label as it would print, where a vendor barcode has no room", async () => {
    answerLabels();
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <LabelDialog records={[{ id: TUBE.id, hrid: TUBE.hrid }]} onClose={() => undefined} />
      </QueryClientProvider>,
    );
    const dialog = await screen.findByRole("dialog");
    const vendor = await within(dialog).findByRole("checkbox", {
      name: "Also print the vendor barcode (Code 128)",
    });
    expect(vendor).toBeEnabled();
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Size" }), "cryo");
    expect(vendor).toBeDisabled();
    expect(vendor).not.toBeChecked();
    await waitFor(() => {
      expect(calls("POST", "/api/v1/storage/labels").at(-1)?.body).toMatchObject({
        size: "cryo",
        symbology: "datamatrix",
        preview: true,
      });
    });
    expect(calls("POST", "/api/v1/storage/labels").at(-1)?.body).not.toHaveProperty(
      "include_vendor_code",
    );
  });

  it("says why, and prints nothing, when the server can’t fit the vendor barcode", async () => {
    answerLabels();
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <LabelDialog records={[{ id: TUBE.id, hrid: TUBE.hrid }]} onClose={() => undefined} />
      </QueryClientProvider>,
    );
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByRole("img", { name: "Label for TUBE-000012" });
    const why =
      "TUBE-000012: There is no vendor barcode on this record. Turn it off to print the Iki ID.";
    answer(
      "POST /api/v1/storage/labels",
      problem(422, "validation_failed", "Some fields are invalid.", [
        { pointer: "#/records/0", detail: why },
      ]),
    );
    await user.click(
      within(dialog).getByRole("checkbox", { name: "Also print the vendor barcode (Code 128)" }),
    );
    expect(await within(dialog).findByText(why)).toBeInTheDocument();
    expect(
      within(dialog).getByText("These labels can’t be printed as chosen:"),
    ).toBeInTheDocument();
    expect(calls("POST", "/api/v1/storage/labels").at(-1)?.body).toMatchObject({
      include_vendor_code: true,
      preview: true,
    });
    for (const name of ["Print", "Download ZPL"]) {
      expect(within(dialog).getByRole("button", { name })).toHaveAttribute("aria-disabled", "true");
    }
  });

  it("prints only the labels, a copy of each as many times as asked", async () => {
    answerLabels();
    const user = userEvent.setup();
    const printed = vi.spyOn(window, "print").mockImplementation(() => undefined);
    render(
      <QueryClientProvider client={queryClient}>
        <LabelDialog records={[{ id: TUBE.id, hrid: TUBE.hrid }]} onClose={() => undefined} />
      </QueryClientProvider>,
    );
    const dialog = await screen.findByRole("dialog");
    await user.selectOptions(
      await within(dialog).findByRole("combobox", { name: "Copies of each" }),
      "2",
    );
    await user.click(within(dialog).getByRole("radio", { name: "QR code" }));
    await user.click(within(dialog).getByRole("button", { name: "Print" }));
    await waitFor(() => {
      expect(printed).toHaveBeenCalled();
    });
    const sheet = document.querySelector(".label-sheet-standard");
    expect(sheet?.parentElement).toBe(document.body);
    expect(sheet?.querySelectorAll(".label")).toHaveLength(2);
    expect(calls("POST", "/api/v1/storage/labels").at(-1)?.body).toMatchObject({
      format: "print",
      symbology: "qr",
      copies: 2,
      preview: false,
    });
  });
});
