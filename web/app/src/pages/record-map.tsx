/**
 * A small map of a sequence's features, for a record page's rail (ADR 0176): a plasmid
 * as a ring, anything else as a line, each feature drawn in its kind's color, as the
 * sequence viewer colors them. The viewer's tab has the sequence itself.
 */

import { Link } from "@tanstack/react-router";
import { useId, useState } from "react";

import { recordSearch, type Entity, type EntityType } from "../entities";
import { featureName, kindOf, readFeatures, spans, type Feature } from "../sequence";

type Viewer = Extract<EntityType["viewers"][number], { viewer: "sequence" }>;

export interface MapOf {
  viewer: Viewer;
  length: number;
  circular: boolean;
  features: Feature[];
}

/** The features a table lists before it's asked for all of them. */
const LISTED = 10;

const STRAND = { forward: "forward", reverse: "reverse", none: "neither", mixed: "both" };

/** Its features as a table, under its values: what they are and where they lie. The
 * viewer has them along the sequence. */
export function FeatureTable({ map, entity }: { map: MapOf; entity: Entity }) {
  const [all, setAll] = useState(false);
  const id = useId();
  const shown = all ? map.features : map.features.slice(0, LISTED);
  const more = map.features.length - LISTED;
  return (
    <section className="card record-section" aria-labelledby={`${id}-title`}>
      <div className="section-head">
        <h2 className="section-title" id={`${id}-title`}>
          Features
        </h2>
        <p className="muted">
          {map.features.length.toLocaleString()} along its {map.length.toLocaleString()}{" "}
          {map.viewer.alphabet === "protein" ? "residues" : "bp"}
        </p>
        <Link
          className="section-link"
          to="/entities/$ref/viewers/$viewer/"
          params={{ ref: entity.hrid, viewer: map.viewer.viewer }}
          search={recordSearch(entity)}
        >
          Open in {map.viewer.label}
        </Link>
      </div>
      {/* On a phone it scrolls across within its card, by keyboard too. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable region */}
      <div className="feature-table" tabIndex={0} role="group" aria-labelledby={`${id}-caption`}>
        <table className="table" id={`${id}-table`}>
          <caption className="visually-hidden" id={`${id}-caption`}>
            Features of {entity.title}
          </caption>
          <thead>
            <tr>
              <th scope="col">Feature</th>
              <th scope="col">Kind</th>
              <th scope="col" className="numeric">
                Start
              </th>
              <th scope="col" className="numeric">
                End
              </th>
              <th scope="col">Strand</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((f) => (
              <tr key={f.index}>
                <td>
                  <svg className="feature-dot" viewBox="0 0 8 8" aria-hidden="true">
                    <circle className={`feature-fill-${kindOf(f.type)}`} cx="4" cy="4" r="4" />
                  </svg>
                  {featureName(f)}
                </td>
                <td className="muted">{f.type}</td>
                <td className="numeric">{f.start.toLocaleString()}</td>
                <td className="numeric">{f.end.toLocaleString()}</td>
                <td>{STRAND[f.strand]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {more > 0 ? (
        <p className="feature-more">
          <button
            type="button"
            className="button button-quiet"
            aria-controls={`${id}-table`}
            aria-expanded={all}
            onClick={() => {
              setAll(!all);
            }}
          >
            {all ? "Show fewer" : `Show all ${map.features.length.toLocaleString()}`}
          </button>
        </p>
      ) : null}
    </section>
  );
}

/** What the rail's map shows, for a record whose type's sequence viewer has features: none
 * for one with no features set. */
export function miniMapOf(entity: Entity, type: EntityType): MapOf | null {
  const viewer = type.viewers.find(
    (v): v is Viewer => v.viewer === "sequence" && "features" in v && Boolean(v.features),
  );
  if (!viewer?.features) return null;
  const bases = entity.values[viewer.sequence];
  if (typeof bases !== "string" || !bases.length) return null;
  const features = readFeatures(entity.values[viewer.features], bases.length);
  if (!features.length) return null;
  const circular = viewer.topology !== null && entity.values[viewer.topology] === "circular";
  return { viewer, length: bases.length, circular, features };
}

const SIZE = 180;
const MIDDLE = SIZE / 2;
const RADIUS = 68;
/** The most features the map's description names; it counts the rest. */
const NAMED = 6;

function point(position: number, length: number, radius: number): [number, number] {
  // From the top, clockwise: where a plasmid map starts its origin.
  const angle = (position / length) * 2 * Math.PI - Math.PI / 2;
  return [MIDDLE + radius * Math.cos(angle), MIDDLE + radius * Math.sin(angle)];
}

/** An arc along the ring from `from` to `to` (bases, from 1, inclusive). */
function arc(from: number, to: number, length: number, radius: number): string {
  const [x1, y1] = point(from - 1, length, radius);
  const [x2, y2] = point(to, length, radius);
  const large = to - from + 1 > length / 2 ? 1 : 0;
  return `M${x1.toFixed(2)} ${y1.toFixed(2)} A${String(radius)} ${String(radius)} 0 ${String(large)} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
}

function described(map: MapOf, title: string): string {
  const names = map.features.slice(0, NAMED).map(featureName);
  const more = map.features.length - names.length;
  const shape = map.circular ? "Circular" : "Linear";
  const bases = `${map.length.toLocaleString()} ${map.viewer.alphabet === "protein" ? "residues" : "bp"}`;
  const listed = more > 0 ? `${names.join(", ")} and ${String(more)} more` : names.join(", ");
  return `${shape} map of ${title}, ${bases}: ${listed}`;
}

export function MiniMap({ map, entity }: { map: MapOf; entity: Entity }) {
  const lanes = Math.max(...map.features.map((f) => f.lane), 0) + 1;
  // Overlapping features sit on rings, or rows, of their own, as in the viewer.
  const step = Math.min(9, 36 / lanes);
  return (
    <section className="card rail-card record-map" aria-label="Map">
      <svg
        viewBox={`0 0 ${String(SIZE)} ${map.circular ? String(SIZE) : "64"}`}
        role="img"
        aria-label={described(map, entity.title)}
      >
        {map.circular ? (
          <>
            <circle className="map-backbone" cx={MIDDLE} cy={MIDDLE} r={RADIUS} />
            {map.features.flatMap((f) =>
              spans(f, map.length).map(([from, to]) => (
                <path
                  key={`${String(f.index)}-${String(from)}`}
                  className={`map-feature feature-stroke-${kindOf(f.type)}`}
                  d={arc(from, to, map.length, RADIUS - f.lane * step)}
                >
                  <title>{featureName(f)}</title>
                </path>
              )),
            )}
            <text className="map-name" x={MIDDLE} y={MIDDLE - 2} textAnchor="middle">
              {entity.title.length > 18 ? `${entity.title.slice(0, 17)}…` : entity.title}
            </text>
            <text className="map-length" x={MIDDLE} y={MIDDLE + 14} textAnchor="middle">
              {map.length.toLocaleString()} bp
            </text>
          </>
        ) : (
          <>
            <line className="map-backbone" x1="4" y1="12" x2={SIZE - 4} y2="12" />
            {map.features.flatMap((f) =>
              spans(f, map.length).map(([from, to]) => {
                const x = 4 + ((from - 1) / map.length) * (SIZE - 8);
                const width = Math.max(2, ((to - from + 1) / map.length) * (SIZE - 8));
                return (
                  <rect
                    key={`${String(f.index)}-${String(from)}`}
                    className={`map-feature-bar feature-fill-${kindOf(f.type)}`}
                    x={x}
                    y={20 + f.lane * step}
                    width={width}
                    height={Math.max(3, step - 2)}
                    rx="1"
                  >
                    <title>{featureName(f)}</title>
                  </rect>
                );
              }),
            )}
          </>
        )}
      </svg>
      <Link
        to="/entities/$ref/viewers/$viewer/"
        params={{ ref: entity.hrid, viewer: map.viewer.viewer }}
        search={recordSearch(entity)}
      >
        Open the {map.viewer.label.toLowerCase()}
      </Link>
    </section>
  );
}
