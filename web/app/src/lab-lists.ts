/**
 * Lab lists (ADR 0270): the dropdowns a lab curates itself, such as its purification
 * methods. A list's options are its registry's: every project in the registry picks from
 * the same ones, in the list's order. A field that uses one is a reference to the list's
 * type, which a form shows as a dropdown of the active options; a record that names an
 * option archived or superseded since keeps it, and says so.
 *
 * Its registry's registrars and the list's curators add, relabel, reorder, archive and
 * supersede options, each with a reason. Org Admins appoint the curators, a list at a time:
 * a registry role (ADR 0292), as a registrar's is.
 */

import { unwrap, type Schemas } from "@ikisci/sdk";
import { queryOptions, useQueries, useQuery } from "@tanstack/react-query";

import { iki } from "./api";
import { ENTITIES, type EntityType, type FieldDef } from "./entities";
import { registriesQuery } from "./registries";

export type LabList = Schemas["ListOut"];
export type ListOption = Schemas["OptionOut"];
export type RegistryRole = Schemas["RegistryRoleOut"];

/** Whose lists: a project's, which its records pick from, or a registry's, as its
 * curators and Org Admins read them. */
export type ListScope = { project: string } | { registry: string };

/** Under ENTITIES: a record's change changes how often an option is used, and an option's
 * change changes how records read. */
export const LAB_LISTS = [...ENTITIES, "lab-lists"] as const;

/** More options than this, and a dropdown is searched as people type, rather than
 * scrolled: as Benchling's dropdowns and GitHub's pickers do once a list is long. */
export const LONG_LIST = 15;

/** The most of a long list's matches its search shows at once, saying how many more there
 * are: as GitHub's pickers do. A list may have thousands of options, and nobody scrolls
 * through them; they type a few letters of the one they want. */
export const SHOWN_MATCHES = 50;

/** What a search shows of its matches, when it doesn't show them all. */
export function narrowText(shown: number, matched: number): string {
  return `Showing ${shown.toLocaleString()} of ${matched.toLocaleString()}. Type to narrow them.`;
}

/** The built-in fields every option has (ADR 0270), before its list's own. */
export const OPTION_FIELDS = ["code", "label", "description", "position"] as const;

function query(scope: ListScope): { project: string } | { registry: string } {
  return "project" in scope ? { project: scope.project } : { registry: scope.registry };
}

function scopeKey(scope: ListScope): string {
  return "project" in scope ? `project:${scope.project}` : `registry:${scope.registry}`;
}

/** The lab list a reference field picks from: its only target, if that's a list. */
export function listOf(field: FieldDef, types: readonly EntityType[]): EntityType | undefined {
  if (field.type !== "ref" || field.to.length !== 1) return undefined;
  const found = types.find((t) => t.name === field.to[0]);
  return found?.lab_list ? found : undefined;
}

/** A list field's item, or the field itself: what `listOf` asks of a list of references. */
export function listOfItems(field: FieldDef, types: readonly EntityType[]): EntityType | undefined {
  if (field.type === "list" && field.items === "ref") {
    return listOf({ ...field, type: "ref" }, types);
  }
  return listOf(field, types);
}

/** One list, in a registry: its registry, how many options it offers, whether the reader
 * curates it, whether its plugin is detached. */
export function labListQuery(name: string, scope: ListScope) {
  return queryOptions({
    queryKey: [...LAB_LISTS, "list", name, scopeKey(scope)],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/lists/{name}", {
          params: { path: { name }, query: query(scope) },
          signal,
        }),
      ),
  });
}

/**
 * A list's options, in its order: the active ones a dropdown offers, or with `every`,
 * archived and superseded ones too, as records may still name them. A list has 10,000
 * options at most (LIST_MAX_OPTIONS), so it's read whole and searched in the browser, which
 * shows a few of its matches at a time (SHOWN_MATCHES).
 */
export function optionsQuery(name: string, scope: ListScope, every = false) {
  return queryOptions({
    queryKey: [...LAB_LISTS, "options", name, scopeKey(scope), every],
    queryFn: async ({ signal }) =>
      unwrap(
        await iki.GET("/api/v1/lists/{name}/options", {
          params: { path: { name }, query: { ...query(scope), archived: every } },
          signal,
        }),
      ),
    staleTime: 30_000,
  });
}

/** A list's own curators in a registry, every page of them: a registry role (ADR 0292).
 * Its registrars curate every list besides. */
