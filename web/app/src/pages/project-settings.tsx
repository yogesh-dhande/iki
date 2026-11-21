import { unwrap } from "@ikisci/sdk";
import { useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useState, type SubmitEvent } from "react";

import { iki, meQuery, problemOf, useApiMutation, type Project } from "../api";
import { APP_TYPE, dashboardChoicesQuery, dashboardKey, dashboardQuery } from "../apps";
import { required, useForm } from "../forms";
import { usePageTitle } from "../layout";
import { registriesQuery, REGISTRY_SETTINGS } from "../registries";
import {
  Button,
  CheckboxField,
  ConfirmDialog,
  Loading,
  ProblemAlert,
  SelectField,
  TextField,
  useMessages,
} from "../ui";
import { Exports } from "./project-exports";
import { Imports } from "./project-imports";
import { Intake } from "./project-intake";
import { useProject } from "./project-overview";

function usePatchProject(project: Project) {
  return useApiMutation(
    async (patch: {
      name?: string;
      description?: string;
      archived?: boolean;
      allow_sharing?: boolean;
      registry?: string;
    }) =>
      unwrap(
        await iki.PATCH("/api/v1/projects/{project_id}", {
          params: { path: { project_id: project.id } },
          body: patch,
        }),
      ),
    [["projects"]],
  );
}

export function ProjectSettings() {
  const project = useProject();
  usePageTitle(`Settings · ${project.name}`);
  if (!project.can.manage) {
    return (
      <div className="columns">
        <section className="card">
          <p>
            Only the project’s Admins can change its {project.can.change_registry ? "other " : ""}
            settings.
          </p>
        </section>
        {project.can.change_registry ? <RegistrySetting project={project} /> : null}
        {project.can.read ? <Intake project={project} /> : null}
      </div>
    );
  }
  return (
    <div className="columns">
      <Details project={project} />
      <RegistrySetting project={project} />
      <Dashboard project={project} />
      <Sharing project={project} />
      <Intake project={project} />
      {project.can.export ? <Exports project={project} /> : null}
      {project.can.import_bundles && !project.archived ? <Imports project={project} /> : null}
      {project.archived ? <Restore project={project} /> : <Archive project={project} />}
    </div>
  );
}

function Details({ project }: { project: Project }) {
  const navigate = useNavigate();
  const { say } = useMessages();
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description);
  const save = usePatchProject(project);
  const form = useForm(save.error);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!form.validate({ name: required(name, "Enter a name for the project.") })) return;
    save.mutate(
      { name: name.trim(), description },
      {
        onSuccess: () => {
          void navigate({ to: "/projects/$slug/", params: { slug: project.slug } }).then(() => {
            say("Saved.");
          });
        },
      },
    );
  }

  return (
    <section className="card">
      <h2 className="section-title">Details</h2>
      {project.archived ? (
        <p className="muted">Restore the project to change its details.</p>
      ) : (
        <>
          {form.alert}
          <form {...form.props} onSubmit={submit}>
            <TextField
              label="Project name"
              value={name}
              onChange={setName}
              error={form.errors["name"]}
              maxLength={100}
            />
            <TextField
              label="Description"
              value={description}
              onChange={setDescription}
              error={form.errors["description"]}
              maxLength={2000}
              optional
              multiline
            />
            <Button type="submit" variant="primary" busy={save.isPending} busyText="Saving…">
              Save
            </Button>
          </form>
        </>
      )}
    </section>
  );
}

/** The app the project's page opens with (ADR 0090), for everyone who can read it: one
 * of its apps that isn't about an entity. */
