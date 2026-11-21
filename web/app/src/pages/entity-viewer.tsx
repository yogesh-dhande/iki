/**
 * Iki's own viewers on an entity's page, as its type configures them: the sequence
 * viewer, with the sequence's features and its tracks of per-residue values along it, the
 * structure viewer, and the alignment viewer.
 */

import type { Schemas } from "@ikisci/sdk";
import { useParams } from "@tanstack/react-router";
import { useWindowVirtualizer } from "@tanstack/react-virtual";
import {
  useCallback,
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

import type { Entity, EntityType } from "../entities";
import { usePageTitle } from "../layout";
import {
  AT_START,
  caretRow,
  columnOf,
  complement,
  extremes,
  featureName,
  gcContent,
  GROUP,
  kindOf,
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
  readTrack,
  residuesOf,
  reverseComplement,
  ROW,
  covered,
  selected,
  stretchLength,
  trackScale,
  type Caret,
  type Feature,
  type Kind,
  type Values,
} from "../sequence";
import { buttonClass } from "../ui";
import type { Camera, Capture, FeatureRef, TargetState } from "../views";
import { EntityTabs, useEntity } from "./entity";
import { AlignmentViewer } from "./alignment-viewer";
import { NotFound } from "./errors";
import { SavedViews } from "./saved-views";
import { StructureViewer } from "./structure-viewer";

type Viewer = Schemas["SequenceViewerOut"];

/** A track as drawn: its values, one per residue, and the scale they're drawn on. */
interface Drawn {
  field: string;
  label: string;
  values: Values;
  scale: [number, number];
}
type AnyViewer = EntityType["viewers"][number];

/** What a viewer shows: an entity now, or as it was at a revision (for a saved view). */
export interface Source {
  title: string;
  values: Record<string, unknown>;
  files: Entity["files"];
  refs: Entity["refs"];
  /** The project to run actions in on what the viewer picks, if there's one. */
  owner_project?: Entity["owner_project"] | undefined;
}

export function EntityViewer() {
  const { entity, type } = useEntity();
  const { viewer: name } = useParams({ from: "/entities/$ref/viewers/$viewer" });
  const viewer = type?.viewers.find((v) => v.viewer === name);
  const capture = useRef<Capture | null>(null);
  const onCapture = useCallback((fn: Capture | null) => {
    capture.current = fn;
  }, []);
  usePageTitle(viewer ? `${viewer.label} of ${entity.title} (${entity.hrid})` : undefined);
  if (!viewer || !type) return <NotFound />;
  return (
    <>
      <EntityTabs entity={entity} type={type} />
      <ViewerBody source={entity} type={type} viewer={viewer} onCapture={onCapture} />
      <SavedViews entity={entity} viewer={viewer} capture={capture} />
    </>
  );
}

/** A viewer, showing `source` as `state` says: for a tab, or for one entity of a saved
 * view. */
export function ViewerBody({
  source,
  type,
  viewer,
  state,
  camera,
  onCapture,
}: {
  source: Source;
  type: EntityType;
  viewer: AnyViewer;
  /** What a saved view kept of it. */
  state?: TargetState | undefined;
  /** Where a saved view's camera was, for the structure viewer. */
  camera?: Camera | undefined;
  onCapture?: ((capture: Capture | null) => void) | undefined;
}) {
  if (viewer.viewer === "alignment") {
    return (
      <AlignmentViewer
        source={source}
        type={type}
        viewer={viewer}
        position={state?.position}
        onCapture={onCapture}
      />
    );
  }
  return viewer.viewer === "structure" ? (
    <StructureViewer
      source={source}
      type={type}
      viewer={viewer}
      camera={camera}
      state={state}
      onCapture={onCapture}
    />
  ) : (
    <SequenceViewer
      source={source}
      type={type}
      viewer={viewer}
      feature={state?.feature}
      onCapture={onCapture}
    />
  );
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

function SequenceViewer({
  source,
  type,
  viewer,
  feature,
  onCapture,
}: {
  source: Source;
  type: EntityType;
  viewer: Viewer;
  /** The feature a saved view showed. */
  feature?: FeatureRef | undefined;
  onCapture?: ((capture: Capture | null) => void) | undefined;
}) {
  const ids = useId();
  const value = source.values[viewer.sequence];
  const sequence = typeof value === "string" ? value : "";
  const features = useMemo(
    () => (viewer.features ? readFeatures(source.values[viewer.features], sequence.length) : []),
    [source.values, viewer.features, sequence.length],
  );
  // A protein's stops (*) are no residues, inside it (MK*V is 3) or ending it (M** is 1): its
  // values are one a residue up to the stops that end it, and none for those.
  const protein = viewer.alphabet === "protein";
  const residues = protein ? sequence.replaceAll("*", "").length : sequence.length;
  const valued = protein ? sequence.replace(/\*+$/, "").length : sequence.length;
  const tracks = useMemo(
    () =>
      viewer.tracks.map((track) => {
        const given = readTrack(source.values[track.field], valued);
        const ending = sequence.length - valued;
        const values = given && ending ? [...given, ...Array<null>(ending).fill(null)] : given;
        return {
          ...track,
          given: source.values[track.field] != null,
          drawn: values
            ? { ...track, values, scale: trackScale(values, track.min, track.max) }
            : null,
        };
      }),
    [source.values, viewer.tracks, sequence.length, valued],
  );
  const drawn = useMemo(() => tracks.flatMap((t) => (t.drawn ? [t.drawn] : [])), [tracks]);
  const [shown, setShown] = useState<Feature | null>(() =>
    feature ? (features.find((f) => sameFeature(f, feature)) ?? null) : null,
  );
  const [at, setAt] = useState<Caret>(AT_START);
  // What copying said, of the selection it copied: once that changes, it's old news.
  const [copying, setCopying] = useState<{ said: string; of: Caret } | null>(null);
  const said = copying?.of === at ? copying.said : "";
  // A duplex's other strand, drawn under it 3′ to 5′, unless it's hidden.
  const double = !protein && viewer.strands !== null && source.values[viewer.strands] === "double";
  const [pairing, setPairing] = useState(true);
  const circular = viewer.topology !== null && source.values[viewer.topology] === "circular";
  const region = useRef<HTMLDivElement | null>(null);
  const tools = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!onCapture) return;
    onCapture(() => Promise.resolve({ targets: [shown ? { feature: featureRef(shown) } : {}] }));
    return () => {
      onCapture(null);
    };
  }, [shown, onCapture]);
  const field = type.fields.find((f) => f.name === viewer.sequence);
  const unit = unitOf(viewer, source.values);
  if (!sequence) {
    return <p className="muted">It has no {field?.label.toLowerCase() ?? "sequence"} yet.</p>;
  }
  const span = selected(at);

  /** Copies what's selected, or its reverse complement, and says so. */
  function copy(reverse: boolean) {
    if (!span) return;
    const bases = residuesOf(sequence, span[0], span[1]);
    const count = number.format(bases.length);
    const noun = residueNoun(viewer.alphabet, bases.length);
    const of = at;
    void copied(reverse ? reverseComplement(bases, viewer.alphabet) : bases).then((ok) => {
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
        {viewer.features
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
            <li key={track.field}>
              <TrackLegend label={track.label} drawn={track.drawn} given={track.given} />
            </li>
          ))}
        </ul>
      ) : null}
      {double ? (
        <div className="sequence-options">
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
        </div>
      ) : null}
      <div className="sequence-tools" ref={tools}>
        <div className="sequence-selection">
          <p id={`${ids}-at`} className="sequence-at" aria-live="polite">
            {span ? (
              <Described
                sequence={sequence}
                alphabet={viewer.alphabet}
                unit={unit}
                start={span[0]}
                end={span[1]}
              />
            ) : (
              caretText(sequence, viewer.alphabet, at.caret, double)
            )}
          </p>
          <div className="sequence-copy">
            <button
              type="button"
              className={buttonClass()}
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
                className={buttonClass()}
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
        alphabet={viewer.alphabet}
        features={features}
        tracks={drawn}
        shown={shown}
        paired={double && pairing}
        label={`${field?.label ?? "Sequence"} of ${source.title}`}
        at={at}
        onMove={setAt}
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
      </p>
      {viewer.features ? (
        <FeatureTable
          features={features}
          length={sequence.length}
          shown={shown}
          onShow={setShown}
        />
      ) : null}
    </>
  );
}

