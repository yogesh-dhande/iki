/**
 * One of Iki's own viewers, showing an entity or what a saved view kept of one: the
 * sequence viewer, the structure viewer or the alignment viewer. Apart from the pages that
 * show one, which load it when they draw it: the viewers are most of what they weigh.
 */

import { createElement, useCallback, useMemo } from "react";
import "@ikisci/science/sequence-viewer";
import type {
  SequenceViewerElement,
  SequenceData,
  SequenceFeature,
} from "@ikisci/science/sequence-viewer";
import type { Entity, EntityType } from "../entities";
import type { Camera, Capture, FeatureRef, TargetState } from "../views";
import { AlignmentViewer } from "./alignment-viewer";
import { StructureViewer } from "./structure-viewer";

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

/** The DNA plugin's action writes library hits to a separate field on the record. */
export function hasFeatureLibrary(
  type: EntityType,
  viewer: Extract<AnyViewer, { viewer: "sequence" }>,
): boolean {
  return viewer.alphabet === "dna" && type.fields.some((f) => f.name === "library_features");
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

function SequenceViewer({
  source,
  type,
  viewer,
  feature,
  onCapture,
}: {
  source: Source;
  type: EntityType;
  viewer: Extract<AnyViewer, { viewer: "sequence" }>;
  feature?: FeatureRef | undefined;
  onCapture?: ((capture: Capture | null) => void) | undefined;
}) {
  const data = useMemo((): SequenceData => {
    const value = source.values[viewer.sequence];
    const strands = viewer.strands ? source.values[viewer.strands] : undefined;
    const label = type.fields.find((field) => field.name === viewer.sequence)?.label;
    // The package validates each JSON feature, coordinate and numeric track before drawing.
    const features = viewer.features ? source.values[viewer.features] : undefined;
    const foundInLibrary = source.values["library_features"];
    const libraryVersion = source.values["library_annotated_with"];
    return {
      title: source.title,
      ...(hasFeatureLibrary(type, viewer)
        ? {
            library: {
              ...(Array.isArray(foundInLibrary)
                ? { features: foundInLibrary as SequenceFeature[] }
                : {}),
              ...(typeof libraryVersion === "string" ? { version: libraryVersion } : {}),
            },
          }
        : {}),
      alphabet: viewer.alphabet,
      sequence: typeof value === "string" ? value : "",
      ...(label ? { label } : {}),
      ...(feature ? { feature } : {}),
      ...(viewer.features
        ? { features: Array.isArray(features) ? (features as SequenceFeature[]) : [] }
        : {}),
      ...(strands === "single" || strands === "double" ? { strands } : {}),
      topology:
        viewer.topology && source.values[viewer.topology] === "circular" ? "circular" : "linear",
      tracks: viewer.tracks.map((track) => ({
        ...track,
        id: track.field,
        values: source.values[track.field] as (number | null)[] | null,
      })),
    };
  }, [source, type, viewer, feature]);
  const setViewer = useCallback(
    (element: SequenceViewerElement | null) => {
      if (!element) {
        onCapture?.(null);
        return;
      }
      onCapture?.(() => {
        const shown = element.shownFeature;
        return Promise.resolve({ targets: [shown ? { feature: shown } : {}] });
      });
    },
    [onCapture],
  );
  return createElement("iki-sequence-viewer", { data, ref: setViewer });
}
