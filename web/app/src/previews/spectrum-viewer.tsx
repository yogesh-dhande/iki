/**
 * Spectra as a plot (ADR 0226): a JCAMP-DX file's spectra (IUPAC's JCAMP-DX, its
 * compressed forms too), and an mzML file's spectra and chromatograms (HUPO-PSI's mzML 1.1),
 * read in the previews' worker by iki-previews' readers.
 *
 * A JCAMP-DX file is read whole and checked. An mzML file can be gigabytes, so it's read a
 * spectrum at a time (ADR 0227): an indexed file's last bytes say where its index is, the
 * index says where each spectrum starts, and only that spectrum's bytes are read. A file
 * without an index is scanned from its start, as far as the preview reads.
 */

import { useId, useMemo, useState } from "react";

import { formatSize } from "../entities";
import { Button, Loading } from "../ui";
import { openBytes, type ByteSource } from "./bytes";
import type { ViewerProps } from "./file-preview";
import { count, Integrity, plural, PreviewNote, Verified } from "./notes";
import { Peaks, Plot, type Series } from "./plot";
import type { PreviewReader } from "./reader";
import type { MzmlElement, MzmlEntry, MzmlIndex, MzmlSpectrum, SpectrumBlock } from "./readings";
import { ReadError } from "./source";
import { ReadFailed, useRead } from "./use-read";

/** The most points a spectrum or chromatogram plots. */
export const MAX_POINTS = 2_000_000;
/** An mzML file up to this is read whole and checked; a larger one, in parts. */
export const MZML_WHOLE = 64 * 1024 * 1024;
/** How much of an mzML file's end the reader looks through for `<indexListOffset>`. */
export const MZML_TAIL = 64 * 1024;
/** The most an index, or one spectrum, may be. */
export const MZML_PART = 64 * 1024 * 1024;

export default function SpectrumViewer(props: ViewerProps) {
  return props.viewer === "spectrum-mzml" ? <Mzml {...props} /> : <Jcamp {...props} />;
}

/** IR and Raman wavenumbers and NMR shifts are drawn high to low, as spectroscopists read them. */
export function reversedAxis(xUnits: string | null, dataType: string | null): boolean {
  const units = (xUnits ?? "").toUpperCase().replace(/\s/g, "");
  const type = (dataType ?? "").toUpperCase();
  return units === "1/CM" || units === "CM-1" || units === "PPM" || type.includes("NMR");
}

function blockName(block: SpectrumBlock, index: number): string {
  return block.title ?? block.data_type ?? `Spectrum ${String(index + 1)}`;
}

function Jcamp({ file, stored, reader }: ViewerProps) {
  const read = useRead(
    async (signal) => {
      const bytes = new Uint8Array(await stored.whole(signal));
      const { value } = await reader.read("jcamp", [], bytes);
      return value;
    },
    [stored, reader],
  );
  const [chosen, setChosen] = useState(0);
  const id = useId();
  if (read.phase === "failed") return <ReadFailed file={file} failed={read} />;
  if (read.phase === "reading") return <Loading label={`Reading ${file.name}…`} />;
  const spectra = read.value;
  const block = spectra.blocks[chosen] ?? spectra.blocks[0];
  return (
    <div className="preview-spectrum">
      {spectra.blocks.length > 1 ? (
        <div className="preview-toolbar" role="toolbar" aria-label={`Spectra in ${file.name}`}>
          <span className="preview-zoom">
            <label htmlFor={`${id}-block`}>Spectrum</label>
            <select
              id={`${id}-block`}
              value={chosen}
              onChange={(event) => {
                setChosen(Number(event.target.value));
              }}
            >
              {spectra.blocks.map((b, i) => (
                <option key={i} value={i}>
                  {blockName(b, i)}
                </option>
              ))}
            </select>
          </span>
        </div>
      ) : null}
      {block ? (
        <JcampBlock block={block} index={chosen} />
      ) : (
        <p>{file.name} has no spectra to plot.</p>
      )}
      <PreviewNote>
        {spectra.truncated ? "Some of its spectra or points are past what the preview reads. " : ""}
        <Verified />
      </PreviewNote>
      <Problems problems={spectra.problems} />
    </div>
  );
}

