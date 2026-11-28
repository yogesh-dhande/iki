/** The installed file formats (ADR 0213). The order of declarations breaks ties for
 * shared extensions like .fasta when the field accepts both readings. */
import { unwrap, type Schemas } from "@ikisci/sdk";
import { bytesToHex } from "@noble/hashes/utils.js";
import { queryOptions, useQuery } from "@tanstack/react-query";
import { everyPage, iki } from "./api";

export type FileFormat = Schemas["InstalledFormatOut"];

/** "a", "a or b", "a, b or c". */
function or(items: readonly string[]): string {
  return items.length > 1
    ? `${items.slice(0, -1).join(", ")} or ${items.at(-1) ?? ""}`
    : (items[0] ?? "");
}

export class MediaFormats {
  private readonly byType = new Map<string, FileFormat>();
  private readonly byExtension = new Map<string, FileFormat[]>();
  private prefixLength = 0;

  constructor(readonly formats: readonly FileFormat[]) {
    for (const format of formats) {
      for (const key of [format.id, ...format.media_types]) this.byType.set(key, format);
      for (const extension of format.extensions) {
        const matches = this.byExtension.get(extension) ?? [];
        matches.push(format);
        this.byExtension.set(extension, matches);
      }
      for (const { offset, hex } of format.signatures) {
        this.prefixLength = Math.max(this.prefixLength, offset + hex.length / 2);
      }
    }
  }

  get(type: string): FileFormat | undefined {
    const normalized = type.split(";", 1)[0]?.trim().toLowerCase();
    return normalized ? this.byType.get(normalized) : undefined;
  }

  named(type: string): string {
    return this.get(type)?.label ?? (type.endsWith("/*") ? type.slice(0, -2) : type);
  }

  /** One filter value per format, even when an older schema lists every MIME alias. */
  choices(types: readonly string[]): string[] {
    const seen = new Set<string>();
    return types.filter((type) => {
      const id = this.get(type)?.id ?? type;
      if (seen.has(id)) return false;
      seen.add(id);
      return true;
    });
  }

  either(choices: readonly string[]): string {
    return or(
      this.choices(choices).map((type) => {
        const format = this.get(type);
        return format?.extensions.length
          ? `${format.label} (${or(format.extensions)})`
          : this.named(type);
      }),
    );
  }

  allowed(type: string, allowed: readonly string[]): boolean {
    if (!allowed.length) return true;
    const format = this.get(type);
    const types = format?.media_types ?? [type];
    return allowed.some(
      (choice) =>
        types.includes(choice) ||
        choice === format?.id ||
        (choice.endsWith("/*") && types.some((t) => t.startsWith(choice.slice(0, -1)))),
    );
  }

  /** Longest suffix first. For a shared suffix, honor a compatible browser type,
   * then use the first variant the field takes. */
  of(file: { name: string; type: string }, allowed: readonly string[] = []): string {
    const name = file.name.toLowerCase();
    const reported = this.get(file.type);
    for (let dot = name.indexOf(".", 1); dot >= 0; dot = name.indexOf(".", dot + 1)) {
      const formats = this.byExtension.get(name.slice(dot));
      if (formats?.length) {
        if (reported && formats.includes(reported) && this.allowed(reported.canonical, allowed)) {
          return reported.canonical;
        }
        const format = formats.find((f) => this.allowed(f.canonical, allowed)) ?? formats[0];
        if (format) return format.canonical;
      }
    }
    return reported?.canonical ?? (file.type || "application/octet-stream");
  }

  /** Read only as much as the installed signatures need. The server checks the stored bytes. */
  async identify(file: File, allowed: readonly string[]): Promise<string> {
    const head = bytesToHex(new Uint8Array(await file.slice(0, this.prefixLength).arrayBuffer()));
    let longest = 0;
    const matches = new Set<FileFormat>();
    for (const format of this.formats) {
      for (const { offset, hex } of format.signatures) {
        if (hex.length < longest || !head.startsWith(hex.toLowerCase(), offset * 2)) continue;
        if (hex.length > longest) matches.clear();
        longest = hex.length;
        matches.add(format);
      }
    }
    if (matches.size > 1) {
      const labels = [...matches].map((f) => f.label);
      throw new FormatError(
        `${file.name} matches ${new Intl.ListFormat("en").format(labels)}. Check its format.`,
      );
    }
    const match = matches.values().next().value;
    const type = match?.canonical ?? this.of(file, allowed);
    const found = this.get(type);
    if (!match && found?.signatures.length) {
      throw new FormatError(
        `${file.name} lacks the byte signature for ${found.label}. Check its format.`,
      );
    }
    if (!this.allowed(type, allowed)) {
      const reason = found
        ? `${file.name} is in ${found.label} format.`
        : `${file.name} isn’t a file this takes.`;
      throw new FormatError(`${reason} Choose a file matching ${this.either(allowed)}.`);
    }
    return type;
  }

  /** Include the extensions so a picker recognizes scientific formats unknown to the OS. */
  accept(allowed: readonly string[]): string | undefined {
    if (!allowed.length) return undefined;
    const types = allowed.flatMap((type) => this.get(type)?.media_types ?? [type]);
    const extensions = [...this.byExtension]
      .filter(([, formats]) => formats.some((f) => this.allowed(f.canonical, allowed)))
      .map(([extension]) => extension);
    return [...new Set([...types, ...extensions])].join(",");
  }
}

export class FormatError extends Error {}

export const formatsQuery = queryOptions({
  queryKey: ["formats"],
  queryFn: async ({ signal }) =>
    new MediaFormats(
      await everyPage(async (page) =>
        unwrap(await iki.GET("/api/v1/formats", { params: { query: page }, signal })),
      ),
    ),
  staleTime: 10 * 60_000,
});

const empty = new MediaFormats([]);

/** Until the query arrives labels fall back to the file's recorded media type. */
export function useMedia(enabled = true): MediaFormats {
  return useQuery({ ...formatsQuery, enabled }).data ?? empty;
}
