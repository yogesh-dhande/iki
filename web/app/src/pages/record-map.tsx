/**
 * A small map of a sequence's features, for a record page's rail (ADR 0176): a plasmid
 * as a ring, anything else as a line, each feature drawn in its kind's color, as the
 * sequence viewer colors them. The viewer's tab has the sequence itself.
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState, type ReactNode } from "react";

import { componentsQuery, entityTabs } from "../components";
import { recordQuery, recordSearch, typesQuery, type Entity, type EntityType } from "../entities";
import { featureName, kindOf, readFeatures, spans, type Feature } from "../sequence";

type Viewer = Extract<EntityType["viewers"][number], { viewer: "sequence" }>;

/** Where its features are drawn in full: a viewer's tab of Iki's, or a plugin's tab. */
export type MapOpens =
  | { label: string; viewer: string; component?: undefined }
  | { label: string; component: string; viewer?: undefined };

export interface MapOf {
  /** The field its features are in. */
  field: string;
  alphabet: Viewer["alphabet"];
  length: number;
  circular: boolean;
  features: Feature[];
  opens: MapOpens | null;
}

/** The id of a record page's Features card, which its features field's row links to. */
export const FEATURES_ID = "record-features";

/** A link to where its features are drawn in full. */
function OpenLink({
  map,
  entity,
  className,
  children,
}: {
  map: MapOf;
  entity: Entity;
  className?: string;
  children: ReactNode;
}) {
  const opens = map.opens;
  if (!opens) return null;
  return opens.viewer ? (
    <Link
      className={className}
      to="/entities/$ref/viewers/$viewer/"
      params={{ ref: entity.hrid, viewer: opens.viewer }}
      search={recordSearch(entity)}
    >
      {children}
    </Link>
  ) : (
    <Link
      className={className}
      to="/entities/$ref/components/$/"
      params={{ ref: entity.hrid, _splat: opens.component }}
      search={recordSearch(entity)}
    >
      {children}
    </Link>
  );
}

/** The features a table lists before it's asked for all of them. */
const LISTED = 10;

const STRAND = { forward: "forward", reverse: "reverse", none: "neither", mixed: "both" };

/** Its features as a table, under its values: what they are and where they lie. The
 * viewer has them along the sequence. */
export function FeatureTable({ map, entity }: { map: MapOf; entity: Entity }) {
  const [all, setAll] = useState(false);
  const id = FEATURES_ID;
  const shown = all ? map.features : map.features.slice(0, LISTED);
  const more = map.features.length - LISTED;
  return (
    <section className="card record-section" id={id} aria-labelledby={`${id}-title`}>
      <div className="section-head">
        <h2 className="section-title" id={`${id}-title`}>
          Features
        </h2>
        <p className="muted">
          {map.features.length.toLocaleString()} along its {map.length.toLocaleString()}{" "}
          {map.alphabet === "protein" ? "residues" : "bp"}
        </p>
        <OpenLink map={map} entity={entity} className="section-link">
          Open in {map.opens?.label}
        </OpenLink>
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
  return {
    field: viewer.features,
    alphabet: viewer.alphabet,
    length: bases.length,
    circular,
    features,
    opens: { label: viewer.label, viewer: viewer.viewer },
  };
}

/** A type whose sequence is another record's, as a plasmid's is its DNA sequence's
 * (ADR 0163): the reference and its own features field, from the sequence export that
 * writes the two together (ADR 0171; every export is a sequence's). */
function referencedSequence(type: EntityType): { sequence: string; features: string } | null {
  const field = (name: string) => type.fields.find((f) => f.name === name);
  for (const e of type.exports ?? []) {
    if (e.features && field(e.sequence)?.type === "ref") {
      return { sequence: e.sequence, features: e.features };
    }
  }
  return null;
}

/**
 * The rail's map: from its own sequence viewer, or, for a record whose sequence is
 * another's, from that record's bases and its own features. Where it's drawn in full is
 * then its plugin's Map tab, if it has one. Null while what it needs is read, and for a
 * record with no features or whose sequence the reader can't read.
 */
export function useMiniMap(entity: Entity, type: EntityType | undefined): MapOf | null {
  const own = type ? miniMapOf(entity, type) : null;
  const referenced = !own && type ? referencedSequence(type) : null;
  const ref = referenced ? entity.values[referenced.sequence] : null;
  const wanted =
    typeof ref === "string" && Array.isArray(entity.values[referenced?.features ?? ""]);
  const types = useQuery({ ...typesQuery, enabled: wanted }).data;
  const components = useQuery({ ...componentsQuery, enabled: wanted }).data;
  const sequence = useQuery({
    ...recordQuery(typeof ref === "string" ? ref : "", entity.owner_project?.id),
    enabled: wanted,
    retry: false,
  }).data;
  if (own) return own;
  if (!wanted || !referenced || !sequence || !types) return null;
  const its = types.find((t) => t.name === sequence.type);
  const viewer = its?.viewers.find((v): v is Viewer => v.viewer === "sequence");
  const bases = viewer ? sequence.values[viewer.sequence] : null;
  if (!viewer || typeof bases !== "string" || !bases.length) return null;
  const features = readFeatures(entity.values[referenced.features], bases.length);
  if (!features.length) return null;
  // A plugin's map of it, such as the plasmid plugin's (ADR 0084), draws them in full.
  const tab = entityTabs(entity.type, types, components ?? []).find(
    (t) => t.label.toLowerCase() === "map",
  );
  return {
    field: referenced.features,
    alphabet: viewer.alphabet,
    length: bases.length,
    circular: viewer.topology !== null && sequence.values[viewer.topology] === "circular",
    features,
    opens: tab ? { label: tab.label, component: tab.component.name } : null,
  };
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
  const bases = `${map.length.toLocaleString()} ${map.alphabet === "protein" ? "residues" : "bp"}`;
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
      <OpenLink map={map} entity={entity}>
        {map.opens?.viewer ? `Open the ${map.opens.label.toLowerCase()}` : "Open the full map"}
      </OpenLink>
    </section>
  );
}
