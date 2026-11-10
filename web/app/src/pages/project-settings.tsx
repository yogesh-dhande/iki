import { unwrap } from "@ikisci/sdk";
import { useNavigate } from "@tanstack/react-router";
import { useState, type SubmitEvent } from "react";

import { iki, problemOf, useApiMutation, type Project } from "../api";
import { required, useForm } from "../forms";
import { usePageTitle } from "../layout";
import { Button, ConfirmDialog, ProblemAlert, TextField, useMessages } from "../ui";
import { useProject } from "./project-overview";

function usePatchProject(project: Project) {
  return useApiMutation(
    async (patch: { name?: string; description?: string; archived?: boolean }) =>
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
