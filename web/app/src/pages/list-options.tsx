/**
 * A lab list's options (ADR 0270), on its type's page: what every project in a registry
 * picks from, in the list's order, with each option's code, label, description and other
 * facts, and whether a dropdown still offers it.
 *
 * Its registry's registrars and the list's curators keep it, as Benchling's Dropdowns and
 * LabWare's list tables are kept: they add options, change them, reorder the list,
 * archive and restore options, and supersede a wrong code with the right one, each with a
 * reason that every project sees in the option's history. They see how many records name
 * each option, in every project, never which. Everyone else reads it.
 */

import { ApiError, unwrap } from "@ikisci/sdk";
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useRouterState, useSearch } from "@tanstack/react-router";
import {
  memo,
  useEffect,
  useId,
  useMemo,
  useReducer,
  useRef,
  useState,
  type Dispatch,
  type ReactNode,
  type SubmitEvent,
} from "react";
import {
  Button as AriaButton,
  Dialog,
  Heading,
  Menu,
  MenuItem,
  MenuTrigger,
  Modal,
  ModalOverlay,
  Popover,
} from "react-aria-components";

import { iki, problemOf, useApiMutation } from "../api";
import {
  inSentence,
  personName,
  valueText,
  type EntityType,
  type FieldDef,
  type Ref,
} from "../entities";
import { FieldInput } from "../entity-inputs";
import { encode, isBlank, same, toDraft, type Draft } from "../entity-values";
import { required, useForm } from "../forms";
import {
  curatorsQuery,
  factFields,
  isActive,
  LAB_LISTS,
  labListQuery,
  listOfItems,
  LONG_LIST,
  matching,
  optionsQuery,
  optionText,
  retiredOf,
  type LabList,
  type ListOption,
} from "../lab-lists";
import { REGISTRY_SETTINGS, registriesQuery, type Registry } from "../registries";
import {
  Badge,
  Button,
  buttonClass,
  CheckboxField,
  Loading,
  ProblemAlert,
  SelectField,
  TextField,
  useMessages,
} from "../ui";

interface Scope {
  registry: string;
}

const NONE: ListOption[] = [];

/** The options a page of the list shows, or of its reordering: a list may have thousands,
 * and a page of them is read, or dragged about, at a time. */
const PAGE_ROWS = 100;

/** The list's options, in the registry the address names: a project's (a record's link
 * to its option), one by its ID, or else the default one, which the API lists first. */