function JcampBlock({ block, index }: { block: SpectrumBlock; index: number }) {
  const series = useMemo<Series>(
    () => ({ x: block.x, y: block.y, kind: block.kind === "peaks" ? "sticks" : "line" }),
    [block],
  );
  const xLabel = block.x_units ? `x (${block.x_units})` : "x";
  const yLabel = block.y_units ? `y (${block.y_units})` : "y";
  const name = blockName(block, index);
  return (
    <>
      <dl className="preview-facts">
        {block.data_type ? (
          <div>
            <dt>Type</dt>
            <dd>{block.data_type}</dd>
          </div>
        ) : null}
        <div>
          <dt>Points</dt>
          <dd>
            {count(block.x.length)}
            {block.declared_points !== null && block.declared_points !== block.x.length
              ? ` (it declares ${count(block.declared_points)})`
              : ""}
          </dd>
        </div>
      </dl>
      {block.x.length ? (
        <>
          <Plot
            series={series}
            xLabel={xLabel}
            yLabel={yLabel}
            reversed={reversedAxis(block.x_units, block.data_type)}
            label={name}
          />
          <Peaks series={series} xLabel={xLabel} yLabel={yLabel} />
        </>
      ) : (
        <p>It has no points to plot.</p>
      )}
    </>
  );
}

function Problems({ problems }: { problems: string[] }) {
  if (!problems.length) return null;
  return (
    <ul className="preview-problems">
      {problems.slice(0, 20).map((p, i) => (
        <li key={i}>{p}</li>
      ))}
    </ul>
  );
}

/** How an mzML file was found to be laid out. */
interface Found {
  source: ByteSource;
  index: MzmlIndex;
  /** Where its index starts, which ends its last spectrum or chromatogram. */
  end: number;
  /** Whether it was scanned, having no index. */
  scanned: boolean;
  /** Every entry's offset, sorted: an element ends where the next begins. */
  offsets: number[];
}

/** Finds an mzML file's spectra: from its index if it has one, else by scanning its start. */
export async function findSpectra(
  source: ByteSource,
  reader: PreviewReader,
  signal: AbortSignal,
): Promise<Found> {
  const sorted = (index: MzmlIndex) =>
    [...index.spectra, ...index.chromatograms].map((e) => e.offset).sort((a, b) => a - b);
  const tailStart = Math.max(0, source.size - MZML_TAIL);
  const tail = await source.read(tailStart, source.size - tailStart, signal);
  const { value: found } = await reader.read("mzml_offset", [], tail);
  const at = found.index_offset;
  if (at !== null && at > 0 && at < source.size && source.size - at <= MZML_PART) {
    try {
      const bytes = await source.read(at, source.size - at, signal);
      const { value: index } = await reader.read("mzml_index", [], bytes);
      if (index.spectra.length || index.chromatograms.length) {
        return { source, index, end: at, scanned: false, offsets: sorted(index) };
      }
    } catch (error) {
      if (signal.aborted) throw error;
      // A broken index: scan instead, as readers of mzML do.
    }
  }
  const length = Math.min(source.size, MZML_WHOLE);
  const bytes = await source.read(0, length, signal);
  const complete = length === source.size;
  const { value: index } = await reader.read("mzml_scan", [complete ? 1 : 0], bytes);
  return {
    source,
    index,
    end: complete ? source.size : length,
    scanned: true,
    offsets: sorted(index),
  };
}

/** The bytes of one spectrum or chromatogram: from its start to the next one's. */
export function elementSpan(found: Found, entry: MzmlEntry): { offset: number; length: number } {
  const { offsets } = found;
  let lo = 0;
  let hi = offsets.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if ((offsets[mid] ?? 0) <= entry.offset) lo = mid + 1;
    else hi = mid;
  }
  const next = offsets[lo] ?? found.end;
  const end = Math.min(next > entry.offset ? next : found.end, found.source.size);
  return { offset: entry.offset, length: Math.max(0, end - entry.offset) };
}

async function readElement(
  found: Found,
  entry: MzmlEntry,
  reader: PreviewReader,
  signal: AbortSignal,
): Promise<MzmlElement> {
  const span = elementSpan(found, entry);
  if (span.length > MZML_PART) {
    throw new ReadError(
      `It’s ${formatSize(span.length)}, more than one spectrum the preview reads (${formatSize(MZML_PART)}).`,
    );
  }
  const bytes = await found.source.read(span.offset, span.length, signal);
  const { value } = await reader.read("mzml_element", [MAX_POINTS], bytes);
  return value;
}

function Mzml({ file, stored, reader }: ViewerProps) {
  const read = useRead(
    async (signal) => findSpectra(await openBytes(stored, MZML_WHOLE, signal), reader, signal),
    [stored, reader],
  );
  if (read.phase === "failed") return <ReadFailed file={file} failed={read} />;
  if (read.phase === "reading") return <Loading label={`Finding the spectra in ${file.name}…`} />;
  return <MzmlFound file={file} found={read.value} reader={reader} />;
}

