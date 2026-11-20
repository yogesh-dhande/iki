// The videos' own fonts, bundled in fonts/ with their licences, so a render looks the same
// on every machine: Inter for text and JetBrains Mono for IDs and sequences, both under
// the SIL Open Font License 1.1. The web app uses the system's fonts, which differ by
// machine, so the videos can't. Families have names of their own, so a copy installed on
// the machine never stands in for them.
import { fontFamily, weight } from "./theme.ts";

/** Each face: its family, its weight, and its file under fonts/. */
export const fontFaces = [
  { family: fontFamily.text, weight: weight.regular, file: "inter/Inter-Regular.woff2" },
  { family: fontFamily.text, weight: weight.medium, file: "inter/Inter-Medium.woff2" },
  { family: fontFamily.text, weight: weight.semibold, file: "inter/Inter-SemiBold.woff2" },
  { family: fontFamily.text, weight: weight.bold, file: "inter/Inter-Bold.woff2" },
  {
    family: fontFamily.mono,
    weight: weight.regular,
    file: "jetbrains-mono/JetBrainsMono-Regular.woff2",
  },
  {
    family: fontFamily.mono,
    weight: weight.medium,
    file: "jetbrains-mono/JetBrainsMono-Medium.woff2",
  },
  {
    family: fontFamily.mono,
    weight: weight.semibold,
    file: "jetbrains-mono/JetBrainsMono-SemiBold.woff2",
  },
  { family: fontFamily.mono, weight: weight.bold, file: "jetbrains-mono/JetBrainsMono-Bold.woff2" },
] as const;
