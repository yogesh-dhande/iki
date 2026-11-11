import { describe, expect, it } from "vitest";

import { media } from "./media";

describe("media", () => {
  it("names lab formats the browser doesn't know, by extension", () => {
    expect(media.of({ name: "1ABC.PDB", type: "" })).toBe("chemical/x-pdb");
    // Some systems take .pdb for a Palm database.
    expect(media.of({ name: "1abc.pdb", type: "application/vnd.palm" })).toBe("chemical/x-pdb");
    expect(media.of({ name: "7xyz.cif", type: "" })).toBe("chemical/x-mmcif");
    expect(media.of({ name: "plate.fcs", type: "" })).toBe("application/vnd.isac.fcs");
  });

  it("keeps the browser's type for other files, or says it's unknown", () => {
    expect(media.of({ name: "gel.png", type: "image/png" })).toBe("image/png");
    expect(media.of({ name: "notes", type: "" })).toBe("application/octet-stream");
    expect(media.of({ name: ".pdb", type: "" })).toBe("application/octet-stream");
  });

  it("lets pickers offer files by extension too", () => {
    expect(media.accept([])).toBeUndefined();
    expect(media.accept(["chemical/x-mmcif", "chemical/x-pdb"])).toBe(
      "chemical/x-mmcif,chemical/x-pdb,.pdb,.ent,.cif,.mmcif",
    );
    expect(media.accept(["image/*"])).toBe("image/*,.tif,.tiff");
  });

  it("allows wildcards", () => {
    expect(media.allowed("image/png", ["image/*"])).toBe(true);
    expect(media.allowed("text/plain", ["image/*"])).toBe(false);
    expect(media.allowed("anything/at-all", [])).toBe(true);
  });
});