export function curatorsQuery(registryId: string, list: string) {
  return queryOptions({
    queryKey: [...LAB_LISTS, "curators", registryId, list],
    queryFn: async ({ signal }) => {
      const found: RegistryRole[] = [];
      let cursor: string | undefined;
      do {
        const page = unwrap(
          await iki.GET("/api/v1/registries/{registry_id}/roles", {
            params: {
              path: { registry_id: registryId },
              query: { role: "list_curator", list, limit: 100, ...(cursor ? { cursor } : {}) },
            },
            signal,
          }),
        );
        found.push(...page.items);
        cursor = page.next_cursor ?? undefined;
      } while (cursor);
      return found;
    },
  });
}

/** Whether an option is offered: not archived, nor superseded. */
export function isActive(option: Pick<ListOption, "archived" | "superseded_by">): boolean {
  return !option.archived && !option.superseded_by;
}

/** What's become of an option no dropdown offers: superseded (by which), or archived. */
export function retiredOf(option: ListOption): "superseded" | "archived" | undefined {
  return option.superseded_by ? "superseded" : option.archived ? "archived" : undefined;
}

/** An option as people read it: its label, and what's become of it if it isn't offered
 * any more. "Dialysis (archived)", "Protien G (superseded)". */
export function optionText(
  label: string,
  retired: "superseded" | "archived" | undefined | null,
): string {
  return retired ? `${label} (${retired})` : label;
}

/** Whether a reference is to a list's option: options have codes, records don't. */
export function isOptionRef(ref: { code?: string | null } | undefined): boolean {
  return Boolean(ref?.code);
}

/** Options whose label or code has `text` in it, in any case, in the list's order. */
export function matching<T extends { label: string; code: string }>(
  options: readonly T[],
  text: string,
): T[] {
  const wanted = text.trim().toLocaleLowerCase();
  if (!wanted) return [...options];
  return options.filter(
    (o) =>
      o.label.toLocaleLowerCase().includes(wanted) || o.code.toLocaleLowerCase().includes(wanted),
  );
}

/** The list's own fields: its options' facts beside their code, label and description. */
export function factFields(type: EntityType): FieldDef[] {
  const builtIn = new Set<string>(OPTION_FIELDS);
  return type.fields.filter((f) => !builtIn.has(f.name) && !f.computed && !f.detached);
}

/** A type's page's address (ADR 0270): a list's options are a registry's, so it says
 * whose: the registry of a project, by its slug (a record's link to its option says the
 * record's), or a registry, by its ID. Neither, and it's the default registry's. */
export interface TypePageSearch {
  project?: string;
  registry?: string;
}

export function validateTypePageSearch(search: Record<string, unknown>): TypePageSearch {
  const text = (value: unknown) =>
    typeof value === "string" && value && value.length <= 100 ? value : undefined;
  const project = text(search["project"]);
  const registry = text(search["registry"]);
  return { ...(project ? { project } : {}), ...(registry ? { registry } : {}) };
}

/** A registry entry's page's address: a superseded option's names the project whose
 * records its notification asks to move (`?project=atlas`). */
export function validateEntrySearch(search: Record<string, unknown>): { project?: string } {
  const project = search["project"];
  return typeof project === "string" && project && project.length <= 100 ? { project } : {};
}

/** Where a list is read: its type's page, in the registry of the project a record is in. */
export function listSearch(project: string | undefined): TypePageSearch {
  return project ? { project } : {};
}

/**
 * How the options of `lists` read, by HRID: "Protein A affinity", "Dialysis (archived)".
 * For a filter's chip, whose value is an option's HRID, as the address holds it. In a
 * project's registry, or across projects, every registry's the reader sees.
 */
export function useOptionTexts(lists: readonly string[], projectId: string | undefined) {
  const registries = useQuery({ ...registriesQuery(), enabled: !projectId && lists.length > 0 });
  const scopes: ListScope[] = projectId
    ? [{ project: projectId }]
    : (registries.data ?? []).map((r) => ({ registry: r.id }));
  const found = useQueries({
    queries: lists.flatMap((name) => scopes.map((scope) => optionsQuery(name, scope, true))),
  });
  const texts = new Map<string, string>();
  for (const q of found) {
    for (const o of q.data ?? []) texts.set(o.hrid, optionText(o.label, retiredOf(o)));
  }
  return texts;
}
