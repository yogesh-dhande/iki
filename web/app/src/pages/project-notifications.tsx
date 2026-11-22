/**
 * How you're told of types' notifications (ADR 0129) in one project: a dialog from its
 * header, where each can follow your choice everywhere or differ here, as Slack's
 * per-channel notification preferences do.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  chooseInProject,
  notificationSettingsQuery,
  problemOf,
  useApiMutation,
  type NotifyBy,
  type NotificationSettings,
  type Project,
} from "../api";
import { Button, ConfirmDialog, SelectField, useMessages } from "../ui";

type Choice = NotifyBy | "everywhere";

const LABEL: Record<NotifyBy, string> = {
  email: "In Iki and by email",
  in_app: "In Iki only",
  off: "Off",
};

type Kind = NotificationSettings["kinds"][number];

function chosenHere(settings: NotificationSettings, project: string): Record<string, Choice> {
  const here: Record<string, Choice> = {};
  for (const row of settings.projects) {
    if (row.project.id === project) here[row.kind] = row.delivery;
  }
  return here;
}

function optionsFor(kind: Kind) {
  return [
    { value: "everywhere" as const, label: `As everywhere: ${LABEL[kind.delivery]}` },
    { value: "email" as const, label: LABEL.email },
    { value: "in_app" as const, label: LABEL.in_app },
    ...(kind.can_turn_off ? [{ value: "off" as const, label: LABEL.off }] : []),
  ];
}

/** The project header's button. Nothing when no type declares a notification. */
export function ProjectNotifications({ project }: { project: Project }) {
  const settings = useQuery({ ...notificationSettingsQuery, enabled: project.can.read });
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Record<string, Choice>>({});
  const client = useQueryClient();
  const { say } = useMessages();
  const save = useApiMutation(
    (choices: { kind: string; delivery: NotifyBy | null }[]) =>
      chooseInProject(project.id, choices),
    [],
  );
  const found = settings.data;
  const kinds = found?.kinds.filter((k) => k.per_project) ?? [];
  if (!found || kinds.length === 0) return null;
  const close = () => {
    setOpen(false);
    save.reset();
  };
  const here = chosenHere(found, project.id);
  return (
    <>
      {/* As GitHub's Watch: how you're told of what happens in the project (ADR 0176). */}
      <Button
        label={`Watch ${project.name}`}
        onClick={() => {
          setDraft(here);
          setOpen(true);
        }}
      >
        <svg
          className="icon"
          viewBox="0 0 24 24"
          aria-hidden="true"
          focusable="false"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.9"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" />
          <circle cx="12" cy="12" r="3" />
        </svg>
        Watch
      </Button>
      <ConfirmDialog
        isOpen={open}
        title={`Notifications in ${project.name}`}
        confirmLabel="Save"
        confirmVariant="primary"
        busy={save.isPending}
        problem={save.error ? problemOf(save.error) : undefined}
        onCancel={close}
        onConfirm={() => {
          const changed = kinds
            .filter((k) => (draft[k.kind] ?? "everywhere") !== (here[k.kind] ?? "everywhere"))
            .map((k) => {
              const choice = draft[k.kind] ?? "everywhere";
              return { kind: k.kind, delivery: choice === "everywhere" ? null : choice };
            });
          if (changed.length === 0) {
            close();
            return;
          }
          save.mutate(changed, {
            onSuccess: (saved) => {
              client.setQueryData(notificationSettingsQuery.queryKey, saved);
              say(`Saved how you’re told in ${project.name}.`);
              close();
            },
          });
        }}
      >
        <p className="muted">
          How you’re told of what its records’ types send, in this project. Iki’s own notifications,
          and your choices everywhere, are in{" "}
          <a href="/account/notifications/">your notification settings</a>.
        </p>
        {kinds.map((kind) => (
          <SelectField
            key={kind.kind}
            label={`${kind.group}: ${kind.label}`}
            value={draft[kind.kind] ?? "everywhere"}
            onChange={(value) => {
              setDraft({ ...draft, [kind.kind]: value });
            }}
            options={optionsFor(kind)}
            {...(kind.required
              ? { help: "Its plugin requires it, so it’s always shown in Iki." }
              : kind.description
                ? { help: kind.description }
                : {})}
          />
        ))}
      </ConfirmDialog>
    </>
  );
}
