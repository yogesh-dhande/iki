// The theme can't drift from the web app's design tokens, and its colours stay readable.
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, expect, it } from "vitest";
import { TOKENS_CSS, lightTokens, tokensModule } from "../src/system/theme/css-tokens.ts";
import { fontFaces } from "../src/system/theme/fonts.ts";
import {
  caption,
  color,
  font,
  fontFamily,
  ink,
  tones,
  type,
  weight,
} from "../src/system/theme/theme.ts";

const root = join(import.meta.dirname, "..");
const fontsDir = join(root, "fonts");

/** WCAG 2.2's contrast ratio between two #rrggbb colours. */
function contrast(a: string, b: string): number {
  const luminance = (hex: string): number => {
    const channels = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
    const [r = 0, g = 0, bl = 0] = channels.map((c) =>
      c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4,
    );
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return ((light ?? 0) + 0.05) / ((dark ?? 0) + 0.05);
}

describe("the theme", () => {
  it("is generated from the web app's design tokens, unchanged", () => {
    const css = readFileSync(join(root, TOKENS_CSS), "utf8");
    const generated = readFileSync(join(root, "src/system/theme/tokens.generated.ts"), "utf8");
    expect(generated, "Run `pnpm --filter @ikisci/videos tokens`.").toBe(
      tokensModule(lightTokens(css)),
    );
  });

  it("reads the light theme only, not the dark one", () => {
    const css =
      ":root { --bg: #fff; } @media (prefers-color-scheme: dark) { :root { --bg: #000; } }";
    expect(lightTokens(css)).toEqual({ bg: "#fff" });
  });

  it.each(Object.entries(ink).filter(([name]) => name !== "onAccent"))(
    "sets %s text at AA contrast on every surface",
    (_, value) => {
      for (const surface of [color.surface, color.surfaceSunken, color.canvas]) {
        expect(contrast(value, surface)).toBeGreaterThanOrEqual(4.5);
      }
    },
  );

  it.each(Object.entries(tones))("sets %s text at AA contrast on its wash", (_, tone) => {
    expect(contrast(tone.text, tone.wash)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(color.text, tone.wash)).toBeGreaterThanOrEqual(4.5);
  });

  it("sets text on the accent at AA contrast", () => {
    expect(contrast(color.onAccent, color.accent)).toBeGreaterThanOrEqual(4.5);
  });

  it("keeps captions large and on a high-contrast plate", () => {
    expect(contrast(color.text, caption.plate.fill)).toBeGreaterThanOrEqual(7);
    // A 1080p frame on a phone is about a fifth of its size: 52 px stays about 10 pt.
    expect(caption.type.size).toBeGreaterThanOrEqual(48);
  });

  it("has no text smaller than the label size", () => {
    for (const role of Object.values(type))
      expect(role.size).toBeGreaterThanOrEqual(type.label.size);
  });
});

describe("the fonts", () => {
  it("are bundled, with their licences, in every weight the theme uses", () => {
    for (const { file } of fontFaces) {
      expect(existsSync(join(fontsDir, file)), file).toBe(true);
      expect(existsSync(join(fontsDir, dirname(file), "OFL.txt")), `${dirname(file)}/OFL.txt`).toBe(
        true,
      );
    }
    for (const family of Object.values(fontFamily)) {
      const weights = fontFaces.filter((face) => face.family === family).map((face) => face.weight);
      expect(weights.sort()).toEqual(Object.values(weight).sort());
    }
  });

  it("are the only fonts the theme names, never the machine's own", () => {
    expect(font.text).toBe(`"${fontFamily.text}"`);
    expect(font.mono).toBe(`"${fontFamily.mono}"`);
  });
});
