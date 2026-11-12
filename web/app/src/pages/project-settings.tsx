import { unwrap } from "@ikisci/sdk";
import { useNavigate } from "@tanstack/react-router";
import { useState, type SubmitEvent } from "react";

import { iki, problemOf, useApiMutation, type Project } from "../api";
import { required, useForm } from "../forms";
import { usePageTitle } from "../layout";
import { Button, CheckboxField, ConfirmDialog, ProblemAlert, TextField, useMessages } from "../ui";
import { useProject } from "./project-overview";

function usePatchProject(project: Project) {
  return useApiMutation(
    async (patch: {
      name?: string;
      description?: string;
      archived?: boolean;
      allow_sharing?: boolean;
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
      <section className="card">
        <p>Only the project’s Admins can change its settings.</p>
      </section>
    );
  }
  return (
    <div className="columns">
      <Details project={project} />
      <Sharing project={project} />
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
