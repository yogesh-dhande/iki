/** Stock and outstanding reorder lines for one project (ADR 0190). The server
 * evaluates expiry, custody and draw guards using the current ledger balances. */
import { unwrap } from "@ikisci/sdk";
import {
  useInfiniteQuery,
  useQuery,
  type InfiniteData,
  type UseInfiniteQueryResult,
} from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState, type ReactNode } from "react";
import { Button as AriaButton } from "react-aria-components";

import { iki, problemOf, type Project } from "../api";
import { ENTITIES } from "../entities";
import { usePageTitle } from "../layout";
import { actionsQuery } from "../runs";
import { Badge, Button, CheckboxField, Loading, ProblemAlert, buttonClass } from "../ui";
import { useProject } from "./project-overview";

const ORDER = "iki/reagents.place_order";
const RECEIVE = "iki/reagents.receive_delivery";
const RECEIVE_STOCK = "iki/reagents.receive_stock";
const RUN_LABELS = {
  [ORDER]: "Place order",
  [RECEIVE]: "Receive delivery",
  [RECEIVE_STOCK]: "Receive without an order",
} as const;
const pageParams = (cursor: string | undefined) => ({ limit: 50, ...(cursor ? { cursor } : {}) });

function stockPage(
  project: string,
  lowOnly: boolean,
  cursor: string | undefined,
  signal: AbortSignal,
) {
  return iki
    .GET("/api/v1/projects/{project_id}/stock", {
      params: {
        path: { project_id: project },
        query: { ...pageParams(cursor), low_only: lowOnly },
      },
      signal,
    })
    .then(unwrap);
}

function lotsPage(project: string, item: string, cursor: string | undefined, signal: AbortSignal) {
  return iki
    .GET("/api/v1/projects/{project_id}/stock/{item_id}/lots", {
      params: { path: { project_id: project, item_id: item }, query: pageParams(cursor) },
      signal,
    })
    .then(unwrap);
}

function ordersPage(project: string, cursor: string | undefined, signal: AbortSignal) {
  return iki
    .GET("/api/v1/projects/{project_id}/stock/orders", {
      params: { path: { project_id: project }, query: pageParams(cursor) },
      signal,
    })
    .then(unwrap);
}

type Item = Awaited<ReturnType<typeof stockPage>>["items"][number];
type Lot = Awaited<ReturnType<typeof lotsPage>>["items"][number];

interface StockPage<T> {
  items: T[];
  next_cursor?: string | null;
}

function usePages<T>(
  key: readonly unknown[],
  load: (cursor: string | undefined, signal: AbortSignal) => Promise<StockPage<T>>,
): UseInfiniteQueryResult<InfiniteData<StockPage<T>>> {
  return useInfiniteQuery({
    queryKey: [...ENTITIES, "stock", ...key],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam, signal }) => {
      // A catalogue page can contain no low items and still have another page.
      // Walk those cursors before calling the filtered list empty.
      let result = await load(pageParam, signal);
      while (!result.items.length && result.next_cursor)
        result = await load(result.next_cursor, signal);
      return result;
    },
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    staleTime: 0,
  });
}

function amount(value: string, unit: string) {
  return `${value} ${unit === "1" ? "each" : unit}`;
}

