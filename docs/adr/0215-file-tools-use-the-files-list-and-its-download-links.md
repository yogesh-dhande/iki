# 0215. File tools use the Files list and its download links

- Status: Accepted
- Date: 2026-11-25
- Refines: [0036](0036-files-content-addressed-uploads.md),
  [0049](0049-mcp-tools-call-the-rest-api.md),
  [0051](0051-the-python-sdk.md),
  [0072](0072-agents-changes-name-the-person-and-the-agent.md),
  [0177](0177-a-projects-files-are-found-by-name-and-say-what-uses-them.md)

## Context

A scientist can find a file by part of its name or acquisition date in Iki and
see which record revisions and runs used it. An agent investigating the same
question needs those results even when the file lives in an instrument's own
bucket. Many questions can be answered from the file's history alone.

## Decision

The three MCP tools use the Files REST endpoints under the caller's grant.
`find_files` returns a page, the capped match count and the format counts
shown in the Files list. `get_file` returns metadata and a page each of the
records and runs under **Used by**. Independent cursors let the caller follow
a long history. Both tools include only files and uses the caller can read.
Looking up an unreadable file by ID returns the same 404 as an unknown file.

`get_file_download_link` asks the file's content URL endpoint for a signed
link when the agent needs the bytes. The link reads one file for five minutes.
The answer includes its expiry and the SHA-256 to check after transfer. Iki
audits issuing the link with the file, the person and the agent acting for
them through MCP. Consumption of the link takes place at object storage.

The SDK wraps the same list, count and detail endpoints and the existing
registration workflow. A script can name an object version or let Iki pin
the current version. Iki checks the deployment's approved locations, checks
that the object has a durable version and verifies its bytes. The call
returns the resulting file with its SHA-256.

## Consequences

The web app, MCP and SDK share the rules for file visibility, filters, sort,
revision spans and run roles. A single MCP answer can require several REST
reads. Each read is subject to the caller's grant and rate limit.

MCP leaves text previews out. A prefix of a CSV can end partway through a
quoted row or a multibyte character, and even a short file can contain
instructions written by its author. The client can choose a parser from the
file's media type and check the downloaded bytes against the recorded hash.