function Dashboard({ project }: { project: Project }) {
  const { say } = useMessages();
  const current = useQuery(dashboardQuery(project.id));
  const choices = useQuery(dashboardChoicesQuery(project.id));
  const [picked, setPicked] = useState<string>();
  const save = useApiMutation(
    async (hrid: string) => {
      if (hrid) {
        unwrap(
          await iki.PUT("/api/v1/projects/{project_id}/dashboard", {
            params: { path: { project_id: project.id } },
            body: { app: hrid },
          }),
        );
      } else {
        unwrap(
          await iki.DELETE("/api/v1/projects/{project_id}/dashboard", {
            params: { path: { project_id: project.id } },
          }),
        );
      }
    },
    [dashboardKey(project.id)],
  );
  const shown = current.data?.app?.hrid ?? "";
  const value = picked ?? shown;
  let body: React.ReactNode;
  if (current.isError || choices.isError) {
    body = <ProblemAlert problem={problemOf(current.error ?? choices.error)} />;
  } else if (!current.data || !choices.data) {
    body = <Loading label="Finding its apps…" />;
  } else if (project.archived) {
    body = (
      <p className="muted">
        {current.data.app ? `It opens with ${current.data.app.name}. ` : null}Restore the project to
        change it.
      </p>
    );
  } else {
    const hidden = current.data.set_by && !current.data.app;
    body = (
      <>
        {hidden ? (
          <p className="muted">
            The app chosen for it was archived or taken out of the project, so the page shows none.
          </p>
        ) : null}
        {choices.data.length === 0 && !shown ? (
          <p className="muted">
            The project has no apps to show yet. An app lays out grids, viewers and text;{" "}
            <Link
              to="/projects/$slug/entities/new/"
              params={{ slug: project.slug }}
              search={{ type: APP_TYPE }}
            >
              make one
            </Link>
            .
          </p>
        ) : (
          <>
            <ProblemAlert problem={save.error ? problemOf(save.error) : undefined} />
            <form
              onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
                event.preventDefault();
                save.mutate(value, {
                  onSuccess: () => {
                    setPicked(undefined);
                    const app = choices.data.find((a) => a.hrid === value);
                    say(
                      app
                        ? `${project.name}’s page opens with ${app.name} now.`
                        : `${project.name}’s page opens without an app now.`,
                    );
                  },
                });
              }}
            >
              <SelectField
                label="Open the project’s page with"
                value={value}
                onChange={setPicked}
                options={[
                  { value: "", label: "No app" },
                  ...choices.data.map((a) => ({ value: a.hrid, label: `${a.name} (${a.hrid})` })),
                ]}
                help="Everyone who can read the project sees it above its summary. Apps about one entity are tabs on their pages instead."
              />
              <Button
                type="submit"
                variant="primary"
                busy={save.isPending}
                busyText="Saving…"
                disabled={value === shown && !hidden}
              >
                Save
              </Button>
            </form>
          </>
        )}
      </>
    );
  }
  return (
    <section className="card">
      <h2 className="section-title">Dashboard</h2>
      {body}
    </section>
  );
}

/** Whether the project's entities may be shared with other projects (ADR 0067). Turning
 * sharing on lets its data leave the project, so it's confirmed; turning it off isn't. */
function Sharing({ project }: { project: Project }) {
  const { say } = useMessages();
  const [allowed, setAllowed] = useState(project.allow_sharing);
  const [confirming, setConfirming] = useState(false);
  const save = usePatchProject(project);

  function apply() {
    save.mutate(
      { allow_sharing: allowed },
      {
        onSuccess: () => {
          setConfirming(false);
          say(
            allowed
              ? `${project.name}’s entities can be shared with other projects.`
              : `${project.name}’s entities can’t be shared outside it now.`,
          );
        },
      },
    );
  }

  return (
    <section className="card">
      <h2 className="section-title">Sharing</h2>
      {confirming ? null : (
        <ProblemAlert problem={save.error ? problemOf(save.error) : undefined} />
      )}
      <form
        onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
          event.preventDefault();
          if (allowed && !project.allow_sharing) {
            save.reset();
            setConfirming(true);
          } else {
            apply();
          }
        }}
      >
        <CheckboxField
          label="Allow sharing this project’s entities with other projects"
          checked={allowed}
          onChange={setAllowed}
          help="When off, no one can add this project’s entities to another project, or use them in runs there. Use this for partner or CRO data and blinded studies. Entities already shared stay shared until you remove them."
        />
        <Button
          type="submit"
          variant="primary"
          busy={save.isPending && !confirming}
          busyText="Saving…"
          disabled={allowed === project.allow_sharing}
        >
          Save
        </Button>
      </form>
      <ConfirmDialog
        isOpen={confirming}
        title={`Allow sharing ${project.name}’s entities?`}
        confirmLabel="Allow sharing"
        busy={save.isPending}
        problem={save.error ? problemOf(save.error) : undefined}
        onCancel={() => {
          setConfirming(false);
        }}
        onConfirm={apply}
      >
        <p>
          Anyone who can read them will be able to add them to other projects they edit, and use
          them in runs there. Those projects’ members will then be able to read them.
        </p>
      </ConfirmDialog>
    </section>
  );
}

/**
 * The project's registry (ADR 0115): where what it registers gets its IDs. Org Admins
 * choose it while the project has no entities; after that it's fixed.
 */