/** What a residue is called, and many of them, by what it's a sequence of. */
function residueNoun(alphabet: Viewer["alphabet"], count = 1): string {
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
  alphabet: Viewer["alphabet"],
  caret: number,
  double: boolean,
): string {
  const noun = residueNoun(alphabet);
  if (caret >= sequence.length) {
    return `Caret after ${noun} ${number.format(sequence.length)}, the last.`;
  }
  const base = sequence.charAt(caret);
  const pairs = double ? `, paired with ${complement(base, alphabet)}` : "";
  return `Caret before ${noun} ${number.format(caret + 1)}: ${base}${pairs}.`;
}

/** What a selection is: where, how long, and for DNA its GC content and Tm, as the DNA
 * plugin computes them for a sequence and a primer. */
function Described({
  sequence,
  alphabet,
  unit,
  start,
  end,
}: {
  sequence: string;
  alphabet: Viewer["alphabet"];
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
    if (tm !== null) facts.push(`Tm ${tenths.format(tm)} °C`);
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
const UNITS: Record<Viewer["alphabet"], string> = { dna: "bp", rna: "nt", protein: "residues" };

/** How a record's sequence length is counted: a duplex's in base pairs (bp) and a single
 * strand's in nucleotides (nt), as the field its viewer names says it is; otherwise as
 * its alphabet usually is. */
function unitOf(viewer: Viewer, values: Record<string, unknown>): string {
  if (viewer.alphabet === "protein") return UNITS.protein;
  const strands = viewer.strands ? values[viewer.strands] : undefined;
  if (strands === "single") return "nt";
  if (strands === "double") return "bp";
  return UNITS[viewer.alphabet];
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
  label,
  at,
  onMove,
  onCopy,
  region,
  tools,
  describedBy,
}: {
  sequence: string;
  alphabet: Viewer["alphabet"];
  features: Feature[];
  tracks: Drawn[];
  shown: Feature | null;
  /** Whether to draw the complementary strand under each row. */
  paired: boolean;
  label: string;
  at: Caret;
  onMove: Dispatch<SetStateAction<Caret>>;
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
  const other = useMemo(
    () => (paired ? complement(sequence, alphabet) : ""),
    [paired, sequence, alphabet],
  );
  const virtualizer = useWindowVirtualizer({
    count: rows,
    // A line of residues, the other strand's, a lane per feature row under it, its tracks,
    // and the gap between rows.
    estimateSize: (row) => 30 + (paired ? 20 : 0) + (lanes[row] ?? 0) * 20 + tracks.length * 44,
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
  const span = selected(at);
  const layers: Layer[] = [
    { className: "residue-shown", ranges: shown ? covered(shown, length) : [] },
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
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <div
        ref={region}
        className="sequence-rows"
        role="application"
        aria-roledescription="sequence"
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
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
}: {
  features: Feature[];
  length: number;
  shown: Feature | null;
  onShow: (feature: Feature | null) => void;
}) {
  if (!features.length) return <p className="muted">It has no features yet.</p>;
  return (
    <section className="card">
      <h2>Features</h2>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Type</th>
              <th scope="col">Location</th>
              <th scope="col">Strand</th>
              <th scope="col">Length</th>
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
                  <td>
                    <button
                      type="button"
                      className={buttonClass("quiet")}
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
    </section>
  );
}
