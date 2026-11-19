/**
 * What a project takes as a service (ADR 0132): the request types other projects may send
 * it, such as a core facility's assay requests, and which projects may. Its Admins choose;
 * its other members read it. The project's page links its queue: the requests sent to it.
 */

import { unwrap } from "@ikisci/sdk";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState, type SubmitEvent } from "react";

import { fieldErrors, iki, problemOf, projectsQuery, useApiMutation, type Project } from "../api";
import { inSentence, typesQuery, type EntityType } from "../entities";
import {
  intakeQuery,
  listed,
  queuedQuery,
  requestTypes,
  type Intake as IntakeSettings,
} from "../requests";
import { Button, CheckboxesField, Loading, ProblemAlert, RadioField, useMessages } from "../ui";

type From = "registry" | "projects";

/** Who may send it requests, in words. */
function fromWhom(project: Project, intake: IntakeSettings): string {
  if (intake.from_registry) return `every project in the ${project.registry.name} registry`;
  if (!intake.projects.length) return "no project yet";
  return listed(intake.projects.map((p) => p.name));
}

/** What it takes, in a sentence: "Protein Core takes assay requests from …". */
export function takesWhat(
  project: Project,
  intake: IntakeSettings,
  types: EntityType[] | undefined,
): string {
  if (!intake.types.length) return `${project.name} takes no requests from other projects.`;
  const labels = intake.types.map((t) =>
    inSentence(types?.find((found) => found.name === t.name)?.label_plural ?? t.label),
  );
  return `${project.name} takes ${listed(labels)} from ${fromWhom(project, intake)}.`;
}

/** On the project's settings page: its Admins choose; anyone else reads it. */
export function Intake({ project }: { project: Project }) {
  const found = useQuery(intakeQuery(project.id));
  const types = useQuery(typesQuery);
  let body: React.ReactNode;
  if (found.isError || types.isError) {
    body = <ProblemAlert problem={problemOf(found.error ?? types.error)} />;
  } else if (!found.data || !types.data) {
    body = <Loading label="Finding what it takes…" />;
  } else if (!project.can.manage || project.archived) {
    body = (
      <>
        <p>{takesWhat(project, found.data, types.data)}</p>
        <p className="muted">
          {project.archived
            ? "Restore the project to change it."
            : "The project’s Admins choose what it takes."}
        </p>
      </>
    );
  } else if (!requestTypes(types.data).length && !found.data.types.length) {
    body = (
      <p className="muted">
        No installed plugin has request types: types whose records one project sends another’s
        service, such as a core facility’s.
      </p>
    );
  } else {
    body = (
      <IntakeForm
        // A fresh form once it's saved: its choices are the saved ones.
        key={JSON.stringify(found.data)}
        project={project}
        intake={found.data}
        types={types.data}
      />
    );
  }
  return (
    <section className="card">
      <h2 className="section-title">Requests it takes</h2>
      <p className="muted">
        Other projects can send it requests, such as samples to measure, if it takes them. Each
        request stays its asking project’s: {project.name} reads it and what it sends, and fills in
        the fields its type gives the service.
      </p>
      {body}
    </section>
  );
}

