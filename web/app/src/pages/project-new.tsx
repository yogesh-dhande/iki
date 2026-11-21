import { unwrap } from "@ikisci/sdk";
import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useState, type SubmitEvent } from "react";

import { creatableTeamsQuery, iki, meQuery, useApiMutation } from "../api";
import { required, useForm } from "../forms";
import { usePageTitle } from "../layout";
import { registriesQuery, type Registry } from "../registries";
import { Button, buttonClass, PageTitle, SelectField, TextField, useMessages } from "../ui";

/** The registry a new project of the team is in, unless it's given another: the
 * deployment's default, if the team may use it. */
function preferred(registries: Registry[]): string {
  return (registries.find((r) => r.default) ?? registries[0])?.id ?? "";
}

export function ProjectNew() {
  usePageTitle("New project");
  const me = useSuspenseQuery(meQuery).data;
  const teams = useSuspenseQuery(creatableTeamsQuery).data;
  const navigate = useNavigate();
  const { say } = useMessages();
  const { team: preselected } = useSearch({ from: "/projects/new" });
  const [team, setTeam] = useState(
    (teams.find((t) => t.slug === preselected) ?? teams[0])?.id ?? "",
  );
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  // The registries the team's projects may be in (ADR 0115): a choice only when there are
  // several. Until they're listed, the project goes in the default one.
  const registries = useQuery({ ...registriesQuery(team), enabled: Boolean(team) }).data ?? [];
  const [picked, setPicked] = useState<{ team: string; registry: string }>();
  const registry =
    picked?.team === team && registries.some((r) => r.id === picked.registry)
      ? picked.registry
      : preferred(registries);

  const create = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/projects", {
          body: { team, name: name.trim(), description, ...(registry ? { registry } : {}) },
        }),
      ),
    [["projects"], ["me"]],
  );
  const form = useForm(create.error);
  const { errors } = form;

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form.validate({ name: required(name, "Enter a name for the project.") })) return;
    create.mutate(undefined, {
      onSuccess: (project) => {
        void navigate({ to: "/projects/$slug/", params: { slug: project.slug } }).then(() => {
          say(`Created ${project.name}. You’re its Admin.`);
        });
      },
    });
  }

  return (
    <section className="card">
      <PageTitle>New project</PageTitle>
      {teams.length > 0 ? (
        <>
          <p>You’ll be the project’s Admin and can add others.</p>
          {form.alert}
          <form {...form.props} onSubmit={submit}>
            <SelectField
              label="Team"
              value={team}
              onChange={setTeam}
              options={teams.map((t) => ({ value: t.id, label: t.name }))}
              error={errors["team"]}
              help="The team’s compute limits apply to the project, and the team pays for its runs."
            />
            <TextField
              label="Project name"
              value={name}
              onChange={setName}
              error={errors["name"]}
              maxLength={100}
            />
            <TextField
              label="Description"
              value={description}
              onChange={setDescription}
              error={errors["description"]}
              maxLength={2000}
              optional
              multiline
            />
            {registries.length > 1 ? (
              <SelectField
                label="Registry"
                value={registry}
                onChange={(value) => {
                  setPicked({ team, registry: value });
                }}
                options={registries.map((r) => ({
                  value: r.id,
                  label: r.default ? `${r.name} (the default)` : r.name,
                }))}
                error={errors["registry"]}
                help={[
                  "What the project registers, such as a compound or a sequence, has one ID in its registry, shared with the registry’s other projects. A separate registry keeps a client’s or partner’s work apart. It can’t change once the project has entities.",
                  registries.find((r) => r.id === registry)?.description,
                ]
                  .filter(Boolean)
                  .join(" ")}
              />
            ) : null}

            <div className="actions">
              <Button type="submit" variant="primary" busy={create.isPending} busyText="Creating…">
                Create project
              </Button>
              <Link to="/projects/" className={buttonClass("quiet")}>
                Cancel
              </Link>
            </div>
          </form>
        </>
      ) : (
        <>
          <p>Projects belong to teams, and you’re not in a team yet.</p>
          <p>
            {me.can.manage_org ? (
              <>
                <a href="/teams/">Create a team</a> and add yourself to it.
              </>
            ) : (
              "Ask an Org Admin to add you to a team."
            )}
          </p>
        </>
      )}
    </section>
  );
}
