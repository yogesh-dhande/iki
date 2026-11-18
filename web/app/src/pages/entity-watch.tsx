/**
 * Watching a record, or muting it (ADR 0129), as GitHub's subscribe and Jira's watch do:
 * a button saying which, opening a dialog to choose.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  chooseWatch,
  problemOf,
  useApiMutation,
  watchQuery,
  type WatchMode,
  type Watching,
} from "../api";
import type { Entity } from "../entities";
import { Button, ConfirmDialog, RadioField, useMessages } from "../ui";

const BUTTON: Record<WatchMode, string> = {
  default: "Watch",
  watching: "Watching",
  muted: "Muted",
};

const SAID: Record<WatchMode, string> = {
  default: "You’re told of it again as it names you.",
  watching: "You’re watching it.",
  muted: "You’ve muted it.",
};

function options(watching: Watching) {
  return [
    {
      value: "default" as const,
      label: "As it names me",
      description:
        "Told what its type says to tell the people its fields name, its creator, or a lab role I hold.",
    },
    ...(watching.can_watch || watching.mode === "watching"
      ? [
          {
            value: "watching" as const,
            label: "Watching",
            description: "Also told what its type tells its watchers.",
            disabled: !watching.can_watch,
          },
        ]
      : []),
    ...(watching.can_mute || watching.mode === "muted"
      ? [
          {
            value: "muted" as const,
            label: "Muted",
            description: "Told nothing of it but what its plugin requires.",
            disabled: !watching.can_mute,
          },
        ]
      : []),
  ];
}

/** The record's watch button. Nothing for an entry, or a type that tells nobody anything. */
export function EntityWatch({ entity }: { entity: Entity }) {
  const ref = entity.id;
  const found = useQuery({ ...watchQuery(ref), enabled: entity.owner_project !== null });
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<WatchMode>("default");
  const client = useQueryClient();
  const { say } = useMessages();
  const change = useApiMutation((chosen: WatchMode) => chooseWatch(ref, chosen), []);
  const watching = found.data;
  if (!watching || (!watching.can_watch && !watching.can_mute && watching.mode === "default")) {
    return null;
  }
  const close = () => {
    setOpen(false);
    change.reset();
  };
  return (
    <>
      <Button
        variant="quiet"
        onClick={() => {
          setMode(watching.mode);
          setOpen(true);
        }}
      >
        {BUTTON[watching.mode]}
        <span className="visually-hidden">: notifications about {entity.hrid}</span>
      </Button>
      <ConfirmDialog
        isOpen={open}
        title={`Notifications about ${entity.hrid}`}
        confirmLabel="Save"
        confirmVariant="primary"
        busy={change.isPending}
        problem={change.error ? problemOf(change.error) : undefined}
        onCancel={close}
        onConfirm={() => {
          change.mutate(mode, {
            onSuccess: (saved) => {
              client.setQueryData(watchQuery(ref).queryKey, saved);
              say(SAID[saved.mode]);
              close();
            },
          });
        }}
      >
        <RadioField
          label="Tell me"
          value={mode}
          onChange={setMode}
          options={options(watching)}
          help="Only while you can read it."
        />
      </ConfirmDialog>
    </>
  );
}
