import { describe, expect, it } from "vitest";
import {
  dnaProperties,
  molecularMass,
  proteinProperties,
  strandExtinction260,
} from "./molecular-properties.js";
import { AVERAGE, AVERAGE_H, MASS_1H, MONOISOTOPIC } from "./molecular-data.js";

describe("molecular mass and spectroscopy", () => {
  it("weighs water and fixed labels by their neutral atom masses", () => {
    // CIAAW's abridged H 1.0080 and O 15.999 (https://www.ciaaw.org/abridged-atomic-weights.htm);
    // monoisotopic, https://physics.nist.gov/PhysRefData/Compositions/
    expect(molecularMass("H2O").average).toBeCloseTo(2 * 1.008 + 15.999, 9);
    expect(molecularMass("H2O").monoisotopic).toBeCloseTo(18.01056468403, 9);
    expect(molecularMass("D2O")).toEqual(molecularMass("[2H]2O"));
    expect(molecularMass("[13C]").average).toBeCloseTo(13.00335483507, 10);
    expect(molecularMass("[12C]")).toEqual({ average: 12, monoisotopic: 12 });
    for (const invalid of ["", "CO2+", "[400C]", "Xx", "C0", "C2H5OH"])
      expect(() => molecularMass(invalid)).toThrow(RangeError);
    // Hydrogen's own constants, which a disulfide's weighing reads, are the tables'.
    expect([AVERAGE_H, MASS_1H]).toEqual([AVERAGE["H"], MONOISOTOPIC["H"]]);
  });

  it("accounts for peptide termini and only the stated disulfides", () => {
    // Two glycine residues and water. wwPDB chemical component GLY.
    expect(proteinProperties("GG").formula).toBe("C4H8N2O3");
    // Synthetic W, Y and two C. Pace et al., https://doi.org/10.1002/pro.5560041120
    const reduced = proteinProperties("wycc**");
    expect(reduced.extinctionCoefficient).toBe(6990);
    const oxidized = proteinProperties("WYCC", 1);
    expect(oxidized.extinctionCoefficient).toBe(7115);
    expect(oxidized.formula).toBe("C26H29N5O6S2");
    expect(proteinProperties("J")).toEqual(proteinProperties("L"));
    expect(proteinProperties("U").extinctionCoefficient).toBeNull();
    expect(() => proteinProperties("WX")).toThrow(RangeError);
    expect(() => proteinProperties("WC", 1)).toThrow(RangeError);
    expect(() => proteinProperties("W*C")).toThrow(RangeError);
  });

  it("weighs a single DNA strand or both strands of a duplex with explicit termini", () => {
    // Neutral deoxyadenosine-5′-monophosphate, PubChem CID 12599.
    expect(dnaProperties("a").formula).toBe("C10H14N5O6P");
    expect(dnaProperties("AT").formula).toBe("C20H27N7O13P2");
    expect(dnaProperties("AT", { doubleStranded: true, circular: true }).formula).toBe(
      "C40H50N14O24P4",
    );
    expect(() => dnaProperties("AN")).toThrow(RangeError);
  });

  it("sums whole doublets and subtracts each middle nucleotide for a single strand", () => {
    // Synthetic trimers. Cantor et al., https://doi.org/10.1002/bip.1970.360090909
    // and Puglisi & Tinoco, https://doi.org/10.1016/0076-6879(89)80108-9 .
    expect(strandExtinction260("ACT", "DNA")).toBe(21200 + 15200 - 7400);
    expect(strandExtinction260("ACU", "RNA")).toBe(21000 + 16200 - 7200);
    expect(strandExtinction260("T", "DNA")).toBe(8700);
    expect(strandExtinction260("", "RNA")).toBe(0);
    expect(() => strandExtinction260("TU", "RNA")).toThrow(RangeError);
  });
});
