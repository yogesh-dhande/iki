/**
 * Iki's own viewers on an entity's page, as its type configures them: the sequence
 * viewer, with the sequence's features and its tracks of per-residue values along it, the
 * structure viewer, and the alignment viewer. Each is a tab of the entity's.
 */

import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useCallback, useRef } from "react";
import type { Entity } from "../entities";
import { usePageTitle } from "../layout";
import { actionsTakingQuery } from "../runs";
import { buttonClass } from "../ui";
import type { Capture } from "../views";
import { useEntity } from "./entity-header";
import { NoSuchTab } from "./errors";
import { SavedViews } from "./saved-views";
import { hasFeatureLibrary, ViewerBody } from "./viewer-body";

const LIBRARY_ACTION = "iki/plasmid.annotate_dna_sequence";

function AnnotateFromLibrary({ entity }: { entity: Entity }) {
  const { data: action } = useQuery({
    ...actionsTakingQuery([entity.type]),
    select: (actions) => actions.find((a) => a.name === LIBRARY_ACTION),
  });
  if (!action || !entity.owner_project) return null;
  return (
    <p>
      <Link
        to="/projects/$slug/runs/new/"
        params={{ slug: entity.owner_project.slug }}
        search={{ action: action.name, entities: entity.hrid }}
        className={buttonClass()}
      >
        Annotate from library
      </Link>
    </p>
  );
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
  if (!viewer || !type) return <NoSuchTab />;
  return (
    <>
      {viewer.viewer === "sequence" &&
      hasFeatureLibrary(type, viewer) &&
      entity.owner_project &&
      entity.can.edit &&
      !entity.archived ? (
        <AnnotateFromLibrary entity={entity} />
      ) : null}
      <ViewerBody source={entity} type={type} viewer={viewer} onCapture={onCapture} />
      <SavedViews entity={entity} viewer={viewer} capture={capture} />
    </>
  );
}
