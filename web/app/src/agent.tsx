/**
 * The agent that acted for a person (ADR 0072): "Ada Lovelace, through Claude
 * Code", and in a record's summary, what Iki knows of it and what it says of
 * itself.
 */

import type { Schemas } from "@ikisci/sdk";

export type Agent = Schemas["AgentOut"];

/** ", through Claude Code". Nothing when no agent acted. */
export function Through({ agent }: { agent: Agent | null | undefined }) {
  return agent ? <>, through {agent.name}</> : null;
}

/** A summary's rows about the agent: its client and grant, its MCP session, and what it
 * says it is, labelled as its own word. Nothing when no agent acted. */
export function AgentRows({ agent }: { agent: Agent | null | undefined }) {
  if (!agent) return null;
  return (
    <>
      <dt>Agent</dt>
      <dd>
        {agent.name}
        {agent.client_id ? (
          <>
            {" "}
            (client <code>{agent.client_id}</code>)
          </>
        ) : null}
        {agent.grant ? (
          <>
            , grant <code>{agent.grant}</code>
          </>
        ) : null}
      </dd>
      {agent.mcp_session ? (
        <>
          <dt>MCP session</dt>
          <dd>
            <code>{agent.mcp_session}</code>
          </dd>
        </>
      ) : null}
      {agent.reported.software || agent.reported.model ? (
        <>
          <dt>Says it is</dt>
          <dd>
            {[agent.reported.software, agent.reported.model].filter(Boolean).join(", ")}{" "}
            <span className="muted">(as the agent reports it)</span>
          </dd>
        </>
      ) : null}
    </>
  );
}
