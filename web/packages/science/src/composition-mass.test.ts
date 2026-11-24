import { describe, expect, it } from "vitest";
import { absorbanceAtOneGramPerLitre, formulaAtoms, withDisulfides } from "./composition-mass.js";

// Insulin chains and their three disulfides: UniProt P01308.
// Average H: CIAAW (1981), doi:10.1351/pac198355071101. NIST's ¹H mass is
// 1.00782503223 u. Cystine's ε280: Pace et al., doi:10.1002/pro.5560041120.
const reduced = {
  formula: "C257H389N65O77S6",
  average: 5813.62,
  monoisotopic: 5809.6846,
  extinctionCoefficient: 5960,
};

describe("composition mass", () => {
  it("applies insulin's three disulfides and Beer–Lambert at 1 g/L over 1 cm", () => {
    const bonded = withDisulfides(reduced, 3);
    expect(bonded.formula).toBe("C257H383N65O77S6");
    expect(bonded.average).toBeCloseTo(5807.57, 2);
    expect(bonded.monoisotopic).toBeCloseTo(5803.63765, 4);
    expect(bonded.extinctionCoefficient).toBe(6335);
    // A = ε c l, doi:10.1351/goldbook.B00626. Concentration in mol/L is 1/M.
    expect(absorbanceAtOneGramPerLitre(bonded)).toBeCloseTo(1.090818, 5);
    expect(reduced.formula).toBe("C257H389N65O77S6");
  });

  it("keeps isotope labels, unknown measurements and extinction coefficients", () => {
    // Synthetic isotope fixture, not a measured composition.
    const partial = {
      formula: "C2[13C]H2DS2",
      average: null,
      monoisotopic: null,
      extinctionCoefficient: null,
    };
    expect(withDisulfides(partial, 1)).toEqual({ ...partial, formula: "C2[13C]DS2" });
    expect(withDisulfides(partial, 0)).toEqual(partial);
    expect(absorbanceAtOneGramPerLitre(partial)).toBeNull();
    expect(absorbanceAtOneGramPerLitre({ ...reduced, extinctionCoefficient: 0 })).toBe(0);
  });

  it("rejects non-integral counts and formulae it cannot interpret", () => {
    expect(() => withDisulfides(reduced, -1)).toThrow(RangeError);
    expect(() => withDisulfides(reduced, 0.5)).toThrow(RangeError);
    expect(() => withDisulfides(reduced, 400)).toThrow(RangeError);
    expect(() => withDisulfides({ ...reduced, formula: "Na+" }, 0)).toThrow(RangeError);
    for (const formula of ["CH3CH3", "Na+", "H2O(2)", "C0", "", "Na Cl"]) {
      expect(() => formulaAtoms(formula)).toThrow(RangeError);
    }
  });
});