function MzmlFound({
  file,
  found,
  reader,
}: {
  file: ViewerProps["file"];
  found: Found;
  reader: PreviewReader;
}) {
  const { spectra, chromatograms } = found.index;
  const [view, setView] = useState<"spectra" | "chromatograms">(
    spectra.length ? "spectra" : "chromatograms",
  );
  const [spectrum, setSpectrum] = useState(0);
  const [typed, setTyped] = useState("1");
  const [chromatogram, setChromatogram] = useState(0);
  const id = useId();
  const entries = view === "spectra" ? spectra : chromatograms;
  const chosen = view === "spectra" ? spectrum : chromatogram;
  const entry = entries[chosen];
  const go = (to: number) => {
    const next = Math.min(spectra.length - 1, Math.max(0, to));
    setSpectrum(next);
    setTyped(String(next + 1));
  };
  if (!spectra.length && !chromatograms.length) {
    return (
      <>
        <p>
          Iki found no spectra or chromatograms in{" "}
          {found.scanned && found.end < found.source.size
            ? `the first ${formatSize(found.end)} of ${file.name}, and it has no index saying where they are.`
            : `${file.name}.`}
        </p>
        <PreviewNote>
          <Integrity source={found.source} />
        </PreviewNote>
      </>
    );
  }
  return (
    <div className="preview-spectrum">
      <div className="preview-toolbar" role="toolbar" aria-label={`Spectra in ${file.name}`}>
        {spectra.length && chromatograms.length ? (
          <span className="segmented" role="group" aria-label="Show">
            {(["spectra", "chromatograms"] as const).map((v) => (
              <button
                key={v}
                type="button"
                aria-pressed={view === v}
                onClick={() => {
                  setView(v);
                }}
              >
                {v === "spectra"
                  ? `Spectra (${count(spectra.length)})`
                  : `Chromatograms (${count(chromatograms.length)})`}
              </button>
            ))}
          </span>
        ) : null}
        {view === "spectra" ? (
          <>
            <Button
              onClick={() => {
                go(spectrum - 1);
              }}
              disabled={spectrum <= 0}
            >
              Previous spectrum
            </Button>
            <span className="preview-page-field">
              <label htmlFor={`${id}-spectrum`}>Spectrum</label>
              <input
                id={`${id}-spectrum`}
                className="preview-page-input"
                type="number"
                inputMode="numeric"
                min={1}
                max={spectra.length}
                value={typed}
                onChange={(event) => {
                  setTyped(event.target.value);
                  const n = Number(event.target.value);
                  if (Number.isInteger(n) && n >= 1 && n <= spectra.length) setSpectrum(n - 1);
                }}
                onBlur={() => {
                  setTyped(String(spectrum + 1));
                }}
              />
              <span>of {count(spectra.length)}</span>
            </span>
            <Button
              onClick={() => {
                go(spectrum + 1);
              }}
              disabled={spectrum >= spectra.length - 1}
            >
              Next spectrum
            </Button>
          </>
        ) : chromatograms.length > 1 ? (
          <span className="preview-zoom">
            <label htmlFor={`${id}-chromatogram`}>Chromatogram</label>
            <select
              id={`${id}-chromatogram`}
              value={chromatogram}
              onChange={(event) => {
                setChromatogram(Number(event.target.value));
              }}
            >
              {chromatograms.map((c, i) => (
                <option key={c.offset} value={i}>
                  {c.id}
                </option>
              ))}
            </select>
          </span>
        ) : null}
      </div>
      {entry ? (
        <MzmlEntryShown
          key={`${view}-${String(entry.offset)}`}
          found={found}
          entry={entry}
          reader={reader}
        />
      ) : null}
      <PreviewNote>
        <IndexNote found={found} file={file.name} />
        <Integrity source={found.source} />
      </PreviewNote>
    </div>
  );
}

function IndexNote({ found, file }: { found: Found; file: string }) {
  const { index } = found;
  const listed = index.spectra.length;
  if (!found.scanned) {
    return (
      <>
        Read from its index, {plural(listed, "spectrum", "spectra")}
        {index.truncated ? `, the first ${count(listed)} it lists` : ""}.{" "}
      </>
    );
  }
  if (index.truncated || found.end < found.source.size) {
    return (
      <>
        {file} has no index, so Iki looked through its first {formatSize(found.end)} of{" "}
        {formatSize(found.source.size)}: {plural(listed, "spectrum", "spectra")}
        {index.declared_spectra !== null
          ? ` of the ${count(index.declared_spectra)} it says it has`
          : ""}
        .{" "}
      </>
    );
  }
  return <>It has no index; Iki looked through all of it. </>;
}

