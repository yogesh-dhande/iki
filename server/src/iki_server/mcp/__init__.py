"""Iki's MCP server (ADR 0013): AI agents' way in, at ``/mcp``.

Agents sign in with OAuth like any other client, and every tool calls Iki's own
REST API in-process with the agent's token. So a tool can do exactly what the
endpoint behind it allows the person the agent acts for (their roles, the
token's scopes and projects), answers with the same documents, and fails with
the same problems. Each call is audited as made through MCP, with the client's
name.
"""
