/**
 * The type browser: every entity type the deployment's plugins define, and
 * each type's fields, units and rules.
 */

import { useSuspenseQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";

import { typeQuery, typesQuery, type EntityType, type FieldDef } from "../entities";
import { usePageTitle } from "../layout";
import { Badge, PageHeader } from "../ui";

const DUPLICATES: Record<string, string> = {
  reuse: "Creating one that’s already there links the existing one instead.",
  warn: "Creating one that’s already there asks first.",
  allow: "Duplicates are allowed.",
};

function TypeLink({ name, types }: { name: string; types: EntityType[] }) {
  const found = types.find((t) => t.name === name);
  return (
    <Link to="/types/$name/" params={{ name }}>
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
                    <Link className="row-title" to="/types/$name/" params={{ name: t.name }}>
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
      case "date":
        return "Date";
      case "datetime":
        return "Date and time";
      case "ref":
        return `Reference to ${field.to === "core.Entity" ? "any entity" : label(field.to ?? "")}`;
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
  const { name } = useParams({ from: "/types/$name" });
  const type = useSuspenseQuery(typeQuery(name)).data;
  const types = useSuspenseQuery(typesQuery).data;
  usePageTitle(type.label);
  const identity = type.fields.filter((f) => type.identity.includes(f.name));
  return (
    <>
      <PageHeader
        eyebrow={<Link to="/types/">Entity types</Link>}
        title={type.label}
        badge={type.abstract ? <Badge muted>Abstract</Badge> : null}
      />
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
                Two are the same when their {identity.map((f) => f.label).join(" and ")} match.{" "}
                {type.on_duplicate ? DUPLICATES[type.on_duplicate] : null}
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
              </tr>
            </thead>
            <tbody>
              {type.fields.map((f) => (
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
                      {type.identity.includes(f.name) ? <Badge muted>Identity</Badge> : null}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
