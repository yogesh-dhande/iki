/**
 * Previewing an app (ADR 0090) as it's written, before saving it: below the form that
 * makes or changes one. The server checks the spec as saving it would; then its panels
 * show here, with what the person may read. An app about an entity previews on one they
 * name.
 */

import { unwrap } from "@ikisci/sdk";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { iki, problemOf, useApiMutation } from "../api";
import { readSpec, type AppSpec } from "../apps";
import { entityQuery, type Entity, type EntityType } from "../entities";
import { encode, formFields, type Draft } from "../entity-values";
import { Button, ProblemAlert, TextField } from "../ui";
import { AppPanel } from "./app";

interface Shown {
  spec: AppSpec;
  about?: Entity;
  /** Each preview its own, so its grids search afresh. */
  key: number;
}

export function AppPreview({
  type,
  types,
  drafts,
  project,
}: {
  type: EntityType;
  types: EntityType[];
  drafts: Record<string, Draft>;
  project: { id: string; name: string; slug: string };
}) {
  const client = useQueryClient();
  const [on, setOn] = useState("");
  const [shown, setShown] = useState<Shown>();
  const [problems, setProblems] = useState<string[]>([]);
  const field = formFields(type).find((f) => f.name === "spec");
  const encoded = field ? encode(field, drafts["spec"] ?? null, type) : undefined;
  const about = encoded?.ok ? readSpec(encoded.value)?.entity : undefined;
  const check = useApiMutation(async (spec: unknown) => {
    const found = unwrap(await iki.POST("/api/v1/apps/check", { body: { spec } }));
    if (!about || found.problems.length) return { problems: found.problems };
    const entity = await client.query(entityQuery(on.trim()));
    // The app is about a type and the ones that extend it, as the server lists its tabs.
    const kinds = [about.type, ...(types.find((t) => t.name === about.type)?.subtypes ?? [])];
    if (!kinds.includes(entity.type)) {
      return { problems: [`${entity.hrid} isn’t a ${about.type}, so it can’t show this app.`] };
    }
    return { problems: [], entity };
  }, []);

  function preview() {
    if (!encoded?.ok) {
      setShown(undefined);
      setProblems([encoded?.error ?? "Write its spec first."]);
      return;
    }
    const spec = encoded.value;
    check.mutate(spec, {
      onSuccess: ({ problems: found, entity }) => {
        setProblems(found);
        const read = readSpec(spec);
        setShown(
          found.length === 0 && read
            ? { spec: read, ...(entity ? { about: entity } : {}), key: Date.now() }
            : undefined,
        );
      },
    });
  }

  return (
    <section className="dashboard app-preview" aria-labelledby="app-preview-title">
      <div className="dashboard-heading">
        <h2 className="section-title" id="app-preview-title">
          Preview
        </h2>
        <span className="muted">
          As it would be saved, with what you can read. Nothing is saved.
        </span>
      </div>
      <form
        className="actions"
        onSubmit={(event) => {
          event.preventDefault();
          preview();
        }}
      >
        {about ? (
          <TextField
            label={`Show it on (the ID of one ${about.type} in ${project.name})`}
            value={on}
            onChange={setOn}
            maxLength={64}
          />
        ) : null}
        <Button
          type="submit"
          busy={check.isPending}
          busyText="Checking…"
          disabled={Boolean(about) && !on.trim()}
        >
          {shown ? "Preview again" : "Preview"}
        </Button>
      </form>
      <ProblemAlert problem={check.error ? problemOf(check.error) : undefined} />
      {problems.length ? (
        <div className="alert alert-warning" role="alert">
          <p>Saving it would say:</p>
          <ul>
            {problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {shown ? (
        <AppPanel
          key={shown.key}
          app={{
            id: `preview-${String(shown.key)}`,
            values: { spec: shown.spec },
            owner_project: project,
          }}
          types={types}
          {...(shown.about ? { about: shown.about } : {})}
        />
      ) : null}
    </section>
  );
}
