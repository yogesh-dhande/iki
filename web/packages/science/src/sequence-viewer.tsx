import { useWindowVirtualizer } from "@tanstack/react-virtual";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
  type RefObject,
  type SetStateAction,
} from "react";

import {
  AT_START,
  caretRow,
  columnOf,
  complement,
  extremes,
  featureName,
  findMotif,
  gcContent,
  GROUP,
  kindOf,
  lanedApart,
  lanesPerRow,
  lengthText,
  locationText,
  LONGEST_TM,
  meltingTemperature,
  moved,
  piecesIn,
  placeAt,
  rangesOf,
  readFeatures,
  readMotif,
  readTrack,
  residuesOf,
  reverseComplement,
  ROW,
  covered,
  selected,
  stepTo,
  stretchLength,
  trackScale,
  type Caret,
  type Feature,
  type Kind,
  type Match,
  type Values,
} from "./sequence.js";
import { viewerThemeUrl } from "./viewer-theme.js";
import {
  FRAME_CODES,
  FRAMES,
  GENETIC_CODES,
  tableOf,
  translateFeature,
  translateFrame,
  type Codon,
  type Frame,
} from "./translation.js";
import { defaultFeatureColors } from "./feature-colors.js";
import { ReactElement } from "./react-element.js";
import type { Alphabet } from "./sequence.js";

export interface FeatureRef {
  type: string;
  start: number;
  end: number;
  name?: string;
}

export interface SequenceFeature extends FeatureRef {
  strand?: "forward" | "reverse" | "none" | "mixed";
  parts?: {
    start: number;
    end: number;
    strand?: "forward" | "reverse" | "none";
    partial_start?: boolean;
    partial_end?: boolean;
    between?: boolean;
  }[];
  operator?: "join" | "order";
  qualifiers?: Record<string, string>;
}

export interface SequenceTrack {
  id: string;
  label: string;
  values: (number | null)[] | null;
  min?: number | null;
  max?: number | null;
}

export interface SequenceData {
  sequence: string;
  title: string;
  alphabet: Alphabet;
  label?: string;
  topology?: "linear" | "circular";
  strands?: "single" | "double";
  features?: SequenceFeature[];
  /** Supplied library annotations. Omit features until a search has run. */
  library?: { features?: readonly SequenceFeature[]; version?: string };
  tracks?: SequenceTrack[];
  feature?: FeatureRef;
}

export interface SequenceSelection {
  /** Zero-based insertion point, from before the first residue to after the last. */
  caret: number;
  /** One-based inclusive endpoints. A reversed interval crosses the origin. */
  range: [number, number] | null;
}

/** A track as drawn: its values, one per residue, and the scale they're drawn on. */
interface Drawn {
  field: string;
  label: string;
  values: Values;
  scale: [number, number];
}
const KIND_LABELS: Record<Kind, string> = {
  coding: "Coding",
  regulatory: "Regulatory",
  terminator: "Terminator",
  origin: "Origin",
  binding: "Binding site",
  other: "Other",
};

const number = new Intl.NumberFormat();
const decimal = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

function featureRef(feature: Feature): FeatureRef {
  return {
    type: feature.type,
    start: feature.start,
    end: feature.end,
    ...(feature.name ? { name: feature.name } : {}),
  };
}

/** Whether a feature is the one a view names: the same kind, place and name. */
function sameFeature(feature: Feature, ref: FeatureRef): boolean {
  return (
    feature.type === ref.type &&
    feature.start === ref.start &&
    feature.end === ref.end &&
    feature.name === (ref.name ?? "")
  );
}