export function ListOptions({ type, types }: { type: EntityType; types: EntityType[] }) {
  const search = useSearch({ from: "/types/$" });
  const navigate = useNavigate();
  const registries = useQuery(registriesQuery());
  const viaProject = useQuery({
    ...labListQuery(type.name, { project: search.project ?? "" }),
    enabled: Boolean(search.project) && !search.registry,
  });
  const registryId =
    search.registry ??
    (search.project ? viaProject.data?.registry.id : registries.data?.[0]?.id) ??
    undefined;
  const problem = viaProject.error ?? (registryId ? null : registries.error);
  const seen = registries.data ?? [];
  const pickerId = useId();
  return (
    <section aria-labelledby="options-title">
      <div className="section-header">
        <h2 className="section-title" id="options-title" tabIndex={-1}>
          Options
        </h2>
        {seen.length > 1 && registryId ? (
          <div className="inline-field">
            <label htmlFor={`${pickerId}-registry`}>Registry</label>
            <select
              id={`${pickerId}-registry`}
              value={registryId}
              onChange={(event) => {
                void navigate({
                  to: "/types/$/",
                  params: { _splat: type.name },
                  search: { registry: event.target.value },
                });
              }}
            >
              {seen.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
          </div>
        ) : null}
      </div>
      {problem ? (
        <ProblemAlert problem={problemOf(problem)} />
      ) : !registryId ? (
        <Loading label="Loading its options…" />
      ) : (
        <ListIn
          key={registryId}
          type={type}
          types={types}
          scope={{ registry: registryId }}
          registry={seen.find((r) => r.id === registryId)}
        />
      )}
    </section>
  );
}

/** The other lists' options a list's facts refer to (a method's category, say), by ID, as
 * references are given with a record: so its facts read as labels. */
function useFactRefs(type: EntityType, types: EntityType[], scope: Scope): Record<string, Ref> {
  const lists = [
    ...new Set(
      factFields(type)
        .map((f) => listOfItems(f, types)?.name)
        .filter((n): n is string => Boolean(n)),
    ),
  ];
  const found = useQueries({ queries: lists.map((name) => optionsQuery(name, scope, true)) });
  const refs: Record<string, Ref> = {};
  found.forEach((q, index) => {
    for (const o of q.data ?? []) {
      refs[o.id] = {
        hrid: o.hrid,
        title: o.label,
        type: lists[index] ?? "",
        archived: o.archived,
        code: o.code,
        superseded: Boolean(o.superseded_by),
        purged: false,
      };
    }
  });
  return refs;
}

type Doing =
  | { kind: "add" }
  | { kind: "edit"; option: ListOption }
  | { kind: "archive" | "restore"; option: ListOption }
  | { kind: "supersede"; option: ListOption };

function ListIn({
  type,
  types,
  scope,
  registry,
}: {
  type: EntityType;
  types: EntityType[];
  scope: Scope;
  registry: Registry | undefined;
}) {
  const list = useQuery(labListQuery(type.name, scope));
  const options = useQuery(optionsQuery(type.name, scope, true));
  const refs = useFactRefs(type, types, scope);
  const [showRetired, setShowRetired] = useState(false);
  const [doing, setDoing] = useState<Doing | null>(null);
  const [reordering, setReordering] = useState(false);
  const [find, setFind] = useState("");
  const [page, setPage] = useState(0);
  const hash = useRouterState({ select: (s) => s.location.hash });
  // A record's link to its option (#option-sec) shows it, once the options are here.
  const target = hash.startsWith("option-") ? hash.slice("option-".length) : "";
  const targetRetired = Boolean(
    target && options.data?.some((o) => o.code === target && !isActive(o)),
  );
  const all = options.data ?? NONE;
  const withRetired = showRetired || targetRetired;
  const shown = useMemo(() => (withRetired ? all : all.filter(isActive)), [all, withRetired]);
  const found = useMemo(() => matching(shown, find), [shown, find]);
  // A page of them at a time: a list may have thousands.
  const pages = Math.max(1, Math.ceil(found.length / PAGE_ROWS));
  const at = Math.min(page, pages - 1);
  const rows = useMemo(() => found.slice(at * PAGE_ROWS, (at + 1) * PAGE_ROWS), [found, at]);
  // The page with the option a record's link points at, once it's here.
  const [sought, setSought] = useState("");
  if (target && options.data && sought !== target) {
    setSought(target);
    const index = shown.findIndex((o) => o.code === target);
    if (index >= 0) setPage(Math.floor(index / PAGE_ROWS));
  }
  useEffect(() => {
    if (!target || !options.data) return;
    document.getElementById(`option-${target}`)?.scrollIntoView({ block: "center" });
  }, [target, options.data, targetRetired, showRetired, at]);

  // An option archived, restored or superseded may leave the table: focus goes to the row
  // that takes its place, or the one before it, as the dialog closes, not to the page.
  const returning = useRef<string[] | null>(null);
  useEffect(() => {
    const codes = returning.current;
    if (!codes || doing) return;
    returning.current = null;
    const button = codes
      .map((code) =>
        document
          .getElementById(`option-${code}`)
          ?.querySelector<HTMLElement>('button[aria-haspopup="menu"]'),
      )
      .find(Boolean);
    (button ?? document.getElementById("options-title"))?.focus();
  }, [doing, rows]);
  const changed = (option: ListOption) => {
    const index = found.findIndex((o) => o.code === option.code);
    returning.current = [option, found[index + 1], found[index - 1]].flatMap((o) =>
      o ? [o.code] : [],
    );
  };

  if (list.error) return <ProblemAlert problem={problemOf(list.error)} />;
  if (!list.data) return <Loading label="Loading its options…" />;
  const curate = list.data.can.curate;
  const retired = all.filter((o) => !isActive(o));
  const facts = factFields(type);
  const name = list.data.registry.name;
  return (
    <>
      <p className="muted">
        What records in the {name} registry’s projects pick from, in this order. An option that’s
        archived or superseded isn’t offered any more; the records that name it keep it.
        {curate
          ? " You curate this list: each change you make, with its reason, is in the option’s history for every project to see."
          : null}
      </p>
      {list.data.detached ? (
        <div className="alert alert-warning" role="note">
          <p>
            Its plugin, <code>{list.data.plugin}</code>, is detached: its options can be read, but
            not changed.
          </p>
        </div>
      ) : null}
      {curate && !reordering ? (
        <div className="actions">
          <Button
            variant="primary"
            onClick={() => {
              setDoing({ kind: "add" });
            }}
          >
            Add an option
          </Button>
          {all.length > 1 ? (
            <Button
              onClick={() => {
                setReordering(true);
              }}
            >
              Reorder
            </Button>
          ) : null}
        </div>
      ) : null}
      {options.error ? <ProblemAlert problem={problemOf(options.error)} /> : null}
      {options.isPending ? <Loading label="Loading its options…" /> : null}
      {reordering && options.data ? (
        <ReorderOptions
          list={list.data}
          options={all}
          scope={scope}
          onDone={() => {
            setReordering(false);
          }}
        />
      ) : null}
      {!reordering && options.data ? (
        <>
          {all.length > LONG_LIST ? (
            <OptionFinder
              value={find}
              found={found.length}
              onChange={(text) => {
                setFind(text);
                setPage(0);
              }}
            />
          ) : null}
          {retired.length ? (
            <CheckboxField
              label={`Show archived and superseded options (${String(retired.length)})`}
              checked={withRetired}
              onChange={(checked) => {
                setShowRetired(checked);
                setPage(0);
              }}
            />
          ) : null}
          {shown.length === 0 ? (
            <p className="empty">
              {all.length
                ? "Every option is archived or superseded: none is offered."
                : curate
                  ? "No options yet. Add the first, and every project in the registry can pick it."
                  : "No options yet. The list’s curators add them."}
            </p>
          ) : found.length === 0 ? (
            <p className="empty">No option’s label or code has “{find.trim()}” in it.</p>
          ) : (
            <>
              <OptionsTable
                list={list.data}
                type={type}
                facts={facts}
                options={rows}
                refs={refs}
                target={target}
                onDo={setDoing}
              />
              <Pager
                label="Pages of options"
                page={at}
                size={PAGE_ROWS}
                count={found.length}
                onPage={setPage}
              />
            </>
          )}
        </>
      ) : null}
      <Curators list={list.data} registry={registry} />
      {doing?.kind === "add" || doing?.kind === "edit" ? (
        <OptionDialog
          list={list.data}
          type={type}
          types={types}
          scope={scope}
          option={doing.kind === "edit" ? doing.option : undefined}
          refs={refs}
          onClose={() => {
            setDoing(null);
          }}
        />
      ) : null}
      {doing?.kind === "archive" || doing?.kind === "restore" ? (
        <ArchiveDialog
          list={list.data}
          option={doing.option}
          archive={doing.kind === "archive"}
          scope={scope}
          onChanged={changed}
          onClose={() => {
            setDoing(null);
          }}
        />
      ) : null}
      {doing?.kind === "supersede" ? (
        <SupersedeDialog
          list={list.data}
          option={doing.option}
          options={all}
          scope={scope}
          onChanged={changed}
          onClose={() => {
            setDoing(null);
          }}
        />
      ) : null}
    </>
  );
}

/** What's become of an option, in a word or two: its status. */
function Status({ option }: { option: ListOption }) {
  if (option.superseded_by) {
    return (
      <>
        <Badge muted>Superseded</Badge>
        <span className="cell-meta">By {option.superseded_by.label}</span>
      </>
    );
  }
  return option.archived ? <Badge muted>Archived</Badge> : <Badge>Active</Badge>;
}

function FactValue({
  field,
  value,
  refs,
}: {
  field: FieldDef;
  value: unknown;
  refs: Record<string, Ref>;
}) {
  if (value === null || value === undefined || value === "") {
    return <span className="muted">—</span>;
  }
  if (field.format === "uri" && typeof value === "string" && /^https?:\/\//i.test(value)) {
    return (
      <a href={value} target="_blank" rel="noreferrer noopener">
        {value}
      </a>
    );
  }
  return <>{valueText(field, value, { refs, files: {}, people: {} })}</>;
}

function OptionsTable({
  list,
  type,
  facts,
  options,
  refs,
  target,
  onDo,
}: {
  list: LabList;
  type: EntityType;
  facts: FieldDef[];
  /** The page of them shown. */
  options: ListOption[];
  refs: Record<string, Ref>;
  target: string;
  onDo: (doing: Doing) => void;
}) {
  const curate = list.can.curate;
  return (
    <div className="table-scroll">
      <table className="table list-options">
        <caption className="visually-hidden">
          {list.label_plural}, in the order dropdowns offer them
        </caption>
        <thead>
          <tr>
            <th scope="col" className="num">
              #
            </th>
            <th scope="col">Option</th>
            <th scope="col">Code</th>
            {facts.map((f) => (
              <th scope="col" key={f.name}>
                {f.label}
              </th>
            ))}
            <th scope="col">Status</th>
            {curate ? (
              <th scope="col" className="num">
                Records
              </th>
            ) : null}
            {curate && !list.detached ? (
              <th scope="col">
                <span className="visually-hidden">Changes</span>
              </th>
            ) : null}
          </tr>
        </thead>
        <tbody>
          {options.map((o) => (
            <tr
              key={o.id}
              id={`option-${o.code}`}
              className={[isActive(o) ? "" : "is-retired", target === o.code ? "is-target" : ""]
                .filter(Boolean)
                .join(" ")}
              aria-current={target === o.code ? "true" : undefined}
            >
              <td className="num">{o.position}</td>
              <th scope="row">
                <Link to="/entries/$ref/" params={{ ref: o.hrid }}>
                  {o.label}
                </Link>
                {o.description ? (
                  <span className="cell-meta preserve-lines">{o.description}</span>
                ) : null}
              </th>
              <td>
                <code>{o.code}</code>
              </td>
              {facts.map((f) => (
                <td key={f.name}>
                  <FactValue field={f} value={o.values[f.name]} refs={refs} />
                </td>
              ))}
              <td>
                <Status option={o} />
              </td>
              {curate ? <td className="num">{(o.uses ?? 0).toLocaleString()}</td> : null}
              {curate && !list.detached ? (
                <td>
                  <OptionMenu option={o} type={type} onDo={onDo} />
                </td>
              ) : null}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Finds an option in a long list, by its label or code, as its dropdowns do. */
function OptionFinder({
  value,
  found,
  onChange,
}: {
  value: string;
  /** How many options match it. */
  found: number;
  onChange: (text: string) => void;
}) {
  const id = useId();
  return (
    <div className="list-toolbar">
      <p className="visually-hidden" role="status">
        {value.trim()
          ? found
            ? `${found.toLocaleString()} ${found === 1 ? "option matches" : "options match"}.`
            : "No option matches."
          : ""}
      </p>
      <div className="list-search">
        <label htmlFor={id} className="visually-hidden">
          Find an option, by its label or code
        </label>
        <input
          id={id}
          type="search"
          autoComplete="off"
          spellCheck={false}
          maxLength={200}
          placeholder="Find an option by its label or code"
          value={value}
          onChange={(event) => {
            onChange(event.target.value);
          }}
        />
      </div>
    </div>
  );
}

/** A long list's pages: which of them shows, and the ones before and after it. Says
 * which options it shows, as it changes, for those who can't see it. */
function Pager({
  label,
  page,
  size,
  count,
  onPage,
}: {
  label: string;
  page: number;
  size: number;
  count: number;
  onPage: (page: number) => void;
}) {
  if (count <= size) return null;
  const last = Math.ceil(count / size) - 1;
  const from = page * size + 1;
  const to = Math.min(count, (page + 1) * size);
  return (
    <nav className="pager" aria-label={label}>
      <p className="pager-range" role="status">
        {from.toLocaleString()}–{to.toLocaleString()} of {count.toLocaleString()}
      </p>
      <div className="pager-moves">
        <Button
          variant="quiet"
          disabled={page === 0}
          onClick={() => {
            onPage(page - 1);
          }}
        >
          <span aria-hidden="true">← </span>
          Previous {size}
        </Button>
        <Button
          variant="quiet"
          disabled={page >= last}
          onClick={() => {
            onPage(page + 1);
          }}
        >
          Next {Math.min(size, count - to).toLocaleString()}
          <span aria-hidden="true"> →</span>
        </Button>
      </div>
    </nav>
  );
}

function OptionMenu({
  option,
  type,
  onDo,
}: {
  option: ListOption;
  type: EntityType;
  onDo: (doing: Doing) => void;
}) {
  const superseded = Boolean(option.superseded_by);
  return (
    <MenuTrigger>
      <AriaButton
        className={buttonClass("quiet")}
        aria-label={`Change ${option.label}`}
        aria-haspopup="menu"
      >
        Change
        <span aria-hidden="true"> ▾</span>
      </AriaButton>
      <Popover className="menu-popover" placement="bottom end" offset={4}>
        <Menu
          className="menu"
          aria-label={`Change ${option.label}`}
          onAction={(key) => {
            if (key === "edit") onDo({ kind: "edit", option });
            if (key === "archive") onDo({ kind: "archive", option });
            if (key === "restore") onDo({ kind: "restore", option });
            if (key === "supersede") onDo({ kind: "supersede", option });
          }}
        >
          <MenuItem id="edit" className="menu-item">
            {factFields(type).length ? "Edit…" : "Edit label or description…"}
          </MenuItem>
          {superseded ? null : option.archived ? (
            <MenuItem id="restore" className="menu-item">
              Restore…
            </MenuItem>
          ) : (
            <MenuItem id="archive" className="menu-item">
              Archive…
            </MenuItem>
          )}
          {superseded ? null : (
            <MenuItem id="supersede" className="menu-item">
              Supersede with another…
            </MenuItem>
          )}
        </Menu>
      </Popover>
    </MenuTrigger>
  );
}

/** Who curates the list in the registry: its curators, and its registrars, who curate
 * every list. Org Admins appoint curators in the registry's settings. */
function Curators({ list, registry }: { list: LabList; registry: Registry | undefined }) {
  const curators = useQuery(curatorsQuery(list.registry.id));
  const mine = (curators.data ?? []).filter((c) => c.list === list.type);
  return (
    <section className="card list-curators" aria-labelledby="curators-title">
      <h3 className="section-title" id="curators-title">
        Who curates it
      </h3>
      {curators.error ? <ProblemAlert problem={problemOf(curators.error)} /> : null}
      {curators.data ? (
        <p>
          {mine.length ? (
            <>
              {new Intl.ListFormat("en", { type: "conjunction" }).format(
                mine.map((c) => personName(c.user)),
              )}
              , and the {list.registry.name} registry’s registrars.
            </>
          ) : (
            <>The {list.registry.name} registry’s registrars. It has no curators of its own.</>
          )}{" "}
          Ask them to add an option, or to change one.
        </p>
      ) : curators.isPending ? (
        <Loading label="Loading who curates it…" />
      ) : null}
      {registry?.can.manage ? (
        <p>
          <a href={`${REGISTRY_SETTINGS}${registry.id}/#list-curators-title`}>
            Appoint or remove its curators
          </a>{" "}
          in the registry’s settings.
        </p>
      ) : null}
    </section>
  );
}

// --- Changing it ----------------------------------------------------------------------------

/** A list's change was refused because the option changed since it was read (412). */
function isStale(error: unknown): boolean {
  return error instanceof ApiError && error.problem.code === "stale";
}

function Shell({
  title,
  busy,
  onClose,
  children,
}: {
  title: string;
  busy: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <ModalOverlay
      className="dialog-overlay"
      isOpen
      isDismissable={!busy}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Modal className="dialog">
        <Dialog className="dialog-body">
          <Heading slot="title" className="section-title">
            {title}
          </Heading>
          {children}
        </Dialog>
      </Modal>
    </ModalOverlay>
  );
}

/** The reason a change asks for: every project in the registry sees it in its history. */
function ReasonField({
  value,
  onChange,
  error,
  optional = false,
  help,
}: {
  value: string;
  onChange: (value: string) => void;
  error: string | undefined;
  optional?: boolean;
  help?: string;
}) {
  return (
    <TextField
      label="Reason"
      value={value}
      onChange={onChange}
      error={error}
      maxLength={2000}
      multiline
      optional={optional}
      help={help ?? "Why, for the record. Every project in the registry sees it in its history."}
    />
  );
}

/** The fields a dialog asks for, in order: the code (once, when it's added), the label,
 * the description, and the list's own fields. */
function editable(type: EntityType, adding: boolean): FieldDef[] {
  const named = (n: string) => type.fields.filter((f) => f.name === n);
  return [
    ...(adding ? named("code") : []),
    ...named("label"),
    ...named("description"),
    ...factFields(type).filter((f) => adding || !f.immutable),
  ];
}

/** Adds an option at the end of the list, or changes one's label, description and other
 * facts, from the revision it was read at (If-Match). A change says why. */
function OptionDialog({
  list,
  type,
  types,
  scope,
  option: given,
  refs,
  onClose,
}: {
  list: LabList;
  type: EntityType;
  types: EntityType[];
  scope: Scope;
  option: ListOption | undefined;
  refs: Record<string, Ref>;
  onClose: () => void;
}) {
  const client = useQueryClient();
  const { say } = useMessages();
  const adding = given === undefined;
  const fields = editable(type, adding);
  // The option as it was read: brought up to date when a save finds it changed since.
  const [option, setOption] = useState(given);
  const valuesOf = (o: ListOption | undefined): Record<string, unknown> =>
    o ? { ...o.values, code: o.code, label: o.label, description: o.description || null } : {};
  const start = (o: ListOption | undefined) =>
    Object.fromEntries(
      fields.map((f) => [
        f.name,
        toDraft(f, valuesOf(o)[f.name] ?? null, { refs, files: {}, people: {} }),
      ]),
    );
  const [base, setBase] = useState<Record<string, Draft>>(() => start(option));
  const [drafts, setDrafts] = useState<Record<string, Draft>>(() => start(option));
  const [reason, setReason] = useState("");
  const [unchanged, setUnchanged] = useState(false);
  const [reloading, setReloading] = useState(false);
  const save = useApiMutation(
    async (values: Record<string, unknown>): Promise<ListOption> => {
      if (!option) {
        const { code, label, description, ...facts } = values;
        return unwrap(
          await iki.POST("/api/v1/lists/{name}/options", {
            params: { path: { name: type.name }, query: scope },
            body: {
              code: typeof code === "string" ? code : "",
              label: typeof label === "string" ? label : "",
              description: typeof description === "string" ? description : "",
              values: facts,
              reason: reason.trim(),
            },
          }),
        );
      }
      const { label, description, ...facts } = values;
      return unwrap(
        await iki.PATCH("/api/v1/lists/{name}/options/{code}", {
          params: {
            path: { name: type.name, code: option.code },
            query: scope,
            header: { "If-Match": `"${String(option.revision)}"` },
          },
          body: {
            ...(label !== undefined ? { label: typeof label === "string" ? label : "" } : {}),
            ...(description !== undefined
              ? { description: typeof description === "string" ? description : "" }
              : {}),
            values: facts,
            reason: reason.trim(),
          },
        }),
      );
    },
    [LAB_LISTS],
  );
  const stale = isStale(save.error);
  const form = useForm(stale ? null : save.error, save.isPending);
  const reasonError = form.errors["reason"] ?? form.errors["values/reason"];

  const reload = async () => {
    setReloading(true);
    try {
      const fresh = await client.query({
        ...optionsQuery(type.name, scope, true),
        staleTime: 0,
      });
      const now = fresh.find((o) => o.code === option?.code);
      setOption(now);
      const drafted = start(now);
      setBase(drafted);
      setDrafts(drafted);
      save.reset();
    } finally {
      setReloading(false);
    }
  };

  return (
    <Shell
      title={adding ? `Add an option to ${inSentence(list.label_plural)}` : `Edit ${given.label}`}
      busy={save.isPending}
      onClose={onClose}
    >
      <form
        {...form.props}
        onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
          event.preventDefault();
          setUnchanged(false);
          const values: Record<string, unknown> = {};
          const checks: Record<string, string | false> = adding
            ? {}
            : { reason: required(reason, "Say why: every project in the registry sees it.") };
          for (const f of fields) {
            const draft = drafts[f.name] ?? null;
            if (!adding && same(draft, base[f.name])) continue;
            if (isBlank(f, draft)) {
              if (f.required) checks[`values/${f.name}`] = `Enter its ${inSentence(f.label)}.`;
              else if (!adding) values[f.name] = f.name === "description" ? "" : null;
              continue;
            }
            const encoded = encode(f, draft, type);
            if (encoded.ok) values[f.name] = encoded.value;
            else checks[`values/${f.name}`] = encoded.error;
          }
          if (!form.validate(checks)) return;
          if (!adding && !Object.keys(values).length) {
            setUnchanged(true);
            return;
          }
          save.mutate(values, {
            onSuccess: (saved) => {
              onClose();
              say(
                adding
                  ? `Added ${saved.label}. Every project in the registry can pick it.`
                  : `Saved ${saved.label}. Records that name it show it as it’s called now.`,
              );
            },
          });
        }}
      >
        {stale ? (
          <div className="alert alert-warning" role="alert">
            <p>
              Someone changed {given?.label ?? "it"} since you opened it, so nothing was saved. Load
              the latest to see what it says now, then make your change again.
            </p>
            <div className="actions">
              <Button
                busy={reloading}
                busyText="Loading…"
                onClick={() => {
                  void reload();
                }}
              >
                Load the latest
              </Button>
            </div>
          </div>
        ) : unchanged ? (
          <div className="alert alert-warning" role="alert">
            <p>Nothing to save: you haven’t changed anything.</p>
          </div>
        ) : (
          form.alert
        )}
        {!adding && option ? (
          <p className="muted">
            Its code is <code>{option.code}</code>, which never changes. If it’s wrong, add an
            option with the right one and supersede this one with it.
          </p>
        ) : null}
        {fields.map((f) => (
          <FieldInput
            key={f.name}
            field={f}
            type={type}
            types={types}
            draft={drafts[f.name] ?? null}
            onChange={(draft) => {
              setDrafts((current) => ({ ...current, [f.name]: draft }));
            }}
            error={form.errors[`values/${f.name}`]}
            projectId=""
            registryId={scope.registry}
          />
        ))}
        <ReasonField value={reason} onChange={setReason} error={reasonError} optional={adding} />
        <div className="actions">
          <Button
            type="submit"
            variant="primary"
            busy={save.isPending}
            busyText="Saving…"
            disabled={stale}
          >
            {adding ? "Add option" : "Save"}
          </Button>
          <Button variant="quiet" onClick={onClose} disabled={save.isPending}>
            Cancel
          </Button>
        </div>
      </form>
    </Shell>
  );
}

/** Archives an option, so no dropdown offers it, or restores one; with a reason. */
function ArchiveDialog({
  list,
  option,
  archive,
  scope,
  onChanged,
  onClose,
}: {
  list: LabList;
  option: ListOption;
  archive: boolean;
  scope: Scope;
  /** Done: before it closes, so focus can go where its row was. */
  onChanged: (option: ListOption) => void;
  onClose: () => void;
}) {
  const { say } = useMessages();
  const [reason, setReason] = useState("");
  const run = useApiMutation(
    async () =>
      unwrap(
        archive
          ? await iki.POST("/api/v1/lists/{name}/options/{code}/archive", {
              params: { path: { name: list.type, code: option.code }, query: scope },
              body: { reason: reason.trim() },
            })
          : await iki.POST("/api/v1/lists/{name}/options/{code}/restore", {
              params: { path: { name: list.type, code: option.code }, query: scope },
              body: { reason: reason.trim() },
            }),
      ),
    [LAB_LISTS],
  );
  const form = useForm(run.error, run.isPending);
  const uses = option.uses ?? 0;
  return (
    <Shell
      title={archive ? `Archive ${option.label}?` : `Restore ${option.label}?`}
      busy={run.isPending}
      onClose={onClose}
    >
      <form
        {...form.props}
        onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
          event.preventDefault();
          if (!form.validate({ reason: required(reason, "Say why: every project sees it.") })) {
            return;
          }
          run.mutate(undefined, {
            onSuccess: () => {
              onChanged(option);
              onClose();
              say(
                archive
                  ? `Archived ${option.label}. Dropdowns don’t offer it; records that name it keep it.`
                  : `Restored ${option.label}. Dropdowns offer it again.`,
              );
            },
          });
        }}
      >
        {form.alert}
        {archive ? (
          <p>
            No dropdown will offer it, in any project in the {list.registry.name} registry.{" "}
            {uses
              ? `The ${uses.toLocaleString()} ${uses === 1 ? "record that names" : "records that name"} it keep it, marked archived, and it’s still in their search, filters and downloads.`
              : "No record names it yet."}{" "}
            You can restore it later.
          </p>
        ) : (
          <p>Dropdowns offer it again, in its place in the list.</p>
        )}
        <ReasonField value={reason} onChange={setReason} error={form.errors["reason"]} />
        <div className="actions">
          <Button
            type="submit"
            variant={archive ? "danger" : "primary"}
            busy={run.isPending}
            busyText={archive ? "Archiving…" : "Restoring…"}
          >
            {archive ? "Archive" : "Restore"}
          </Button>
          <Button variant="quiet" onClick={onClose} disabled={run.isPending}>
            Cancel
          </Button>
        </div>
      </form>
    </Shell>
  );
}

/** Supersedes a wrong option, for good, with another of the list: dropdowns offer that
 * one instead, and each project whose records name it is told to move them. */
function SupersedeDialog({
  list,
  option,
  options,
  scope,
  onChanged,
  onClose,
}: {
  list: LabList;
  option: ListOption;
  options: ListOption[];
  scope: Scope;
  onChanged: (option: ListOption) => void;
  onClose: () => void;
}) {
  const { say } = useMessages();
  const others = options.filter((o) => o.code !== option.code && isActive(o));
  const [replacement, setReplacement] = useState("");
  const [reason, setReason] = useState("");
  const run = useApiMutation(
    async () =>
      unwrap(
        await iki.POST("/api/v1/lists/{name}/options/{code}/supersede", {
          params: { path: { name: list.type, code: option.code }, query: scope },
          body: { replacement, reason: reason.trim() },
        }),
      ),
    [LAB_LISTS],
  );
  const form = useForm(run.error, run.isPending);
  const chosen = others.find((o) => o.code === replacement);
  const uses = option.uses ?? 0;
  return (
    <Shell title={`Supersede ${option.label}?`} busy={run.isPending} onClose={onClose}>
      <form
        {...form.props}
        onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
          event.preventDefault();
          if (
            !form.validate({
              replacement: !replacement && "Choose the option that replaces it.",
              reason: required(reason, "Say why: each project that names it is told."),
            })
          ) {
            return;
          }
          run.mutate(undefined, {
            onSuccess: (saved) => {
              onChanged(option);
              onClose();
              say(
                `Superseded ${saved.label} with ${saved.superseded_by?.label ?? replacement}. Each project whose records name it has been told.`,
              );
            },
          });
        }}
      >
        {form.alert}
        <p>
          For an option added by mistake, such as one with the wrong code: it’s replaced for good,
          and dropdowns offer its replacement instead.{" "}
          {uses
            ? `The Admins of each project whose records name it (${uses.toLocaleString()} ${uses === 1 ? "record" : "records"}) are told, and its Editors move them to the replacement.`
            : "No record names it yet."}
        </p>
        {others.length ? (
          <SelectField
            label="Replaced by"
            value={replacement}
            onChange={setReplacement}
            error={form.errors["replacement"]}
            options={[
              { value: "", label: "Choose one" },
              ...others.map((o) => ({ value: o.code, label: `${o.label} (${o.code})` })),
            ]}
          />
        ) : (
          <p className="alert alert-warning" role="note">
            There’s no other active option to replace it with: add the right one first.
          </p>
        )}
        <ReasonField value={reason} onChange={setReason} error={form.errors["reason"]} />
        <p>
          <strong>This can’t be undone.</strong>{" "}
          {chosen
            ? `${option.label} will be marked as replaced by ${chosen.label}, and its code can’t be added again.`
            : "Its code can’t be added again."}
        </p>
        <div className="actions">
          <Button
            type="submit"
            variant="danger"
            busy={run.isPending}
            busyText="Superseding…"
            disabled={!others.length}
          >
            Supersede
          </Button>
          <Button variant="quiet" onClick={onClose} disabled={run.isPending}>
            Cancel
          </Button>
        </div>
      </form>
    </Shell>
  );
}

