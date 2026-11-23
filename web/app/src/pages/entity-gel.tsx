import {
  GEL_LADDERS,
  type GelFragment,
  type GelLadderId,
  type GelLane,
  type VirtualGelData,
} from "@ikisci/science/virtual-gel";
import "@ikisci/science/virtual-gel.css";
import { unwrap } from "@ikisci/sdk";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { createElement, useId, useMemo, useState } from "react";
import { Button } from "react-aria-components";

import { iki } from "../api";
import { ENTITIES, recordSearch, type Entity } from "../entities";
import { Loading, SelectField } from "../ui";

const ASSEMBLY = "iki/plasmid.Assembly";
const LADDER_OPTIONS = (Object.keys(GEL_LADDERS) as GelLadderId[]).map((value) => ({
  value,
  label: GEL_LADDERS[value].label,
}));
const AGAROSE_OPTIONS = ["0.5", "0.7", "0.8", "1", "1.2", "1.5", "2", "2.5", "3"].map((value) => ({
  value,
  label: `${value}%`,
}));

/** The recorded digest has one row per fragment, with a part label and a length in bp. */
function sampleLanes(value: unknown): GelLane[] {
  if (!Array.isArray(value)) return [];
  const parts = new Map<string, GelFragment[]>();
  for (const row of value as unknown[]) {
    if (!row || typeof row !== "object" || !("part" in row) || !("length" in row)) continue;
    const { part, length } = row;
    if (typeof part !== "string" || !part.trim()) continue;
    if (typeof length !== "number" || !Number.isSafeInteger(length) || length < 1) continue;
    const fragment = { sizeBp: length };
    const fragments = parts.get(part);
    if (fragments) fragments.push(fragment);
    else parts.set(part, [fragment]);
  }
  return Array.from(parts, ([label, fragments]) => ({ label, fragments }));
}

function Gel({ result }: { result: Entity }) {
  const [ladder, setLadder] = useState<GelLadderId>("neb-1-kb");
  const [agarose, setAgarose] = useState("1");
  const fragments: unknown = result.values["fragments"];
  const data = useMemo<VirtualGelData | undefined>(() => {
    const lanes = sampleLanes(fragments);
    if (!lanes.length) return undefined;
    return { agarosePercent: Number(agarose), lanes: [{ ladder }, ...lanes] };
  }, [fragments, agarose, ladder]);
  if (!data) return <p className="muted">No digest fragments have been recorded.</p>;
  return (
    <>
      <div className="toolbar">
        <SelectField
          label="DNA ladder"
          value={ladder}
          onChange={setLadder}
          options={LADDER_OPTIONS}
        />
        <SelectField
          label="Agarose (%)"
          value={agarose}
          onChange={setAgarose}
          options={AGAROSE_OPTIONS}
        />
      </div>
      {createElement("iki-virtual-gel", { data })}
    </>
  );
}

function PlasmidGels({ project, sequence }: { project: string; sequence: string }) {
  const [picked, setPicked] = useState<string | null>(null);
  const query = useInfiniteQuery({
    queryKey: [...ENTITIES, "digest-results", project, sequence],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam, signal }) =>
      unwrap(
        await iki.POST("/api/v1/entities/search", {
          body: {
            project,
            type: ASSEMBLY,
            exact_type: false,
            archived: false,
            where: {
              all: [
                { field: "fragments", op: "is_set", value: true },
                {
                  any: [
                    { field: "backbone", op: "eq", value: sequence },
                    { field: "inserts", op: "has", value: sequence },
                  ],
                },
              ],
            },
            sort: { field: "updated_at", descending: true },
            limit: 25,
            ...(pageParam ? { cursor: pageParam } : {}),
          },
          signal,
        }),
      ),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    retry: false,
  });
  if (query.isPending) return <Loading label="Finding digest results…" />;
  const results = query.data?.pages.flatMap((page) => page.items) ?? [];
  const result = results.find((row) => row.id === picked) ?? results[0];
  if (!result) {
    return query.isError ? (
      <p className="muted" role="status">
        Digest results could not be loaded.
      </p>
    ) : (
      <p className="muted">No digest results for this plasmid in this project.</p>
    );
  }
  return (
    <>
      <div className="toolbar">
        <SelectField
          label="Digest result"
          value={result.id}
          onChange={setPicked}
          options={results.map(({ id, hrid, title }) => ({
            value: id,
            label: title === hrid ? title : `${title} (${hrid})`,
          }))}
        />
        {query.hasNextPage ? (
          <Button
            className="button button-quiet"
            isDisabled={query.isFetching}
            onPress={() => void query.fetchNextPage()}
          >
            {query.isFetchingNextPage ? "Loading…" : "More results"}
          </Button>
        ) : null}
        <Link
          className="button button-quiet"
          to="/entities/$ref/"
          params={{ ref: result.hrid }}
          search={recordSearch(result)}
        >
          Open digest record
        </Link>
      </div>
      {query.isFetchNextPageError ? <p role="status">More results could not be loaded.</p> : null}
      <Gel result={result} />
    </>
  );
}

export default function EntityGel({
  entity,
  kind,
}: {
  entity: Entity;
  kind: "assembly" | "plasmid";
}) {
  const heading = useId();
  const sequence: unknown = entity.values["sequence"];
  const project = entity.owner_project?.id;
  return (
    <section className="card" aria-labelledby={heading}>
      <h2 className="section-title" id={heading}>
        Virtual gel
      </h2>
      {kind === "assembly" ? (
        <Gel result={entity} />
      ) : project && typeof sequence === "string" && entity.refs[sequence] ? (
        <PlasmidGels key={`${project}:${sequence}`} project={project} sequence={sequence} />
      ) : (
        <p className="muted">No digest results are available for this record.</p>
      )}
    </section>
  );
}
