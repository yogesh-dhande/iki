/**
 * The sections plugins' components add to a project's page or a person's home (ADR
 * 0117): each one's search, there, as the person may read it. A section shows only once
 * its search finds something, so a component with nothing to show takes no room; one
 * whose search is refused says so, rather than hiding a problem.
 */

import { useQuery } from "@tanstack/react-query";
import { useId } from "react";

import type { Project } from "../api";
import { usePlaceEntities } from "../component-entities";
import { componentsQuery, sectionsFor, type Section } from "../components";
import { typesQuery, type EntityType } from "../entities";
import { useSettled } from "../settling";
import { PluginPlace } from "./plugin-place";

type Kind = "project.page" | "person.home";

export function PluginSections({ kind, project }: { kind: Kind; project?: Project }) {
  const read = { components: useQuery(componentsQuery), types: useQuery(typesQuery) };
  // Its sections are found once these are in: each then waits on its own search.
  useSettled(Object.values(read).every((q) => !q.isLoading));
  const components = read.components.data;
  const types = read.types.data;
  if (!components || !types) return null;
  return sectionsFor(kind, components).map((section) => (
    <PluginSection
      key={section.component.name}
      kind={kind}
      section={section}
      project={project}
      types={types}
    />
  ));
}

function PluginSection({
  kind,
  section,
  project,
  types,
}: {
  kind: Kind;
  section: Section;
  project: Project | undefined;
  types: EntityType[];
}) {
  const headingId = useId();
  const place =
    kind === "project.page" && project ? { project: project.id } : { home: true as const };
  const data = usePlaceEntities(section.component, place);
  useSettled(data.settled);
  const { items, refinement } = data.entities;
  // Nothing to show until it's found something; once it's been narrowed, it stays.
  if (!data.settled) return null;
  if (!data.problem && !refinement && items.length === 0) return null;
  return (
    <section className="card plugin-section" aria-labelledby={headingId}>
      <h2 className="section-title" id={headingId}>
        {section.label}
      </h2>
      {section.component.description ? (
        <p className="muted">{section.component.description}</p>
      ) : null}
      <PluginPlace
        component={section.component}
        label={section.label}
        place={{ slot: kind, label: section.label }}
        data={data}
        project={kind === "project.page" && project ? project : null}
        types={types}
      />
    </section>
  );
}