/** Moves the code at `from` to `to` in `order`. */
export function movedTo(order: readonly string[], from: number, to: number): string[] {
  const next = [...order];
  const [code] = next.splice(from, 1);
  if (code === undefined) return next;
  next.splice(Math.max(0, Math.min(next.length, to)), 0, code);
  return next;
}

/** A reordering: the new order, the option last moved, the page shown, and what's being
 * dragged and over what. One reducer, so its rows' handlers never change, and a drag that
 * passes over a row redraws that row alone. */
interface Reordering {
  order: string[];
  moved: string | null;
  page: number;
  dragging: string | null;
  over: string | null;
}

type ReorderStep =
  | { kind: "move"; code: string; to: number }
  | { kind: "start"; order: string[] }
  | { kind: "page"; page: number }
  | { kind: "drag"; code: string }
  | { kind: "over"; code: string }
  | { kind: "leave"; code: string }
  | { kind: "drop"; code: string; from: string }
  | { kind: "end" };

function moving(state: Reordering, code: string, to: number): Reordering {
  const from = state.order.indexOf(code);
  if (from < 0 || to === from) return state;
  const order = movedTo(state.order, from, to);
  // The page follows the option, wherever it went.
  return { ...state, order, moved: code, page: Math.floor(order.indexOf(code) / PAGE_ROWS) };
}

