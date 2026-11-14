/**
 * Iki's own viewers on an entity's page, as its type configures them: the sequence
 * viewer, with the sequence's features as a track along it, and the structure viewer.
 */

import type { Schemas } from "@ikisci/sdk";
import { useParams } from "@tanstack/react-router";
import { useWindowVirtualizer } from "@tanstack/react-virtual";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import type { Entity, EntityType } from "../entities";
import { usePageTitle } from "../layout";
import {
  featureLength,
  featureName,
  GROUP,
  kindOf,
  lanesPerRow,
  locationText,
  piecesIn,
  readFeatures,
  ROW,
  spans,
  type Feature,
  type Kind,
} from "../sequence";
import { buttonClass } from "../ui";
import { EntityTabs, useEntity } from "./entity";
import { NotFound } from "./errors";
import { StructureViewer } from "./structure-viewer";

type Viewer = Schemas["SequenceViewerOut"];

export function EntityViewer() {
  const { entity, type } = useEntity();
  const { viewer: name } = useParams({ from: "/entities/$ref/viewers/$viewer" });
  const viewer = type?.viewers.find((v) => v.viewer === name);
  usePageTitle(viewer ? `${viewer.label} of ${entity.title} (${entity.hrid})` : undefined);
  if (!viewer || !type) return <NotFound />;
  return (
    <>
      <EntityTabs entity={entity} type={type} />
      {viewer.viewer === "structure" ? (
        <StructureViewer entity={entity} type={type} viewer={viewer} />
      ) : (
        <SequenceViewer entity={entity} type={type} viewer={viewer} />
      )}
    </>
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

function SequenceViewer({
  entity,
  type,
  viewer,
}: {
  entity: Entity;
  type: EntityType;
  viewer: Viewer;
}) {
  const value = entity.values[viewer.sequence];
  const sequence = typeof value === "string" ? value : "";
  const features = useMemo(
    () => (viewer.features ? readFeatures(entity.values[viewer.features], sequence.length) : []),
    [entity.values, viewer.features, sequence.length],
  );
  const [shown, setShown] = useState<Feature | null>(null);
  const field = type.fields.find((f) => f.name === viewer.sequence);
  const unit = viewer.alphabet === "dna" ? "bp" : "residues";
  if (!sequence) {
    return <p className="muted">It has no {field?.label.toLowerCase() ?? "sequence"} yet.</p>;
  }
  const kinds = [...new Set(features.map((f) => kindOf(f.type)))];
  return (
    <>
      <p className="muted">
        {number.format(sequence.length)} {unit}
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
      <Residues
        sequence={sequence}
        features={features}
        shown={shown}
        label={`${field?.label ?? "Sequence"} of ${entity.title}`}
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
function Residues({
  sequence,
  features,
  shown,
  label,
}: {
  sequence: string;
  features: Feature[];
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
    // A line of residues, a lane per feature row under it, and the gap between rows.
    estimateSize: (row) => 30 + (lanes[row] ?? 0) * 20,
    overscan: 8,
    scrollMargin: margin,
  });
  // Showing a feature brings its first row into view.
  useEffect(() => {
    if (shown) virtualizer.scrollToIndex(Math.floor((shown.start - 1) / ROW), { align: "center" });
  }, [shown, virtualizer]);
  const highlighted = shown ? spans(shown, sequence.length) : [];
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
                      const arrow =
                        p.feature.strand === "forward" && p.last
                          ? " feature-forward"
                          : p.feature.strand === "reverse" && p.first
                            ? " feature-reverse"
                            : "";
                      const name = featureName(p.feature);
                      return (
                        <span
                          key={`${String(p.feature.index)}-${String(p.column)}`}
                          className={`feature feature-${kind}${arrow}${
                            shown?.index === p.feature.index ? " feature-shown" : ""
                          }`}
                          title={`${name} (${p.feature.type}, ${locationText(p.feature)})`}
                          style={{
                            left: `${String(p.column)}ch`,
                            width: `${String(p.width)}ch`,
                            top: `${String(p.feature.lane * 1.25)}rem`,
                          }}
                        >
                          {p.width >= name.length + 2 ? (
                            <span className="feature-label">{name}</span>
                          ) : null}
                        </span>
                      );
                    })}
                  </span>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
    </figure>
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