const tenths = new Intl.NumberFormat(undefined, {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

function SequenceView({
  data,
  reportFeature,
  reportSelection,
}: {
  data: SequenceData;
  reportFeature: (feature: FeatureRef | null) => void;
  reportSelection: (selection: SequenceSelection) => void;
}) {
  const ids = useId();
  const { sequence, alphabet, feature } = data;
  const { features, recordedFeatures, libraryFeatures } = useMemo(() => {
    const recorded: readonly unknown[] = Array.isArray(data.features) ? data.features : [];
    const annotated: readonly unknown[] = Array.isArray(data.library?.features)
      ? data.library.features
      : [];
    const features = readFeatures([...recorded, ...annotated], sequence.length);
    return {
      features,
      recordedFeatures: features.filter((f) => f.index < recorded.length),
      libraryFeatures: features.filter((f) => f.index >= recorded.length),
    };
  }, [data.features, data.library?.features, sequence.length]);
  // A protein's stops (*) are no residues, inside it (MK*V is 3) or ending it (M** is 1): its
  // values are one a residue up to the stops that end it, and none for those.
  const protein = alphabet === "protein";
  const residues = protein ? sequence.replaceAll("*", "").length : sequence.length;
  const valued = protein ? sequence.replace(/\*+$/, "").length : sequence.length;
  const tracks = useMemo(
    () =>
      (data.tracks ?? []).map((track) => {
        const given = readTrack(track.values, valued);
        const ending = sequence.length - valued;
        const values = given && ending ? [...given, ...Array<null>(ending).fill(null)] : given;
        return {
          ...track,
          given: track.values != null,
          drawn: values
            ? {
                field: track.id,
                label: track.label,
                values,
                scale: trackScale(values, track.min ?? null, track.max ?? null),
              }
            : null,
        };
      }),
    [data.tracks, sequence.length, valued],
  );
  const drawn = useMemo(() => tracks.flatMap((t) => (t.drawn ? [t.drawn] : [])), [tracks]);
  const [shown, setShown] = useState<Feature | null>(() =>
    feature ? (features.find((f) => sameFeature(f, feature)) ?? null) : null,
  );
  const [at, setAt] = useState<Caret>(AT_START);
  const circular = data.topology === "circular";
  // What copying said, of the selection it copied: once that changes, it's old news.
  const [copying, setCopying] = useState<{ said: string; of: Caret } | null>(null);
  const said = copying?.of === at ? copying.said : "";
  // A duplex's other strand, drawn under it 3′ to 5′, unless it's hidden.
  const double = !protein && data.strands === "double";
  const [pairing, setPairing] = useState(true);
  const region = useRef<HTMLDivElement | null>(null);
  const tools = useRef<HTMLDivElement | null>(null);
  // A row to bring into view, asked for afresh each time (a new object): the rows' own
  // scrolling does it.
  const [revealed, setRevealed] = useState<{ row: number } | null>(null);
  // Finding a motif: what's typed, where it's looked for, and the match gone to.
  const [typed, setTyped] = useState("");
  const [bothStrands, setBothStrands] = useState(true);
  const [aroundOrigin, setAroundOrigin] = useState(circular);
  const [found, setFound] = useState<{ index: number; round: "start" | "end" | null } | null>(null);
  const motif = useMemo(() => readMotif(typed, alphabet), [typed, alphabet]);
  const search = useMemo(
    () =>
      motif && "motif" in motif
        ? findMotif(sequence, motif.motif, {
            alphabet: alphabet,
            bothStrands,
            circular: aroundOrigin,
          })
        : { matches: [], more: false },
    [motif, sequence, alphabet, bothStrands, aroundOrigin],
  );
  // Translations: each coding sequence's, by the code it names, and reading frames by one
  // chosen, which is the coding sequences' if they agree on one.
  const nucleic = !protein;
  const coding = useMemo(
    () =>
      nucleic
        ? lanedApart(
            recordedFeatures.filter((f) => f.type.toLowerCase() === "cds"),
            sequence.length,
          ).map((f) => ({ feature: f, read: translateFeature(f, sequence) }))
        : [],
    [nucleic, recordedFeatures, sequence],
  );
  const [translating, setTranslating] = useState(true);
  const [frames, setFrames] = useState<Frame[]>([]);
  const [frameTable, setFrameTable] = useState(() => {
    const named = new Set(coding.map((c) => tableOf(c.feature)));
    const [only] = named;
    return named.size === 1 && only !== undefined && FRAME_CODES.includes(only) ? only : 1;
  });
  const lanes = useMemo(() => {
    const out: Lane[] = [];
    if (translating) {
      for (const { feature: f, read } of coding) {
        if ("error" in read) continue;
        out.push(laneOf(`cds-${String(f.index)}`, featureName(f), read.codons, f));
      }
    }
    for (const frame of frames) {
      const label = frameLabel(frame);
      out.push(
        laneOf(`frame-${label}`, `frame ${label}`, translateFrame(sequence, frame, frameTable)),
      );
    }
    return out;
  }, [translating, coding, frames, frameTable, sequence]);
  const untranslated = coding.flatMap(({ feature: f, read }) =>
    "error" in read ? [`${featureName(f)} (${read.error})`] : [],
  );
  useEffect(() => {
    reportFeature(shown ? featureRef(shown) : null);
  }, [shown, reportFeature]);
  useEffect(() => {
    reportSelection({ caret: at.caret, range: selected(at) });
  }, [at, reportSelection]);
  const unit = unitOf(alphabet, data.strands);
  if (!sequence) {
    return <p className="muted">It has no {data.label?.toLowerCase() ?? "sequence"} yet.</p>;
  }
  const span = selected(at);
  const { matches } = search;

  /** Goes to the `index`th match: selects it, so the readout says what it is, and brings it
   * into view. */
  function goTo(index: number, round: "start" | "end" | null) {
    const match = matches[index];
    if (!match) return;
    setAt({
      anchor: match.start - 1,
      caret: match.end,
      across: match.end < match.start,
      atEnd: match.end % ROW === 0,
    });
    setFound({ index, round: matches.length > 1 ? round : null });
    setRevealed({ row: Math.floor((match.start - 1) / ROW) });
  }

  /** Goes to the next match, or the one before: from the one gone to, else from the caret,
   * round past either end as find on a page does. */
  function step(direction: 1 | -1) {
    if (!matches.length) return;
    if (found && matches[found.index]) {
      const next = found.index + direction;
      const index = (next + matches.length) % matches.length;
      goTo(index, next >= matches.length ? "start" : next < 0 ? "end" : null);
      return;
    }
    const next = stepTo(matches, at.caret, direction);
    if (next) goTo(next.index, next.wrapped ? (direction === 1 ? "start" : "end") : null);
  }

  /** Copies what's selected, or its reverse complement, and says so. */
  function copy(reverse: boolean) {
    if (!span) return;
    const bases = residuesOf(sequence, span[0], span[1]);
    const count = number.format(bases.length);
    const noun = residueNoun(alphabet, bases.length);
    const of = at;
    void copied(reverse ? reverseComplement(bases, alphabet) : bases).then((ok) => {
      setCopying({
        of,
        said: !ok
          ? "The browser didn’t allow copying."
          : reverse
            ? `Copied the reverse complement of ${count} ${noun}.`
            : `Copied ${count} ${noun}.`,
      });
    });
  }

  const kinds = [...new Set(features.map((f) => kindOf(f.type)))];
  return (
    <>
      <p className="muted">
        {number.format(residues)} {unit}
        {circular ? " · circular" : ""}
        {data.features || libraryFeatures.length
          ? ` · ${String(features.length)} feature${features.length === 1 ? "" : "s"}`
          : ""}
      </p>
      {kinds.length ? (
        <ul className="feature-legend" aria-label="Feature kinds">
          {kinds.map((kind) => (
            <li key={kind}>
              <span className={`feature-swatch feature-${kind}`} aria-hidden="true" />
              {KIND_LABELS[kind]}
            </li>
          ))}
        </ul>
      ) : null}
      {tracks.length ? (
        <ul className="track-legend" aria-label="Tracks">
          {tracks.map((track) => (
            <li key={track.id}>
              <TrackLegend label={track.label} drawn={track.drawn} given={track.given} />
            </li>
          ))}
        </ul>
      ) : null}
      {nucleic ? (
        <div className="sequence-options">
          {double ? (
            <label className="choice" htmlFor={`${ids}-pairing`}>
              <input
                id={`${ids}-pairing`}
                type="checkbox"
                role="switch"
                checked={pairing}
                onChange={(event) => {
                  setPairing(event.target.checked);
                }}
              />
              <span className="choice-text">Complementary strand, 3′ to 5′</span>
            </label>
          ) : null}
          {coding.length ? (
            <label className="choice" htmlFor={`${ids}-translating`}>
              <input
                id={`${ids}-translating`}
                type="checkbox"
                role="switch"
                checked={translating}
                onChange={(event) => {
                  setTranslating(event.target.checked);
                }}
              />
              <span className="choice-text">Coding sequences’ translations</span>
            </label>
          ) : null}
          <fieldset className="sequence-frames">
            <legend>Reading frames</legend>
            {FRAMES.map((frame) => {
              const label = frameLabel(frame);
              return (
                <label
                  key={frame}
                  className="sequence-find-option"
                  htmlFor={`${ids}-frame${String(frame)}`}
                >
                  <input
                    id={`${ids}-frame${String(frame)}`}
                    type="checkbox"
                    checked={frames.includes(frame)}
                    onChange={(event) => {
                      const on = event.target.checked;
                      setFrames((now) =>
                        FRAMES.filter((f) => (f === frame ? on : now.includes(f))),
                      );
                    }}
                  />
                  {label}
                </label>
              );
            })}
            <label className="sequence-find-option" htmlFor={`${ids}-code`}>
              by
              <select
                id={`${ids}-code`}
                value={frameTable}
                onChange={(event) => {
                  setFrameTable(Number(event.target.value));
                }}
              >
                {FRAME_CODES.map((n) => (
                  <option key={n} value={n}>
                    {n}. {GENETIC_CODES[n]?.name ?? ""}
                  </option>
                ))}
              </select>
            </label>
          </fieldset>
          {translating && untranslated.length ? (
            <p className="muted sequence-untranslated">
              Not translated: {untranslated.join("; ")}.
            </p>
          ) : null}
        </div>
      ) : null}
      <div className="sequence-tools" ref={tools}>
        <FindBar
          id={`${ids}-find`}
          alphabet={alphabet}
          typed={typed}
          motif={motif}
          matches={matches}
          more={search.more}
          found={found}
          bothStrands={bothStrands}
          aroundOrigin={aroundOrigin}
          onType={(text) => {
            setTyped(text);
            setFound(null);
          }}
          onBothStrands={(on) => {
            setBothStrands(on);
            setFound(null);
          }}
          onAroundOrigin={(on) => {
            setAroundOrigin(on);
            setFound(null);
          }}
          onStep={step}
        />
        <div className="sequence-selection">
          <p id={`${ids}-at`} className="sequence-at" aria-live="polite">
            {span ? (
              <Described
                sequence={sequence}
                alphabet={alphabet}
                unit={unit}
                start={span[0]}
                end={span[1]}
              />
            ) : (
              caretText(sequence, alphabet, at.caret, double, lanes)
            )}
          </p>
          <div className="sequence-copy">
            <button
              type="button"
              className="button"
              disabled={!span}
              onClick={() => {
                copy(false);
              }}
            >
              Copy
            </button>
            {protein ? null : (
              <button
                type="button"
                className="button"
                disabled={!span}
                onClick={() => {
                  copy(true);
                }}
              >
                Copy reverse complement
              </button>
            )}
            <span role="status" className={said ? undefined : "visually-hidden"}>
              {said}
            </span>
          </div>
        </div>
      </div>
      <Residues
        sequence={sequence}
        alphabet={alphabet}
        features={features}
        tracks={drawn}
        shown={shown}
        paired={double && pairing}
        lanes={lanes}
        label={`${data.label ?? "Sequence"} of ${data.title}`}
        at={at}
        onMove={(next) => {
          setAt(next);
          setFound(null);
        }}
        matches={matches}
        current={found ? (matches[found.index] ?? null) : null}
        onStep={step}
        revealed={revealed}
        onCopy={() => {
          copy(false);
        }}
        region={region}
        tools={tools}
        describedBy={`${ids}-keys ${ids}-at`}
      />
      <p id={`${ids}-keys`} className="visually-hidden">
        The arrow keys move the caret along the {protein ? "residues" : "bases"}, and with Shift
        select them; Home and End go to a row’s start and end, and with Control to the sequence’s;
        Control+A selects it all, Control+C copies the selection, and Escape clears it.
        {matches.length ? " N and Shift+N go to the next and previous match." : null}
      </p>
      {data.features ? (
        <FeatureTable
          features={recordedFeatures}
          length={sequence.length}
          shown={shown}
          onShow={setShown}
        />
      ) : null}
      {data.library ? (
        <FeatureTable
          features={libraryFeatures}
          length={sequence.length}
          shown={shown}
          onShow={setShown}
          library={{
            id: `${ids}-library`,
            searched: Array.isArray(data.library.features),
            version: data.library.version ?? null,
          }}
        />
      ) : null}
    </>
  );
}

/** A box to find a motif in, with what it found and buttons to step through it. */
function FindBar({
  id,
  alphabet,
  typed,
  motif,
  matches,
  more,
  found,
  bothStrands,
  aroundOrigin,
  onType,
  onBothStrands,
  onAroundOrigin,
  onStep,
}: {
  id: string;
  alphabet: Alphabet;
  typed: string;
  motif: ReturnType<typeof readMotif>;
  matches: Match[];
  more: boolean;
  found: { index: number; round: "start" | "end" | null } | null;
  bothStrands: boolean;
  aroundOrigin: boolean;
  onType: (text: string) => void;
  onBothStrands: (on: boolean) => void;
  onAroundOrigin: (on: boolean) => void;
  onStep: (direction: 1 | -1) => void;
}) {
  const protein = alphabet === "protein";
  const error = motif && "error" in motif ? motif.error : null;
  const current = found ? matches[found.index] : undefined;
  let status = "";
  if (error) status = error;
  else if (current && found) {
    const noun = protein ? "residues" : "bases";
    const strand = protein
      ? ""
      : current.strand === "both"
        ? ", on both strands"
        : `, on the ${current.strand} strand`;
    const across = current.end < current.start ? ", across the origin" : "";
    const round = found.round ? `, back at the ${found.round}` : "";
    status = `Match ${number.format(found.index + 1)} of ${number.format(matches.length)}${more ? "+" : ""}: ${noun} ${number.format(current.start)}–${number.format(current.end)}${strand}${across}${round}.`;
  } else if (motif) {
    status = matches.length
      ? more
        ? `More than ${number.format(matches.length)} matches: the first ${number.format(matches.length)} are marked.`
        : `${number.format(matches.length)} ${matches.length === 1 ? "match" : "matches"}.`
      : "No matches.";
  }
  return (
    <div
      className="sequence-find"
      role="search"
      aria-label={`Find ${protein ? "residues" : "bases"}`}
    >
      <label htmlFor={id}>Find</label>
      <input
        id={id}
        type="search"
        className="sequence-find-input"
        value={typed}
        autoComplete="off"
        autoCapitalize="characters"
        spellCheck={false}
        placeholder={protein ? "Residues, X for any" : "Bases, in IUPAC codes"}
        aria-invalid={error ? true : undefined}
        aria-describedby={`${id}-found`}
        onChange={(event) => {
          onType(event.target.value);
        }}
        onKeyDown={(event) => {
          if (event.key !== "Enter") return;
          event.preventDefault();
          onStep(event.shiftKey ? -1 : 1);
        }}
      />
      <button
        type="button"
        className="button"
        disabled={!matches.length}
        onClick={() => {
          onStep(-1);
        }}
      >
        Previous
      </button>
      <button
        type="button"
        className="button"
        disabled={!matches.length}
        onClick={() => {
          onStep(1);
        }}
      >
        Next
      </button>
      {protein ? null : (
        <>
          <label className="sequence-find-option" htmlFor={`${id}-strands`}>
            <input
              id={`${id}-strands`}
              type="checkbox"
              checked={bothStrands}
              onChange={(event) => {
                onBothStrands(event.target.checked);
              }}
            />
            Both strands
          </label>
          <label className="sequence-find-option" htmlFor={`${id}-origin`}>
            <input
              id={`${id}-origin`}
              type="checkbox"
              checked={aroundOrigin}
              onChange={(event) => {
                onAroundOrigin(event.target.checked);
              }}
            />
            Across the origin
          </label>
        </>
      )}
      <p
        id={`${id}-found`}
        role="status"
        className={`sequence-found${error ? " sequence-found-error" : ""}`}
      >
        {status}
      </p>
    </div>
  );
}

/** What a residue is called, and many of them, by what it's a sequence of. */
function residueNoun(alphabet: Alphabet, count = 1): string {
  const one = alphabet === "protein" ? "residue" : "base";
  return count === 1 ? one : `${one}s`;
}

/** Copies `text`, saying whether it could. */
async function copied(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Where the caret is: before which residue, and what it is. */
function caretText(
  sequence: string,
  alphabet: Alphabet,
  caret: number,
  double: boolean,
  lanes: Lane[] = [],
): string {
  const noun = residueNoun(alphabet);
  if (caret >= sequence.length) {
    return `Caret after ${noun} ${number.format(sequence.length)}, the last.`;
  }
  const base = sequence.charAt(caret);
  const pairs = double ? `, paired with ${complement(base, alphabet)}` : "";
  // What the base reads as in each translation shown, so a screen reader hears the lanes.
  const reads = lanes.flatMap((lane) => {
    const codon = lane.byBase.get(caret + 1);
    if (!codon) return [];
    const what = codon.residue === "*" ? "a stop" : codon.residue;
    return lane.feature
      ? [`in ${lane.label}, codon ${number.format(codon.number)}, ${codon.codon}: ${what}`]
      : [`in ${lane.label}, ${codon.codon}: ${what}`];
  });
  return `Caret before ${noun} ${number.format(caret + 1)}: ${base}${pairs}${reads.length ? `; ${reads.join("; ")}` : ""}.`;
}

/** A translation lane as it's drawn: its codons in order along the sequence (by their
 * middle base, which a residue is drawn over), and each base's codon. */
interface Lane {
  key: string;
  label: string;
  codons: Codon[];
  byBase: Map<number, Codon>;
  /** The coding sequence it translates, laned among the others; none for a frame. */
  feature?: Feature;
}

function laneOf(key: string, label: string, codons: Codon[], feature?: Feature): Lane {
  const byBase = new Map<number, Codon>();
  for (const codon of codons) for (const b of codon.bases) byBase.set(b, codon);
  const sorted = [...codons].sort((a, b) => middleOf(a) - middleOf(b));
  return { key, label, codons: sorted, byBase, ...(feature ? { feature } : {}) };
}

/** The base a codon's residue is drawn over: its middle one, or its first if it has fewer. */
function middleOf(codon: Codon): number {
  return codon.bases[1] ?? codon.bases[0] ?? 0;
}

/** +1, +2, +3, −1, −2, −3, with a minus sign. */
function frameLabel(frame: Frame): string {
  return frame > 0 ? `+${String(frame)}` : `−${String(-frame)}`;
}

/** A lane's codons drawn over the row from `start` to `end`. */
function codonsIn(lane: Lane, start: number, end: number): ReactNode[] {
  const { codons } = lane;
  let low = 0;
  let high = codons.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    const at = codons[mid];
    if (at && middleOf(at) < start) low = mid + 1;
    else high = mid;
  }
  const out: ReactNode[] = [];
  for (let i = low; i < codons.length; i++) {
    const codon = codons[i];
    if (!codon) break;
    const middle = middleOf(codon);
    if (middle > end) break;
    // Over its bases in this row when they're side by side; over its middle one when a
    // join's intron or the row's end parts them.
    const here = codon.bases.filter((b) => b >= start && b <= end);
    const from = Math.min(...here);
    const to = Math.max(...here);
    const together = to - from + 1 === here.length;
    const a = together ? from : middle;
    const b = together ? to : middle;
    const left = columnOf(a - start);
    const kind =
      codon.residue === "*" ? " codon-stop" : codon.residue === "M" ? " codon-start" : "";
    out.push(
      <span
        key={`${String(codon.number)}-${String(middle)}`}
        className={`codon codon-${codon.number % 2 ? "odd" : "even"}${kind}`}
        title={`${lane.label}${lane.feature ? `, codon ${String(codon.number)}` : ""}: ${codon.codon}, ${codon.residue}`}
        style={{ left: `${String(left)}ch`, width: `${String(columnOf(b - start) - left + 1)}ch` }}
      >
        {codon.residue}
      </span>,
    );
  }
  return out;
}

/** What a selection is: where, how long, and for DNA its GC content and Tm, as the DNA
 * plugin computes them for a sequence and a primer. The Tm says its conditions, since a
 * primer also carries a Tm in PCR buffer that reads several degrees higher (ADR 0201). */
function Described({
  sequence,
  alphabet,
  unit,
  start,
  end,
}: {
  sequence: string;
  alphabet: Alphabet;
  unit: string;
  start: number;
  end: number;
}) {
  const bases = residuesOf(sequence, start, end);
  const count = stretchLength(start, end, sequence.length);
  const facts = [`${number.format(count)} ${unit}`];
  if (alphabet !== "protein") {
    const gc = gcContent(bases);
    if (gc !== null) facts.push(`GC ${decimal.format(gc)}%`);
  }
  if (alphabet === "dna" && count > 1) {
    const tm = count <= LONGEST_TM ? meltingTemperature(bases) : null;
    if (tm !== null) facts.push(`Tm ${tenths.format(tm)} °C (50 mM Na⁺)`);
    else if (count > LONGEST_TM) facts.push(`no Tm past ${String(LONGEST_TM)} bases`);
    else facts.push("no Tm with bases other than A, C, G and T");
  }
  return (
    <>
      <strong>
        {alphabet === "protein" ? "Residues" : "Bases"} {number.format(start)}–{number.format(end)}
      </strong>
      {end < start ? ", across the origin," : ""} selected: {facts.join(" · ")}
    </>
  );
}

/** How a sequence's length is counted, by what it's a sequence of, when its record
 * doesn't say how many strands it has: DNA in base pairs, RNA in nucleotides. */
const UNITS: Record<Alphabet, string> = { dna: "bp", rna: "nt", protein: "residues" };

/** How a record's sequence length is counted: a duplex's in base pairs (bp) and a single
 * strand's in nucleotides (nt), as the field its viewer names says it is; otherwise as
 * its alphabet usually is. */
function unitOf(alphabet: Alphabet, strands?: SequenceData["strands"]): string {
  if (alphabet === "protein") return UNITS.protein;
  if (strands === "single") return "nt";
  if (strands === "double") return "bp";
  return UNITS[alphabet];
}

/** A layer of marks over the residues, as ranges from 1, inclusive, none across the origin:
 * the feature shown, what's selected. */
interface Layer {
  className: string;
  ranges: [number, number][];
}

/** The place between residues a pointer is at, and whether it's at the end of its row. */
function placeOf(
  event: PointerEvent<HTMLElement>,
  length: number,
): { place: number; atEnd: boolean } | null {
  // While a drag holds the pointer, its events are the rows' own: what's under it says
  // which row it's over.
  const target =
    event.target instanceof Element && event.target !== event.currentTarget
      ? event.target
      : document.elementFromPoint(event.clientX, event.clientY);
  const row = target?.closest<HTMLElement>(".sequence-row");
  const line = row?.querySelector<HTMLElement>(".sequence-residues");
  const group = line?.querySelector<HTMLElement>(".sequence-group");
  if (!row || !line || !group) return null;
  const ch = group.getBoundingClientRect().width / GROUP;
  if (!ch) return null;
  const rowStart = Number(row.dataset["index"]) * ROW + 1;
  const place = placeAt((event.clientX - line.getBoundingClientRect().left) / ch, rowStart, length);
  return { place, atEnd: place > rowStart - 1 && place === Math.min(rowStart - 1 + ROW, length) };
}

/** The sequence in rows of 60, in groups of 10, with each row's features under it. Rows
 * off screen aren't drawn, so a genome reads as quickly as a primer. */
function Residues({
  sequence,
  alphabet,
  features,
  tracks,
  shown,
  paired,
  lanes: translated,
  label,
  at,
  onMove,
  matches,
  current,
  onStep,
  revealed,
  onCopy,
  region,
  tools,
  describedBy,
}: {
  sequence: string;
  alphabet: Alphabet;
  features: readonly Feature[];
  tracks: Drawn[];
  shown: Feature | null;
  /** Whether to draw the complementary strand under each row. */
  paired: boolean;
  /** Translation lanes: coding sequences', then reading frames'. */
  lanes: Lane[];
  label: string;
  at: Caret;
  onMove: Dispatch<SetStateAction<Caret>>;
  /** Where a motif looked for was found, each marked, and the one gone to. */
  matches: Match[];
  current: Match | null;
  onStep: (direction: 1 | -1) => void;
  revealed: { row: number } | null;
  onCopy: () => void;
  region: RefObject<HTMLDivElement | null>;
  /** What stays at the top of the window over the rows, which a row brought into view
   * mustn't be under. */
  tools: RefObject<HTMLDivElement | null>;
  describedBy: string;
}) {
  const length = sequence.length;
  // Where the rows begin on the page, which the window's scrolling is measured from: it
  // moves as what's above them does.
  const [margin, setMargin] = useState(0);
  const [overlaid, setOverlaid] = useState(0);
  useLayoutEffect(() => {
    const measure = () => {
      const top = (region.current?.getBoundingClientRect().top ?? 0) + window.scrollY;
      setMargin((was) => (Math.abs(top - was) >= 1 ? top : was));
      setOverlaid(tools.current?.getBoundingClientRect().height ?? 0);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    // What's above the rows changes height (the readout wraps, a message shows), and the
    // page's height changes with it.
    const observer = new ResizeObserver(measure);
    observer.observe(document.body);
    return () => {
      observer.disconnect();
    };
  }, [region, tools]);
  const rows = Math.ceil(length / ROW);
  const lanes = useMemo(() => lanesPerRow(features, length), [features, length]);
  const coding = translated.filter((lane) => lane.feature);
  const reading = translated.filter((lane) => !lane.feature);
  const codingLanes = useMemo(
    () =>
      lanesPerRow(
        translated.flatMap((lane) => (lane.feature ? [lane.feature] : [])),
        length,
      ),
    [translated, length],
  );
  const other = useMemo(
    () => (paired ? complement(sequence, alphabet) : ""),
    [paired, sequence, alphabet],
  );
  const virtualizer = useWindowVirtualizer({
    count: rows,
    // A line of residues, the other strand's, a lane per feature row under it, its tracks,
    // and the gap between rows.
    estimateSize: (row) =>
      30 +
      (paired ? 20 : 0) +
      ((lanes[row] ?? 0) + (codingLanes[row] ?? 0) + reading.length) * 20 +
      tracks.length * 44,
    overscan: 8,
    scrollMargin: margin,
    scrollPaddingStart: overlaid,
  });
  // Showing a feature brings its first row into view.
  useEffect(() => {
    if (shown) virtualizer.scrollToIndex(Math.floor((shown.start - 1) / ROW), { align: "center" });
  }, [shown, virtualizer]);
  const [focused, setFocused] = useState(false);
  const dragging = useRef(false);
  useEffect(() => {
    if (revealed) virtualizer.scrollToIndex(revealed.row, { align: "center" });
  }, [revealed, virtualizer]);
  // Each row's matches, so a row marks its own without reading them all.
  const hits = useMemo(() => {
    const rows = new Map<number, [number, number][]>();
    for (const m of matches) {
      for (const [a, b] of rangesOf(m.start, m.end, length)) {
        for (let row = Math.floor((a - 1) / ROW); row <= Math.floor((b - 1) / ROW); row++) {
          const listed = rows.get(row);
          if (listed) listed.push([a, b]);
          else rows.set(row, [[a, b]]);
        }
      }
    }
    return rows;
  }, [matches, length]);
  const span = selected(at);
  const layersOf = (row: number): Layer[] => [
    { className: "residue-shown", ranges: shown ? covered(shown, length) : [] },
    { className: "residue-hit", ranges: hits.get(row) ?? [] },
    {
      className: "residue-current",
      ranges: current ? rangesOf(current.start, current.end, length) : [],
    },
    { className: "residue-selected", ranges: span ? rangesOf(span[0], span[1], length) : [] },
  ];
  const caretAt = caretRow(at, length);
  const digits = String(length).length;

  function keyed(event: KeyboardEvent<HTMLDivElement>) {
    if (event.target !== event.currentTarget) return;
    const command = event.ctrlKey || event.metaKey;
    if (command && !event.shiftKey && event.key.toLowerCase() === "c") {
      if (!span) return;
      event.preventDefault();
      onCopy();
      return;
    }
    const plain = !command && !event.altKey;
    if (plain && matches.length && event.key.toLowerCase() === "n") {
      event.preventDefault();
      onStep(event.shiftKey ? -1 : 1);
      return;
    }
    if (event.key === "Escape" && span) {
      event.preventDefault();
      onMove({ anchor: at.caret, caret: at.caret, across: false, atEnd: at.atEnd ?? false });
      return;
    }
    // A page is what the window shows, a row being about three lines.
    const page = Math.max(Math.floor(window.innerHeight / 60) - 1, 1);
    const next = moved(at, event, length, page);
    if (!next) return;
    event.preventDefault();
    onMove(next);
    virtualizer.scrollToIndex(caretRow(next, length), { align: "auto" });
  }

  function pressed(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    // Not the browser's own selection of the text: the rows' own.
    event.preventDefault();
    region.current?.focus({ preventScroll: true });
    // A feature's bar selects all of it.
    const bar =
      event.target instanceof Element ? event.target.closest<HTMLElement>("[data-feature]") : null;
    const picked = bar ? features.find((f) => String(f.index) === bar.dataset["feature"]) : null;
    if (picked) {
      onMove({
        anchor: picked.start - 1,
        caret: picked.end,
        across: picked.end < picked.start,
        atEnd: picked.end % ROW === 0,
      });
      return;
    }
    const found = placeOf(event, length);
    if (!found) return;
    const { place, atEnd } = found;
    const extend = event.shiftKey;
    onMove((now) => ({ anchor: extend ? now.anchor : place, caret: place, across: false, atEnd }));
    dragging.current = true;
    // So a drag past the rows' edge goes on selecting (not every DOM has it: jsdom).
    if ("setPointerCapture" in event.currentTarget) {
      event.currentTarget.setPointerCapture(event.pointerId);
    }
  }

  function dragged(event: PointerEvent<HTMLDivElement>) {
    if (!dragging.current) return;
    const found = placeOf(event, length);
    if (!found) return;
    onMove((now) => ({
      anchor: now.anchor,
      caret: found.place,
      across: false,
      atEnd: found.atEnd,
    }));
  }

  function released(event: PointerEvent<HTMLDivElement>) {
    dragging.current = false;
    if (
      "hasPointerCapture" in event.currentTarget &&
      event.currentTarget.hasPointerCapture(event.pointerId)
    ) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  return (
    <figure className="sequence" aria-label={label}>
      {/* A widget of its own: its keys move a caret along the residues and select them, as
          in a text field, and what's at the caret or selected is read out above it. */}

      <div
        ref={region}
        className="sequence-rows"
        role="application"
        aria-roledescription="sequence"
        tabIndex={0}
        aria-label={label}
        aria-describedby={describedBy}
        style={{ height: `${String(virtualizer.getTotalSize())}px` }}
        onKeyDown={keyed}
        onPointerDown={pressed}
        onPointerMove={dragged}
        onPointerUp={released}
        onPointerCancel={released}
        onFocus={() => {
          setFocused(true);
        }}
        onBlur={() => {
          setFocused(false);
        }}
      >
        {virtualizer.getVirtualItems().map((item) => {
          const start = item.index * ROW + 1;
          const end = Math.min(start + ROW - 1, length);
          const pieces = piecesIn(features, start, length);
          const lanes = pieces.reduce((most, p) => Math.max(most, p.feature.lane + 1), 0);
          const offset = at.caret - start + 1;
          const layers = layersOf(item.index);
          return (
            <div
              key={item.key}
              data-index={item.index}
              ref={virtualizer.measureElement}
              className="sequence-row"
              style={{
                transform: `translateY(${String(item.start - virtualizer.options.scrollMargin)}px)`,
              }}
            >
              <span className="sequence-position" style={{ width: `${String(digits)}ch` }}>
                {start}
              </span>
              <div className="sequence-line">
                <span className="sequence-residues">{groups(sequence, start, layers)}</span>
                {paired ? (
                  <span className="sequence-residues sequence-complement" aria-hidden="true">
                    {groups(other, start, layers)}
                  </span>
                ) : null}
                {focused && caretAt === item.index ? (
                  <span
                    className={`sequence-caret${paired ? " sequence-caret-paired" : ""}`}
                    aria-hidden="true"
                    style={{
                      left: `calc(${String(offset <= 0 ? 0 : columnOf(offset - 1) + 1)}ch - 1px)`,
                    }}
                  />
                ) : null}
                {reading.map((lane) => (
                  <span
                    key={lane.key}
                    className="translation"
                    data-label={lane.label.replace("frame ", "")}
                    aria-hidden="true"
                  >
                    {codonsIn(lane, start, end)}
                  </span>
                ))}
                {lanes ? (
                  <span
                    className="sequence-track"
                    aria-hidden="true"
                    style={{ height: `${String(lanes * 1.25)}rem` }}
                  >
                    {pieces.map((p) => {
                      const kind = kindOf(p.feature.type);
                      const arrow = p.arrow ? ` feature-${p.arrow}` : "";
                      const shape = p.shape === "part" ? "" : ` feature-${p.shape}`;
                      const name = featureName(p.feature);
                      return (
                        <span
                          key={`${String(p.feature.index)}-${p.shape}-${String(p.column)}`}
                          data-feature={p.feature.index}
                          className={`feature feature-${kind}${shape}${arrow}${
                            shown?.index === p.feature.index ? " feature-shown" : ""
                          }`}
                          title={`${name} (${p.feature.type}, ${locationText(p.feature)})`}
                          style={
                            p.shape === "site"
                              ? {
                                  // On the boundary after the base, between it and the next.
                                  left: `calc(${String(p.column + 1)}ch - 1px)`,
                                  width: "2px",
                                  top: `${String(p.feature.lane * 1.25)}rem`,
                                }
                              : {
                                  left: `${String(p.column)}ch`,
                                  width: `${String(p.width)}ch`,
                                  top: `${String(p.feature.lane * 1.25)}rem`,
                                }
                          }
                        >
                          {p.shape === "part" && p.width >= name.length + 2 ? (
                            <span className="feature-label">{name}</span>
                          ) : null}
                        </span>
                      );
                    })}
                  </span>
                ) : null}
                {(codingLanes[item.index] ?? 0) ? (
                  <span
                    className="translation-track"
                    aria-hidden="true"
                    style={{ height: `${String((codingLanes[item.index] ?? 0) * 1.25)}rem` }}
                  >
                    {coding.map((lane) => (
                      <span
                        key={lane.key}
                        className="translation"
                        style={{ top: `${String((lane.feature?.lane ?? 0) * 1.25)}rem` }}
                      >
                        {codonsIn(lane, start, end)}
                      </span>
                    ))}
                  </span>
                ) : null}
                {tracks.map((track) => (
                  <TrackStrip key={track.field} track={track} start={start} end={end} />
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </figure>
  );
}

/** What a track is, its scale, and where its highest and lowest values are, which is how
 * a screen reader learns what the bars show. */
function TrackLegend({
  label,
  drawn,
  given,
}: {
  label: string;
  drawn: Drawn | null;
  given: boolean;
}) {
  if (!drawn) {
    return (
      <>
        <span className="track-swatch track-swatch-none" aria-hidden="true" />
        {label}: {given ? "not drawn, as it doesn’t have one number per residue" : "none yet"}
      </>
    );
  }
  const [low, high] = drawn.scale;
  const ends = extremes(drawn.values);
  return (
    <>
      <span className="track-swatch" aria-hidden="true" />
      {label}, on a scale from {decimal.format(low)} to {decimal.format(high)}
      {ends
        ? `: highest ${decimal.format(ends.highest[0])} at ${number.format(ends.highest[1])}, lowest ${decimal.format(ends.lowest[0])} at ${number.format(ends.lowest[1])}`
        : ": no values"}
    </>
  );
}

/** One row's stretch of a track: a bar per residue, above the baseline for values over
 * zero and below it for those under, in the residues' own columns. */
function TrackStrip({ track, start, end }: { track: Drawn; start: number; end: number }) {
  const width = columnOf(end - start) + 1;
  const [low, high] = track.scale;
  const y = (v: number) => (high - v) / (high - low);
  const base = y(0);
  const bars: ReactNode[] = [];
  for (let at = start; at <= end; at++) {
    const v = track.values[at - 1];
    if (v === null || v === undefined) continue;
    const top = Math.min(y(v), base);
    bars.push(
      <rect
        key={at}
        className={v >= 0 ? "track-above" : "track-below"}
        x={columnOf(at - start) + 0.1}
        width={0.8}
        y={top}
        height={Math.max(Math.abs(y(v) - base), 0.02)}
      >
        <title>{`${track.label} at ${number.format(at)}: ${decimal.format(v)}`}</title>
      </rect>,
    );
  }
  return (
    <svg
      className="value-track"
      aria-hidden="true"
      viewBox={`0 0 ${String(width)} 1`}
      preserveAspectRatio="none"
      style={{ width: `${String(width)}ch` }}
    >
      <line
        className="track-baseline"
        x1={0}
        x2={width}
        y1={base}
        y2={base}
        vectorEffect="non-scaling-stroke"
      />
      {bars}
    </svg>
  );
}

/** A row's residues in groups of ten, each stretch marked by the layers it's in: the feature
 * shown, what's selected. */
function groups(sequence: string, start: number, layers: Layer[]): ReactNode[] {
  const out: ReactNode[] = [];
  const end = Math.min(start + ROW - 1, sequence.length);
  // Where a mark starts or ends in the row: the residues between two neighbours are in
  // the same layers.
  const cuts = new Set<number>([start, end + 1]);
  for (const layer of layers) {
    for (const [a, b] of layer.ranges) {
      if (b < start || a > end) continue;
      cuts.add(Math.max(a, start));
      cuts.add(Math.min(b, end) + 1);
    }
  }
  for (let from = start; from <= end; from += GROUP) cuts.add(from);
  const bounds = [...cuts].sort((x, y) => x - y);
  let group: ReactNode[] = [];
  let groupStart = start;
  const flush = () => {
    out.push(
      <span key={groupStart} className="sequence-group" style={{ width: `${String(GROUP)}ch` }}>
        {group}
      </span>,
    );
    group = [];
  };
  for (let i = 0; i < bounds.length - 1; i++) {
    const a = bounds[i] ?? start;
    const b = (bounds[i + 1] ?? end + 1) - 1;
    if (a > groupStart && (a - start) % GROUP === 0) {
      flush();
      groupStart = a;
    }
    const classes = layers
      .filter((l) => l.ranges.some(([x, y]) => x <= a && b <= y))
      .map((l) => l.className);
    const text = sequence.slice(a - 1, b);
    group.push(
      classes.length ? (
        <span key={a} className={classes.join(" ")}>
          {text}
        </span>
      ) : (
        text
      ),
    );
  }
  flush();
  return out;
}

function FeatureTable({
  features,
  length,
  shown,
  onShow,
  library,
}: {
  features: readonly Feature[];
  length: number;
  shown: Feature | null;
  onShow: (feature: Feature | null) => void;
  library?: { id: string; searched: boolean; version: string | null };
}) {
  if (!library && !features.length) return <p className="muted">It has no features yet.</p>;
  return (
    <section className="card" aria-labelledby={library?.id}>
      <h2 id={library?.id}>{library ? "Library features" : "Features"}</h2>
      {library?.version ? <p className="muted">{library.version}</p> : null}
      {features.length ? (
        <div className="table-scroll">
          <table className={library ? "table table-compact" : undefined}>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Type</th>
                <th scope="col">Location</th>
                <th scope="col">Strand</th>
                <th scope="col">Length</th>
                {library ? <th scope="col">Source</th> : null}
                <th scope="col">
                  <span className="visually-hidden">Show</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {features.map((f) => {
                const showing = shown?.index === f.index;
                return (
                  <tr key={f.index}>
                    <th scope="row">
                      <span
                        className={`feature-swatch feature-${kindOf(f.type)}`}
                        aria-hidden="true"
                      />
                      {featureName(f)}
                    </th>
                    <td>{f.type}</td>
                    <td>{locationText(f)}</td>
                    <td>{f.strand === "none" ? "—" : f.strand}</td>
                    <td>{lengthText(f, length, number)}</td>
                    {library ? (
                      <td className="cell-wide">{f.qualifiers["source"] ?? "—"}</td>
                    ) : null}
                    <td>
                      <button
                        type="button"
                        className="button button-quiet"
                        aria-pressed={showing}
                        aria-label={`${showing ? "Hide" : "Show"} ${featureName(f)}`}
                        onClick={() => {
                          onShow(showing ? null : f);
                        }}
                      >
                        {showing ? "Hide" : "Show"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">
          {library?.searched ? "No library features matched." : "No library search has run."}
        </p>
      )}
    </section>
  );
}

/** A client-side element. Assign a new data object to replace its input. */
export class SequenceViewerElement extends ReactElement {
  private value: SequenceData | null = null;
  private revision = 0;
  private feature: FeatureRef | null = null;

  override connectedCallback() {
    defaultFeatureColors(this);
    super.connectedCallback();
  }

  get data(): SequenceData | null {
    return this.value;
  }
  set data(next: SequenceData | null) {
    if (next === this.value) return;
    this.value = next;
    this.revision++;
    this.feature = next?.feature ?? null;
    this.invalidate();
  }

  get shownFeature(): FeatureRef | null {
    return this.feature;
  }

  private readonly reportFeature = (feature: FeatureRef | null) => {
    this.feature = feature;
    this.dispatchEvent(
      new CustomEvent("feature-change", { detail: feature, bubbles: true, composed: true }),
    );
  };
  private readonly reportSelection = (selection: SequenceSelection) => {
    this.dispatchEvent(
      new CustomEvent("selection-change", { detail: selection, bubbles: true, composed: true }),
    );
  };

  protected override renderView() {
    return (
      this.value && (
        <>
          <link rel="stylesheet" href={viewerThemeUrl} />
          <link
            rel="stylesheet"
            href={new URL("./sequence-viewer.css?no-inline", import.meta.url).href}
          />
          <SequenceView
            key={this.revision}
            data={this.value}
            reportFeature={this.reportFeature}
            reportSelection={this.reportSelection}
          />
        </>
      )
    );
  }
}

if (!customElements.get("iki-sequence-viewer"))
  customElements.define("iki-sequence-viewer", SequenceViewerElement);

declare global {
  interface HTMLElementTagNameMap {
    "iki-sequence-viewer": SequenceViewerElement;
  }
}