function reorder(state: Reordering, step: ReorderStep): Reordering {
  switch (step.kind) {
    case "move":
      return moving(state, step.code, step.to);
    case "start":
      return { order: step.order, moved: null, page: 0, dragging: null, over: null };
    case "page":
      return { ...state, page: step.page };
    case "drag":
      return { ...state, dragging: step.code };
    case "over":
      return state.over === step.code ? state : { ...state, over: step.code };
    case "leave":
      return state.over === step.code ? { ...state, over: null } : state;
    case "drop": {
      const from = state.dragging ?? step.from;
      const done = { ...state, dragging: null, over: null };
      return from && from !== step.code ? moving(done, from, state.order.indexOf(step.code)) : done;
    }
    case "end":
      return { ...state, dragging: null, over: null };
  }
}

/**
 * The list's order, changed: dragged, moved up and down a place at a time by its buttons,
 * or to a position, which work by keyboard and say where each option went. Every option is
 * in it, archived and superseded ones too, as the API takes the whole order; it's saved
 * once, with a reason, as one change. A long list shows a page of it at a time.
 */
function ReorderOptions({
  list,
  options,
  scope,
  onDone,
}: {
  list: LabList;
  options: ListOption[];
  scope: Scope;
  onDone: () => void;
}) {
  const { say } = useMessages();
  const client = useQueryClient();
  const [state, dispatch] = useReducer(reorder, options, (given) => ({
    order: given.map((o) => o.code),
    moved: null,
    page: 0,
    dragging: null,
    over: null,
  }));
  const { order, page } = state;
  const [reason, setReason] = useState("");
  const [placing, setPlacing] = useState<string | null>(null);
  const focusing = useRef<{ code: string; direction: Direction } | null>(null);
  const listId = useId();
  const byCode = useMemo(() => new Map(options.map((o) => [o.code, o])), [options]);
  const save = useApiMutation(
    async () =>
      unwrap(
        await iki.PUT("/api/v1/lists/{name}/order", {
          params: { path: { name: list.type }, query: scope },
          body: { codes: order, reason: reason.trim() },
        }),
      ),
    [LAB_LISTS],
  );
  const form = useForm(save.error, save.isPending);
  const changed = order.some((code, i) => code !== options[i]?.code);
  const [reloading, setReloading] = useState(false);
  // Someone added or removed an option meanwhile: the order must name every one.
  const reload = async () => {
    setReloading(true);
    try {
      const fresh = await client.query({
        ...optionsQuery(list.type, scope, true),
        staleTime: 0,
      });
      dispatch({ kind: "start", order: fresh.map((o) => o.code) });
      save.reset();
    } finally {
      setReloading(false);
    }
  };

  // After a move, focus stays on the moved option's button, or its other one once it's at
  // the end it moved to.
  useEffect(() => {
    const wanted = focusing.current;
    if (!wanted) return;
    focusing.current = null;
    const row = document.getElementById(`${listId}-${wanted.code}`);
    const buttons = row?.querySelectorAll<HTMLButtonElement>("button[data-move]");
    const same = [...(buttons ?? [])].find((b) => b.dataset["move"] === wanted.direction);
    const target = same && !same.disabled ? same : [...(buttons ?? [])].find((b) => !b.disabled);
    target?.focus();
  }, [order, listId, placing]);

  // Stable, as the rows are memoized: a drag over one redraws it alone.
  const move = useMemo(
    () => (code: string, to: number, direction: Direction) => {
      focusing.current = { code, direction };
      dispatch({ kind: "move", code, to });
    },
    [],
  );

  const moved = state.moved
    ? `Moved ${byCode.get(state.moved)?.label ?? state.moved} to position ${String(
        order.indexOf(state.moved) + 1,
      )} of ${String(order.length)}.`
    : "";
  const first = page * PAGE_ROWS;
  const placed = placing ? byCode.get(placing) : undefined;

  return (
    <form
      {...form.props}
      className="card list-reorder"
      aria-labelledby={`${listId}-title`}
      onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
        event.preventDefault();
        if (
          !form.validate({
            reason: required(reason, "Say why: every project’s dropdowns follow it."),
          })
        ) {
          return;
        }
        save.mutate(undefined, {
          onSuccess: (saved) => {
            client.setQueryData(optionsQuery(list.type, scope, true).queryKey, saved);
            onDone();
            say(
              `Saved the order of ${inSentence(list.label_plural)}. Every project’s dropdowns follow it.`,
            );
          },
        });
      }}
    >
      <h3 className="section-title" id={`${listId}-title`}>
        Reorder {inSentence(list.label_plural)}
      </h3>
      <p className="muted">
        Drag an option to its place, or move it with its buttons: up or down a place, or to a
        position. Dropdowns offer them in this order, in every project in the {list.registry.name}{" "}
        registry.
      </p>
      {form.alert}
      {save.error ? (
        <div className="actions">
          <Button
            busy={reloading}
            busyText="Loading…"
            onClick={() => {
              void reload();
            }}
          >
            Start again from the latest order
          </Button>
        </div>
      ) : null}
      <ol
        className="reorder-list"
        aria-label={`${list.label_plural}, in their new order`}
        start={first + 1}
      >
        {order.slice(first, first + PAGE_ROWS).map((code, offset) => {
          const o = byCode.get(code);
          const index = first + offset;
          return (
            <ReorderItem
              key={code}
              id={`${listId}-${code}`}
              code={code}
              label={o?.label ?? code}
              retired={o ? retiredOf(o) : undefined}
              index={index}
              last={index === order.length - 1}
              dragging={state.dragging === code}
              over={state.over === code && state.dragging !== code}
              busy={save.isPending}
              dispatch={dispatch}
              onMove={move}
              onPlace={setPlacing}
            />
          );
        })}
      </ol>
      <Pager
        label="Pages of the order"
        page={page}
        size={PAGE_ROWS}
        count={order.length}
        onPage={(next) => {
          dispatch({ kind: "page", page: next });
        }}
      />
      <p className="visually-hidden" role="status" aria-live="polite">
        {moved}
      </p>
      <ReasonField
        value={reason}
        onChange={setReason}
        error={form.errors["reason"]}
        help="Why, for the record. Every option that moved has it in its history."
      />
      <div className="actions">
        <Button
          type="submit"
          variant="primary"
          busy={save.isPending}
          busyText="Saving…"
          disabled={!changed}
        >
          Save order
        </Button>
        <Button
          variant="quiet"
          onClick={() => {
            onDone();
          }}
          disabled={save.isPending}
        >
          Cancel
        </Button>
      </div>
      {placing ? (
        <PlaceDialog
          label={placed?.label ?? placing}
          position={order.indexOf(placing) + 1}
          count={order.length}
          onPlace={(position) => {
            move(placing, position - 1, "to");
            setPlacing(null);
          }}
          onClose={() => {
            setPlacing(null);
          }}
        />
      ) : null}
    </form>
  );
}