function IntakeForm({
  project,
  intake,
  types,
}: {
  project: Project;
  intake: IntakeSettings;
  types: EntityType[];
}) {
  const offered = requestTypes(types);
  const { say } = useMessages();
  const [chosen, setChosen] = useState<string[]>(intake.types.map((t) => t.name));
  const [from, setFrom] = useState<From>(intake.from_registry ? "registry" : "projects");
  const [named, setNamed] = useState<string[]>(intake.projects.map((p) => p.id));
  const seen = useQuery(projectsQuery);
  const save = useApiMutation(
    async () =>
      unwrap(
        await iki.PUT("/api/v1/projects/{project_id}/intake", {
          params: { path: { project_id: project.id } },
          body: {
            types: chosen,
            from_registry: from === "registry",
            projects: from === "registry" ? [] : named,
          },
        }),
      ),
    [intakeQuery(project.id).queryKey],
  );
  const problem = save.error ? problemOf(save.error) : undefined;
  const errors = Object.entries(fieldErrors(problem));
  const errorOf = (key: string) => errors.find(([k]) => k.startsWith(`${key}/`))?.[1];
  // Types it took that aren't installed as request types any more still show, to be dropped.
  const options = [
    ...offered.map((t) => ({
      value: t.name,
      label: t.abstract ? `${t.label} (any kind)` : t.label,
      ...(t.description ? { description: t.description } : {}),
    })),
    ...intake.types
      .filter((t) => !offered.some((o) => o.name === t.name))
      .map((t) => ({ value: t.name, label: t.label, description: "Not a request type any more." })),
  ];
  // Projects in its registry: requests stay within one (ADR 0115). Each with its team.
  const projects = [
    ...(seen.data ?? [])
      .filter((p) => p.id !== project.id && p.registry.id === project.registry.id && !p.archived)
      .map((p) => ({ value: p.id, label: p.name, description: p.team.name })),
    ...intake.projects
      .filter((p) => !seen.data?.some((s) => s.id === p.id))
      .map((p) => ({ value: p.id, label: p.name })),
  ].sort((a, b) => a.label.localeCompare(b.label));
  const unchanged =
    JSON.stringify([...chosen].sort()) === JSON.stringify(intake.types.map((t) => t.name).sort()) &&
    (from === "registry") === intake.from_registry &&
    (from === "registry" ||
      JSON.stringify([...named].sort()) ===
        JSON.stringify(intake.projects.map((p) => p.id).sort()));
  return (
    <>
      {problem && !errors.length ? <ProblemAlert problem={problem} /> : null}
      <form
        noValidate
        onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
          event.preventDefault();
          save.mutate(undefined, {
            onSuccess: (saved) => {
              say(takesWhat(project, saved, types).replace(/\.$/, " now."));
            },
          });
        }}
      >
        <CheckboxesField
          label="Request types"
          value={chosen}
          onChange={setChosen}
          options={options}
          literal={false}
          error={errorOf("types")}
          help="A type takes its subtypes too. None: it takes no requests."
        />
        <RadioField<From>
          label="From"
          value={from}
          onChange={setFrom}
          options={[
            { value: "registry", label: `Every project in the ${project.registry.name} registry` },
            { value: "projects", label: "Only the projects chosen here" },
          ]}
        />
        {from === "projects" ? (
          seen.isError ? (
            <ProblemAlert problem={problemOf(seen.error)} />
          ) : !seen.data ? (
            <Loading label="Finding the projects…" />
          ) : projects.length ? (
            <CheckboxesField
              label="Projects"
              value={named}
              onChange={setNamed}
              options={projects}
              literal={false}
              error={errorOf("projects")}
              help={`Projects you can see in the ${project.registry.name} registry: requests stay within one.`}
            />
          ) : (
            <p className="muted">
              There’s no other project you can see in the {project.registry.name} registry.
            </p>
          )
        ) : null}
        <p className="muted">Requests already sent stay, whatever it takes from now on.</p>
        <Button
          type="submit"
          variant="primary"
          busy={save.isPending}
          busyText="Saving…"
          disabled={unchanged}
        >
          Save
        </Button>
      </form>
    </>
  );
}

/** On the project's page, for a project that takes requests or was sent some: what it
 * takes, and its queue, the requests sent to it. */
export function ProjectRequests({ project }: { project: Project }) {
  const found = useQuery(intakeQuery(project.id));
  const queued = useQuery(queuedQuery(project.id));
  const types = useQuery(typesQuery).data;
  const intake = found.data;
  if (!intake || (!intake.types.length && !queued.data)) return null;
  return (
    <section className="card" aria-labelledby="requests-title">
      <h2 className="section-title" id="requests-title">
        Requests
      </h2>
      <p>{takesWhat(project, intake, types)}</p>
      <p>
        {/* A shortcut to its Requests tab, as the service sees it. */}
        <Link
          to="/projects/$slug/requests/"
          params={{ slug: project.slug }}
          search={{ side: "received" }}
        >
          Requests sent to {project.name}
        </Link>
        {project.can.manage && !project.archived ? (
          <>
            {" · "}
            <Link to="/projects/$slug/settings/" params={{ slug: project.slug }}>
              Choose what it takes
            </Link>
          </>
        ) : null}
      </p>
    </section>
  );
}