function StockTable({
  name,
  columns,
  children,
}: {
  name: string;
  columns: string[];
  children: ReactNode;
}) {
  return (
    // Narrow screens scroll the table independently, including from the keyboard.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- a scrollable table
    <div className="table-scroll" role="region" aria-label={`${name} table`} tabIndex={0}>
      <table className="table" aria-label={name}>
        <thead>
          <tr>
            {columns.map((label) => (
              <th key={label} scope="col">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

interface PageState {
  error: unknown;
  isPending: boolean;
  isFetchingNextPage: boolean;
  hasNextPage: boolean;
  fetchNextPage: () => Promise<unknown>;
}

function Rows({
  query,
  label,
  count,
  children,
  empty,
}: {
  query: PageState;
  label: string;
  count: number;
  children: ReactNode;
  empty: string;
}) {
  if (query.isPending) return <Loading label={`Loading ${label}…`} />;
  return (
    <>
      {query.error ? <ProblemAlert problem={problemOf(query.error)} /> : null}
      {count ? children : query.error ? null : <p className="muted">{empty}</p>}
      {query.hasNextPage ? (
        <Button
          variant="quiet"
          busy={query.isFetchingNextPage}
          busyText="Loading…"
          onClick={() => {
            void query.fetchNextPage();
          }}
        >
          Show more {label}
        </Button>
      ) : null}
    </>
  );
}

function RunLink({
  project,
  action,
  hrid,
}: {
  project: Project;
  action: keyof typeof RUN_LABELS;
  hrid: string;
}) {
  return (
    <Link
      to="/projects/$slug/runs/new/"
      params={{ slug: project.slug }}
      search={{ action, entities: hrid }}
    >
      {RUN_LABELS[action]}
    </Link>
  );
}

function Location({ lot }: { lot: Lot }) {
  if (!lot.container_hrid) {
    if (lot.checked_out) return "—";
    return lot.placed ? "In a location you can’t open" : "Not in storage";
  }
  return (
    <>
      <Link to="/storage/$ref/" params={{ ref: lot.container_hrid }}>
        {lot.container_hrid}
      </Link>
      {lot.row != null && lot.column != null ? (
        <span className="cell-meta">
          Row {lot.row} · column {lot.column}
        </span>
      ) : null}
    </>
  );
}

function Lots({ project, item }: { project: Project; item: Item }) {
  const query = usePages([project.id, "lots", item.id], (cursor, signal) =>
    lotsPage(project.id, item.id, cursor, signal),
  );
  const lots = query.data?.pages.flatMap((p) => p.items) ?? [];
  const name = `Lots for ${item.name}`;
  return (
    <section id="stock-lots">
      <h3 className="section-title">{name}</h3>
      <Rows label="lots" query={query} count={lots.length} empty="No lots recorded for this item.">
        <StockTable
          name={name}
          columns={["Lot", "Storage location", "Physical", "Usable", "Expiry", "Holds and custody"]}
        >
          {lots.map((lot) => (
            <tr key={lot.id}>
              <th scope="row">
                <Link to="/entities/$ref/" params={{ ref: lot.hrid }}>
                  {lot.lot_number ?? lot.hrid}
                </Link>
                {lot.lot_number ? <span className="cell-meta">{lot.hrid}</span> : null}
              </th>
              <td>
                <Location lot={lot} />
              </td>
              <td>{amount(lot.amount, lot.unit)}</td>
              <td>{amount(lot.available, lot.unit)}</td>
              <td>
                {lot.expires_on ? <time dateTime={lot.expires_on}>{lot.expires_on}</time> : "—"}
                {lot.expired ? <span className="cell-meta">Expired</span> : null}
              </td>
              <td>
                {lot.checked_out ? <div>Checked out</div> : null}
                {lot.held ? <div>{lot.held}</div> : null}
                {!lot.checked_out && !lot.held ? "—" : null}
              </td>
            </tr>
          ))}
        </StockTable>
      </Rows>
    </section>
  );
}

function Stock({ project }: { project: Project }) {
  const [lowOnly, setLowOnly] = useState(false);
  const [shown, setShown] = useState<string | null>(null);
  const stock = usePages([project.id, "catalogue", lowOnly], (cursor, signal) =>
    stockPage(project.id, lowOnly, cursor, signal),
  );
  const orders = usePages([project.id, "orders"], (cursor, signal) =>
    ordersPage(project.id, cursor, signal),
  );
  const runnable = !project.archived && project.can.run.length > 0;
  const actions = useQuery({ ...actionsQuery, enabled: runnable }).data;
  const canRun = (name: string) =>
    runnable &&
    actions?.some((a) => a.name === name && a.available && project.can.run.includes(a.tier));
  const items = stock.data?.pages.flatMap((p) => p.items) ?? [];
  const selected = items.find((i) => i.id === shown);
  const lines = orders.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <>
      <h2>Stock</h2>
      <section>
        <div className="section-header">
          <h3 className="section-title">Catalogue stock</h3>
          <CheckboxField
            label="Low stock only"
            checked={lowOnly}
            onChange={(on) => {
              setLowOnly(on);
              setShown(null);
            }}
          />
        </div>
        <Rows
          query={stock}
          count={items.length}
          label="items"
          empty={
            lowOnly
              ? "No items are at or below their reorder level."
              : "No stock items in this project."
          }
        >
          <StockTable
            name="Catalogue stock"
            columns={[
              "Item",
              "Physical",
              "In storage",
              "Usable",
              "Reorder level",
              "Packs on order",
              "Actions",
            ]}
          >
            {items.map((item) => (
              <tr key={item.id}>
                <th scope="row">
                  <Link to="/entities/$ref/" params={{ ref: item.hrid }}>
                    {item.name}
                  </Link>
                  <span className="cell-meta">{item.hrid}</span>
                </th>
                <td>{amount(item.physical, item.unit)}</td>
                <td>{amount(item.stored, item.unit)}</td>
                <td>
                  {amount(item.available, item.unit)}
                  {item.low ? (
                    <div>
                      <Badge>Low stock</Badge>
                    </div>
                  ) : null}
                </td>
                <td>
                  {item.threshold === null ? (
                    <span className="cell-meta">Not reordered by level</span>
                  ) : (
                    amount(item.threshold, item.unit)
                  )}
                </td>
                <td>{item.outstanding_packs}</td>
                <td>
                  <div className="actions">
                    <AriaButton
                      className={buttonClass("quiet")}
                      aria-expanded={item.id === shown}
                      {...(item.id === shown ? { "aria-controls": "stock-lots" } : {})}
                      aria-label={`${item.id === shown ? "Hide" : "Show"} lots for ${item.name}`}
                      onPress={() => {
                        setShown(item.id === shown ? null : item.id);
                      }}
                    >
                      {item.id === shown ? "Hide lots" : "Show lots"}
                    </AriaButton>
                    {canRun(ORDER) ? (
                      <RunLink project={project} action={ORDER} hrid={item.hrid} />
                    ) : null}
                    {canRun(RECEIVE_STOCK) ? (
                      <RunLink project={project} action={RECEIVE_STOCK} hrid={item.hrid} />
                    ) : null}
                  </div>
                </td>
              </tr>
            ))}
          </StockTable>
        </Rows>
      </section>
      {selected ? <Lots key={selected.id} project={project} item={selected} /> : null}
      <section>
        <h3 className="section-title">Open orders</h3>
        <Rows
          query={orders}
          count={lines.length}
          label="orders"
          empty="No orders awaiting delivery."
        >
          <StockTable
            name="Open orders"
            columns={[
              "Order",
              "Item",
              "Ordered packs",
              "Received packs",
              "Outstanding packs",
              ...(canRun(RECEIVE) ? ["Actions"] : []),
            ]}
          >
            {lines.map((line) => (
              <tr key={line.id}>
                <th scope="row">
                  <Link to="/entities/$ref/" params={{ ref: line.hrid }}>
                    {line.order_reference ?? line.hrid}
                  </Link>
                  {line.order_reference ? <span className="cell-meta">{line.hrid}</span> : null}
                </th>
                <td>
                  <Link to="/entities/$ref/" params={{ ref: line.item_hrid }}>
                    {line.item_name}
                  </Link>
                </td>
                <td>{line.ordered_packs}</td>
                <td>{line.received_packs}</td>
                <td>{line.outstanding_packs}</td>
                {canRun(RECEIVE) ? (
                  <td>
                    <RunLink project={project} action={RECEIVE} hrid={line.hrid} />
                  </td>
                ) : null}
              </tr>
            ))}
          </StockTable>
        </Rows>
      </section>
    </>
  );
}

export function ProjectStock() {
  const project = useProject();
  usePageTitle(`Stock · ${project.name}`);
  return project.can.read ? (
    <Stock key={project.id} project={project} />
  ) : (
    <p className="empty">Only project members can see its stock.</p>
  );
}