type Direction = "up" | "down" | "to";

/** What a dragged option carries: its code, as nothing else dragged does. */
const DRAGGED = "application/x-iki-list-option";

/** An option in the reordering: dragged by a pointer; by keyboard, its buttons move it,
 * and say where. Memoized: a drag over another row doesn't redraw it. */
const ReorderItem = memo(function ReorderItem({
  id,
  code,
  label,
  retired,
  index,
  last,
  dragging,
  over,
  busy,
  dispatch,
  onMove,
  onPlace,
}: {
  id: string;
  code: string;
  label: string;
  retired: "superseded" | "archived" | undefined;
  index: number;
  last: boolean;
  dragging: boolean;
  over: boolean;
  busy: boolean;
  dispatch: Dispatch<ReorderStep>;
  onMove: (code: string, to: number, direction: Direction) => void;
  onPlace: (code: string) => void;
}) {
  return (
    // Dragging is for a pointer; the buttons move it by keyboard, and say where.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <li
      id={id}
      className={["reorder-item", dragging ? "is-dragging" : "", over ? "is-over" : ""]
        .filter(Boolean)
        .join(" ")}
      draggable
      onDragStart={(event) => {
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData(DRAGGED, code);
        dispatch({ kind: "drag", code });
      }}
      onDragOver={(event) => {
        // Only an option of the order drops here: not a file, nor text from elsewhere.
        if (!event.dataTransfer.types.includes(DRAGGED)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        dispatch({ kind: "over", code });
      }}
      onDragLeave={() => {
        dispatch({ kind: "leave", code });
      }}
      onDrop={(event) => {
        event.preventDefault();
        dispatch({ kind: "drop", code, from: event.dataTransfer.getData(DRAGGED) });
      }}
      onDragEnd={() => {
        dispatch({ kind: "end" });
      }}
    >
      <span className="reorder-handle" aria-hidden="true">
        ⠿
      </span>
      <span className="reorder-position">{index + 1}.</span>
      <span className="reorder-label">
        {optionText(label, retired)} <code className="muted">{code}</code>
      </span>
      <span className="reorder-moves">
        <button
          type="button"
          className={buttonClass("quiet")}
          data-move="up"
          disabled={index === 0 || busy}
          aria-label={`Move ${label} up`}
          onClick={() => {
            onMove(code, index - 1, "up");
          }}
        >
          ↑ Up
        </button>
        <button
          type="button"
          className={buttonClass("quiet")}
          data-move="down"
          disabled={last || busy}
          aria-label={`Move ${label} down`}
          onClick={() => {
            onMove(code, index + 1, "down");
          }}
        >
          ↓ Down
        </button>
        <button
          type="button"
          className={buttonClass("quiet")}
          data-move="to"
          disabled={(index === 0 && last) || busy}
          // Its name says what it moves, and keeps the words it shows (WCAG 2.5.3).
          aria-label={`Move ${label} to a position`}
          onClick={() => {
            onPlace(code);
          }}
        >
          To a position…
        </button>
      </span>
    </li>
  );
});

/** Asks where an option goes in the order: a position, from 1 at the top. */
function PlaceDialog({
  label,
  position,
  count,
  onPlace,
  onClose,
}: {
  label: string;
  position: number;
  count: number;
  onPlace: (position: number) => void;
  onClose: () => void;
}) {
  const [text, setText] = useState(String(position));
  const form = useForm(undefined);
  const range = `a whole number from 1, the top, to ${count.toLocaleString()}, the bottom`;
  return (
    <Shell title={`Move ${label}`} busy={false} onClose={onClose}>
      <form
        {...form.props}
        onSubmit={(event: SubmitEvent<HTMLFormElement>) => {
          event.preventDefault();
          // Its own form, in a dialog over the order's: the order's isn't sent.
          event.stopPropagation();
          const wanted = Number(text.trim());
          const valid = /^\d+$/.test(text.trim()) && wanted >= 1 && wanted <= count;
          if (!form.validate({ position: !valid && `Enter ${range}.` })) return;
          onPlace(wanted);
        }}
      >
        <p>
          It’s at position {position.toLocaleString()} of {count.toLocaleString()} now. The options
          from there to its new position move a place to make room.
        </p>
        <TextField
          label="New position"
          value={text}
          onChange={setText}
          error={form.errors["position"]}
          help={`Enter ${range}.`}
          maxLength={6}
          // The dialog was just asked for: its one field takes focus.
          // eslint-disable-next-line jsx-a11y/no-autofocus
          autoFocus
        />
        <div className="actions">
          <Button type="submit" variant="primary">
            Move
          </Button>
          <Button variant="quiet" onClick={onClose}>
            Cancel
          </Button>
        </div>
      </form>
    </Shell>
  );
}
