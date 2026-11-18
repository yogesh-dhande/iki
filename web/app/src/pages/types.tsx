/**
 * The type browser: every entity type the deployment's plugins define, and
 * each type's fields, units and rules.
 */

import { useSuspenseQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";

import { typeQuery, typesQuery, type EntityType, type FieldDef } from "../entities";
import { usePageTitle } from "../layout";
import { Badge, PageHeader } from "../ui";

/** Where a field of a type with an identity is kept (ADR 0115). */
const KEPT: Record<"identity" | "derived" | "fact" | "record", { badge: string; help: string }> = {
  identity: { badge: "Identity", help: "Part of what it is, in its registry entry." },
  derived: {
    badge: "Registry: derived",
    help: "Worked out from its identity when it’s registered, in its registry entry.",
  },
  fact: {
    badge: "Registry: fact",
    help: "In its registry entry, the same for every project. Once set, only the registry’s registrars change it.",
  },
  record: { badge: "Project record", help: "Each project’s own." },
};

function keptAs(type: EntityType, field: FieldDef): keyof typeof KEPT | null {
  if (!type.identity.length) return null;
  if (field.registry) return field.registry;
  return type.identity.includes(field.name) ? "identity" : "record";
}

function TypeLink({ name, types }: { name: string; types: EntityType[] }) {
  const found = types.find((t) => t.name === name);
  return (
    <Link to="/types/$/" params={{ _splat: name }}>
      {found?.label ?? name}
    </Link>
  );
}

export function TypeList() {
  usePageTitle("Entity types");
  const types = useSuspenseQuery(typesQuery).data;
  const plugins = [...new Set(types.map((t) => t.plugin))].sort();
  return (
    <>
      <PageHeader title="Entity types" />
      <p className="muted">
        What this deployment’s plugins record. Each type’s fields, units and rules come from its
        plugin.
      </p>
      {types.length === 0 ? (
        <p className="empty">No plugins with entity types are installed.</p>
      ) : null}
      {plugins.map((plugin) => (
        <section key={plugin} aria-labelledby={`plugin-${plugin}`}>
          <h2 className="section-title" id={`plugin-${plugin}`}>
            <code>{plugin}</code>
          </h2>
          <ul className="rows">
            {types
              .filter((t) => t.plugin === plugin)
              .map((t) => (
                <li className="row" key={t.name}>
                  <div className="row-main">
                    <Link className="row-title" to="/types/$/" params={{ _splat: t.name }}>
                      {t.label}
                    </Link>
                    <span className="row-meta">
                      <code>{t.name}</code>
                      {t.extends ? (
                        <>
                          {" "}
                          · extends <TypeLink name={t.extends} types={types} />
                        </>
                      ) : null}
                      {t.description ? ` · ${t.description}` : ""}
                    </span>
                  </div>
                  <div className="row-tags">
                    {t.abstract ? <Badge muted>Abstract</Badge> : null}
                    {t.detached ? <Badge muted>Detached</Badge> : null}
                    {t.hrid_prefix ? <Badge>{t.hrid_prefix}</Badge> : null}
                  </div>
                </li>
              ))}
          </ul>
        </section>
      ))}
    </>
  );
}

/** What a field holds, in a few words: "Quantity in Å", "Reference to Protein", … */
export function fieldKind(field: FieldDef, types: EntityType[]): string {
  const label = (name: string) => types.find((t) => t.name === name)?.label ?? name;
  const kind = (type: string) => {
    switch (type) {
      case "string":
        return "Text";
      case "text":
        return "Long text";
      case "int":
        return "Whole number";
      case "float":
        return "Number";
      case "quantity":
        return `Quantity in ${field.unit_symbol ?? field.unit ?? ""}`;
      case "bool":
        return "Yes or no";
      case "enum":
        return `One of: ${field.values.join(", ")}`;
      case "state": {
        // A state field (ADR 0125): its states, where new records start, and which lock them.
        const states = field.states.map((st) => {
          const notes = [
            ...(st.name === field.initial ? ["new records start here"] : []),
            ...(st.locks ? ["locks it"] : []),
          ];
          return notes.length ? `${st.label} (${notes.join(", ")})` : st.label;
        });
        return `State, one of: ${states.join(", ")}`;
      }
      case "date":
        return "Date";
      case "datetime":
        return "Date and time";
      case "ref":
        return `Reference to ${
          !field.to.length || field.to.includes("iki/core.Entity")
            ? "any entity"
            : new Intl.ListFormat("en", { type: "disjunction" }).format(field.to.map(label))
        }`;
      case "file":
        return "File";
      case "json":
        return "Structured data (JSON)";
      default:
        return type;
    }
  };
  if (field.type === "list") return `List of: ${kind(field.items ?? "")}`;
  return kind(field.type);
}

export function TypeDetail() {
  const { _splat: name = "" } = useParams({ from: "/types/$" });
  const type = useSuspenseQuery(typeQuery(name)).data;
  const types = useSuspenseQuery(typesQuery).data;
  usePageTitle(type.label);
  const identity = type.fields.filter((f) => type.identity.includes(f.name));
  return (
    <>
      <PageHeader
        eyebrow={<Link to="/types/">Entity types</Link>}
        title={type.label}
        badge={
          <>
            {type.abstract ? <Badge muted>Abstract</Badge> : null}
            {type.detached ? <Badge muted>Detached</Badge> : null}
          </>
        }
      />
      {type.detached ? <DetachedNote plugin={type.plugin} what="type" /> : null}
      <section className="card">
        {type.description ? <p className="preserve-lines">{type.description}</p> : null}
        <dl className="summary">
          <dt>Name</dt>
          <dd>
            <code>{type.name}</code>
          </dd>
          {type.hrid_prefix ? (
            <>
              <dt>IDs</dt>
              <dd>
                <code>{type.hrid_prefix}-000001</code> and on
              </dd>
            </>
          ) : null}
          {type.extends ? (
            <>
              <dt>Extends</dt>
              <dd>
                <TypeLink name={type.extends} types={types} />
              </dd>
            </>
          ) : null}
          {type.subtypes.length ? (
            <>
              <dt>Subtypes</dt>
              <dd>
                {type.subtypes.map((sub, index) => (
                  <span key={sub}>
                    {index ? ", " : ""}
                    <TypeLink name={sub} types={types} />
                  </span>
                ))}
              </dd>
            </>
          ) : null}
          {identity.length ? (
            <>
              <dt>Identity</dt>
              <dd>
                Two are the same when their {identity.map((f) => f.label).join(" and ")} match. Each
                is registered once in a registry, with one ID, and each project that has it keeps
                its own record of it. Creating one that’s registered already gives the project its
                own record, with the same ID.
              </dd>
            </>
          ) : null}
        </dl>
        {type.abstract ? (
          <p className="muted">
            Abstract types have no entities of their own: entities are one of their subtypes.
          </p>
        ) : null}
      </section>
      <section aria-labelledby="fields-title">
        <h2 className="section-title" id="fields-title">
          Fields
        </h2>
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Field</th>
                <th scope="col">Holds</th>
                <th scope="col">Rules</th>
                {type.identity.length ? <th scope="col">Kept in</th> : null}
              </tr>
            </thead>
            <tbody>
              {type.fields.map((f) => {
                const kept = keptAs(type, f);
                return (
                  <tr key={f.name}>
                    <th scope="row">
                      {f.label}
                      <span className="cell-meta">
                        <code>{f.name}</code>
                        {f.plugin !== type.plugin ? ` · from ${f.plugin}` : ""}
                      </span>
                      {f.description ? <span className="cell-meta">{f.description}</span> : null}
                    </th>
                    <td>
                      {fieldKind(f, types)}
                      {f.display_unit && f.display_unit !== f.unit ? (
                        <span className="cell-meta">
                          Shown in {f.display_unit_symbol ?? f.display_unit}
                        </span>
                      ) : null}
                    </td>
                    <td>
                      <span className="tags">
                        {f.required ? <Badge>Required</Badge> : null}
                        {f.immutable ? <Badge muted>Set once</Badge> : null}
                        {f.computed ? <Badge muted>Computed</Badge> : null}
                        {f.unique ? <Badge muted>Unique</Badge> : null}
                      </span>
                    </td>
                    {kept ? (
                      <td>
                        <Badge muted={kept === "record"}>{KEPT[kept].badge}</Badge>
                        <span className="cell-meta">{KEPT[kept].help}</span>
                      </td>
                    ) : null}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
      {type.notifications.length ? (
        <TypeNotifications notifications={type.notifications} type={type} types={types} />
      ) : null}
    </>
  );
}

/** Whom its records tell, and when (ADR 0129): what its plugin declares, in words. */
function TypeNotifications({
  notifications,
  type,
  types,
}: {
  notifications: EntityType["notifications"];
  type: EntityType;
  types: EntityType[];
}) {
  return (
    <section aria-labelledby="notifications-title">
      <h2 className="section-title" id="notifications-title">
        Notifications
      </h2>
      <p className="muted">
        Who is told when its records change. Each person is told only of records they can read, and
        not of their own changes; emails say what happened and link here, without values. Choose how
        you’re told in <a href="/account/notifications/">your notification settings</a>.
      </p>
      <ul className="rows" aria-label="Its notifications">
        {notifications.map((n) => (
          <li className="row" key={n.kind}>
            <div className="row-main">
              <span className="row-title">{n.label}</span>
              {n.description ? <span className="row-meta">{n.description}</span> : null}
              <span className="row-meta">
                {n.when}
                {n.only_some ? ", for those its conditions find" : ""}. Tells{" "}
                {n.tells.map((who, index) => (
                  <span key={who}>
                    {index ? (index === n.tells.length - 1 ? " and " : ", ") : ""}
                    {who.charAt(0).toLowerCase() + who.slice(1)}
                  </span>
                ))}
                .
              </span>
              {n.declared_by !== type.name ? (
                <span className="row-meta">
                  From <TypeLink name={n.declared_by} types={types} />
                </span>
              ) : null}
            </div>
            <div className="row-tags">
              {n.required ? <Badge>Required</Badge> : null}
              <Badge muted>{n.email ? "In Iki and by email" : "In Iki"}</Badge>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** A detached plugin's data is kept, readable and frozen (ADR 0091). */
export function DetachedNote({ plugin, what }: { plugin: string; what: "type" | "entity" }) {
  return (
    <div className="alert alert-warning" role="note">
      <p>
        {what === "type" ? "This type" : "This entity"} is from the <code>{plugin}</code> plugin,
        which is detached. {what === "type" ? "Its entities’" : "Its"} values and history are kept
        and can be read, but can’t change
        {what === "type" ? ", and no new ones can be made" : ""}.
      </p>
    </div>
  );
}