function RegistrySetting({ project }: { project: Project }) {
  const me = useSuspenseQuery(meQuery).data;
  const { say } = useMessages();
  const can = project.can.change_registry;
  const choices = useQuery({ ...registriesQuery(project.team.id), enabled: can });
  const [picked, setPicked] = useState<string>();
  const save = usePatchProject(project);
  const value = picked ?? project.registry.id;
  const manage = me.can.manage_org ? (
    <p>
      <a href={REGISTRY_SETTINGS}>Manage registries</a>
    </p>
  ) : null;
  const about = (
    <p>
      {project.name} is in the <strong>{project.registry.name}</strong> registry. What it registers,
      such as a compound or a sequence, has one ID there, shared with the registry’s other projects;
      each keeps its own record of it.
    </p>
  );
  if (!can) {
    return (
      <section className="card">
        <h2 className="section-title">Registry</h2>
        {about}
        <p className="muted">
          {me.can.manage_org
            ? "It’s fixed now: a project’s registry can’t change once it has entities."
            : "Org Admins choose a project’s registry, until it has entities. After that, it’s fixed."}
        </p>
        {manage}
      </section>
    );
  }
  let body: React.ReactNode;
  if (choices.isError) {
    body = <ProblemAlert problem={problemOf(choices.error)} />;
  } else if (!choices.data) {
    body = <Loading label="Finding the registries it may use…" />;
  } else if (choices.data.length < 2) {
    body = (
      <p className="muted">
        {project.team.name}’s projects may use no other registry. Org Admins choose which teams use
        each.
      </p>
    );
  } else {
    body = (
      <>
        <ProblemAlert problem={save.error ? problemOf(save.error) : undefined} />
        <form
          onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
            event.preventDefault();
            save.mutate(
              { registry: value },
              {
                onSuccess: (changed) => {
                  setPicked(undefined);
                  say(`${changed.name} is in the ${changed.registry.name} registry now.`);
                },
              },
            );
          }}
        >
          <SelectField
            label="Registry"
            value={value}
            onChange={setPicked}
            options={choices.data.map((r) => ({
              value: r.id,
              label: r.default ? `${r.name} (the default)` : r.name,
            }))}
            help="It can change only while the project has no entities: after its first, it’s fixed."
          />
          <Button
            type="submit"
            variant="primary"
            busy={save.isPending}
            busyText="Saving…"
            disabled={value === project.registry.id}
          >
            Save
          </Button>
        </form>
      </>
    );
  }
  return (
    <section className="card">
      <h2 className="section-title">Registry</h2>
      {about}
      {body}
      {manage}
    </section>
  );
}

function Archive({ project }: { project: Project }) {
  const navigate = useNavigate();
  const { say } = useMessages();
  const [confirming, setConfirming] = useState(false);
  const archive = usePatchProject(project);
  return (
    <section className="card">
      <h2 className="section-title">Archive</h2>
      <p>
        Archiving makes the project read-only and hides it from home pages. Nothing is deleted, and
        you can restore it at any time.
      </p>
      <Button
        variant="danger"
        onClick={() => {
          archive.reset();
          setConfirming(true);
        }}
      >
        Archive project
      </Button>
      <ConfirmDialog
        isOpen={confirming}
        title={`Archive ${project.name}?`}
        confirmLabel="Archive project"
        busy={archive.isPending}
        problem={archive.error ? problemOf(archive.error) : undefined}
        onCancel={() => {
          setConfirming(false);
        }}
        onConfirm={() => {
          archive.mutate(
            { archived: true },
            {
              onSuccess: () => {
                setConfirming(false);
                void navigate({ to: "/projects/$slug/", params: { slug: project.slug } }).then(
                  () => {
                    say(`Archived ${project.name}.`);
                  },
                );
              },
            },
          );
        }}
      >
        <p>Its members can still see it, but no one can change it until it’s restored.</p>
      </ConfirmDialog>
    </section>
  );
}

function Restore({ project }: { project: Project }) {
  const navigate = useNavigate();
  const { say } = useMessages();
  const restore = usePatchProject(project);
  return (
    <section className="card">
      <h2 className="section-title">Restore</h2>
      <p>Restoring makes the project editable again for its members.</p>
      <ProblemAlert problem={restore.error ? problemOf(restore.error) : undefined} />
      <Button
        variant="primary"
        busy={restore.isPending}
        busyText="Restoring…"
        onClick={() => {
          restore.mutate(
            { archived: false },
            {
              onSuccess: () => {
                void navigate({ to: "/projects/$slug/", params: { slug: project.slug } }).then(
                  () => {
                    say(`Restored ${project.name}.`);
                  },
                );
              },
            },
          );
        }}
      >
        Restore project
      </Button>
    </section>
  );
}
