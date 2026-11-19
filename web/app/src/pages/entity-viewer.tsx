/**
 * Iki's own viewers on an entity's page, as its type configures them: the sequence
 * viewer, with the sequence's features and its tracks of per-residue values along it, the
 * structure viewer, and the alignment viewer.
 */

import type { Schemas } from "@ikisci/sdk";
import { useParams } from "@tanstack/react-router";
import { useWindowVirtualizer } from "@tanstack/react-virtual";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import type { Entity, EntityType } from "../entities";
import { usePageTitle } from "../layout";
import {
  columnOf,
  extremes,
  featureLength,
  featureName,
  GROUP,
  kindOf,
  lanesPerRow,
  locationText,
  piecesIn,
  readFeatures,
  readTrack,
  ROW,
  covered,
  trackScale,
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
  const value = source.values[viewer.sequence];
  const sequence = typeof value === "string" ? value : "";
  const features = useMemo(
    () => (viewer.features ? readFeatures(source.values[viewer.features], sequence.length) : []),
    [source.values, viewer.features, sequence.length],
  );
  // A protein's final stop (*) is no residue: its values are one a residue, none for it.
  const residues =
    viewer.alphabet === "protein" && sequence.endsWith("*") ? sequence.length - 1 : sequence.length;
  const tracks = useMemo(
    () =>
      viewer.tracks.map((track) => {
        const given = readTrack(source.values[track.field], residues);
        const values = given && residues < sequence.length ? [...given, null] : given;
        return {
          ...track,
          given: source.values[track.field] != null,
          drawn: values
            ? { ...track, values, scale: trackScale(values, track.min, track.max) }
            : null,
        };
      }),
    [source.values, viewer.tracks, sequence.length, residues],
  );
  const drawn = useMemo(() => tracks.flatMap((t) => (t.drawn ? [t.drawn] : [])), [tracks]);
  const [shown, setShown] = useState<Feature | null>(() =>
    feature ? (features.find((f) => sameFeature(f, feature)) ?? null) : null,
  );
  useEffect(() => {
    if (!onCapture) return;
    onCapture(() => Promise.resolve({ targets: [shown ? { feature: featureRef(shown) } : {}] }));
    return () => {
      onCapture(null);
    };
  }, [shown, onCapture]);
  const field = type.fields.find((f) => f.name === viewer.sequence);
  // A DNA's length is in base pairs, an RNA's, single-stranded, in nucleotides.
  const unit = UNITS[viewer.alphabet];
  if (!sequence) {
    return <p className="muted">It has no {field?.label.toLowerCase() ?? "sequence"} yet.</p>;
  }
  const kinds = [...new Set(features.map((f) => kindOf(f.type)))];
  return (
    <>
      <p className="muted">
        {number.format(residues)} {unit}
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
      <Residues
        sequence={sequence}
        features={features}
        tracks={drawn}
        shown={shown}
        label={`${field?.label ?? "Sequence"} of ${source.title}`}
      />
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

/** The sequence in rows of 60, in groups of 10, with each row's features under it. Rows
 * off screen aren't drawn, so a genome reads as quickly as a primer. */
/** How a sequence's length is counted, by what it's a sequence of. */
const UNITS: Record<Viewer["alphabet"], string> = { dna: "bp", rna: "nt", protein: "residues" };

function Residues({
  sequence,
  features,
  tracks,
  shown,
  label,
}: {
  sequence: string;
  features: Feature[];
  tracks: Drawn[];
  shown: Feature | null;
  label: string;
}) {
  // Where the rows begin on the page, which the window's scrolling is measured from.
  const [margin, setMargin] = useState(0);
  const list = useCallback((node: HTMLDivElement | null) => {
    if (node) setMargin(node.getBoundingClientRect().top + window.scrollY);
  }, []);
  const rows = Math.ceil(sequence.length / ROW);
  const lanes = useMemo(() => lanesPerRow(features, sequence.length), [features, sequence.length]);
  const virtualizer = useWindowVirtualizer({
    count: rows,
    // A line of residues, a lane per feature row under it, its tracks, and the gap between
    // rows.
    estimateSize: (row) => 30 + (lanes[row] ?? 0) * 20 + tracks.length * 44,
    overscan: 8,
    scrollMargin: margin,
  });
  // Showing a feature brings its first row into view.
  useEffect(() => {
    if (shown) virtualizer.scrollToIndex(Math.floor((shown.start - 1) / ROW), { align: "center" });
  }, [shown, virtualizer]);
  const highlighted = shown ? covered(shown, sequence.length) : [];
  const digits = String(sequence.length).length;
  return (
    <figure className="sequence" aria-label={label}>
      <div
        ref={list}
        className="sequence-rows"
        style={{ height: `${String(virtualizer.getTotalSize())}px` }}
      >
        {virtualizer.getVirtualItems().map((item) => {
          const start = item.index * ROW + 1;
          const pieces = piecesIn(features, start, sequence.length);
          const lanes = pieces.reduce((most, p) => Math.max(most, p.feature.lane + 1), 0);
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
                <span className="sequence-residues">{groups(sequence, start, highlighted)}</span>
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
                  <TrackStrip
                    key={track.field}
                    track={track}
                    start={start}
                    end={Math.min(start + ROW - 1, sequence.length)}
                  />
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

/** A row's residues in groups of ten, the shown feature's marked. */
function groups(sequence: string, start: number, marked: [number, number][]): ReactNode[] {
  const out: ReactNode[] = [];
  const end = Math.min(start + ROW - 1, sequence.length);
  for (let from = start; from <= end; from += GROUP) {
    const to = Math.min(from + GROUP - 1, end);
    const parts: ReactNode[] = [];
    let at = from;
    for (const [a, b] of marked) {
      const s = Math.max(a, at);
      const e = Math.min(b, to);
      if (s > e) continue;
      if (s > at) parts.push(sequence.slice(at - 1, s - 1));
      parts.push(<mark key={s}>{sequence.slice(s - 1, e)}</mark>);
      at = e + 1;
    }
    if (at <= to) parts.push(sequence.slice(at - 1, to));
    out.push(
      <span key={from} className="sequence-group" style={{ width: `${String(GROUP)}ch` }}>
        {parts}
      </span>,
    );
  }
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
                  <td>{number.format(featureLength(f, length))}</td>
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