function MzmlEntryShown({
  found,
  entry,
  reader,
}: {
  found: Found;
  entry: MzmlEntry;
  reader: PreviewReader;
}) {
  const read = useRead((signal) => readElement(found, entry, reader, signal), [found, entry]);
  if (read.phase === "reading") return <Loading label={`Reading ${entry.id}…`} />;
  if (read.phase === "failed") {
    return (
      <p className="alert alert-warning" role="alert">
        Iki couldn’t read {entry.id}. {read.message}
      </p>
    );
  }
  const element = read.value;
  return element.kind === "spectrum" ? (
    <SpectrumShown spectrum={element} />
  ) : (
    <ChromatogramShown element={element} />
  );
}

function minutes(value: number): string {
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 3 })} min`;
}

function SpectrumShown({ spectrum }: { spectrum: MzmlSpectrum }) {
  const series = useMemo<Series>(
    () => ({
      x: spectrum.mz,
      y: spectrum.intensity,
      kind: spectrum.centroided ? "sticks" : "line",
    }),
    [spectrum],
  );
  const facts: [string, string][] = [
    ["ID", spectrum.id],
    ...(spectrum.ms_level !== null
      ? [["MS level", `MS${String(spectrum.ms_level)}`] as [string, string]]
      : []),
    ...(spectrum.scan_start_minutes !== null
      ? [["Retention time", minutes(spectrum.scan_start_minutes)] as [string, string]]
      : []),
    ...(spectrum.polarity ? [["Polarity", spectrum.polarity] as [string, string]] : []),
    ...(spectrum.centroided !== null
      ? [["Data", spectrum.centroided ? "Centroid" : "Profile"] as [string, string]]
      : []),
    ...(spectrum.precursor_mz.length
      ? [
          [
            spectrum.precursor_mz.length === 1 ? "Precursor m/z" : "Precursors m/z",
            spectrum.precursor_mz.map((m) => m.toFixed(4)).join(", "),
          ] as [string, string],
        ]
      : []),
    ...(spectrum.base_peak_mz !== null
      ? [["Base peak m/z", spectrum.base_peak_mz.toFixed(4)] as [string, string]]
      : []),
    ...(spectrum.total_ion_current !== null
      ? [["TIC", spectrum.total_ion_current.toExponential(3)] as [string, string]]
      : []),
    ["Points", count(spectrum.points)],
  ];
  return (
    <>
      <dl className="preview-facts">
        {facts.map(([name, value]) => (
          <div key={name}>
            <dt>{name}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      {spectrum.mz.length ? (
        <>
          <Plot series={series} xLabel="m/z" yLabel="Intensity" label={`Spectrum ${spectrum.id}`} />
          <Peaks series={series} xLabel="m/z" yLabel="Intensity" />
        </>
      ) : (
        <p>It has no points to plot.</p>
      )}
      {spectrum.truncated ? (
        <PreviewNote>
          Showing its first {count(spectrum.mz.length)} of {count(spectrum.points)} points.
        </PreviewNote>
      ) : null}
    </>
  );
}

function ChromatogramShown({
  element,
}: {
  element: Extract<MzmlElement, { kind: "chromatogram" }>;
}) {
  const series = useMemo<Series>(
    () => ({ x: element.time_minutes, y: element.intensity, kind: "line" }),
    [element],
  );
  return (
    <>
      <dl className="preview-facts">
        <div>
          <dt>ID</dt>
          <dd>{element.id}</dd>
        </div>
        {element.chromatogram_type ? (
          <div>
            <dt>Type</dt>
            <dd>{element.chromatogram_type}</dd>
          </div>
        ) : null}
        <div>
          <dt>Points</dt>
          <dd>{count(element.points)}</dd>
        </div>
      </dl>
      {element.time_minutes.length ? (
        <>
          <Plot
            series={series}
            xLabel="Time (min)"
            yLabel="Intensity"
            label={`Chromatogram ${element.id}`}
          />
          <Peaks series={series} xLabel="Time (min)" yLabel="Intensity" />
        </>
      ) : (
        <p>It has no points to plot.</p>
      )}
      {element.truncated ? (
        <PreviewNote>
          Showing its first {count(element.time_minutes.length)} of {count(element.points)} points.
        </PreviewNote>
      ) : null}
    </>
  );
}
