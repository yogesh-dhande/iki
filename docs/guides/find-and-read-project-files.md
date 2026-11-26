# Find and read a project's files with an agent

Suppose you need the plate reader CSV acquired on March 14 and the runs
that used it. Connect your agent to your deployment's `/mcp` endpoint
and grant `read` for the project. [Connect an MCP client](../../server/README.md#ai-agents-mcp)

1. Call `list_projects` and take the project's `id`. Pass it to
   `find_files` with the filters below. Replace `<project-id>` with that ID.

   ```json
   {
     "project": "<project-id>",
     "q": "plate",
     "format": ["text/csv"],
     "acquired_from": "2026-03-14",
     "acquired_to": "2026-03-14"
   }
   ```

2. Read the names, acquisition dates and use counts in `items`. If the
   matching file is on a later page, repeat the search with `next_cursor`
   as `cursor`. Call `get_file` with the selected item's `id` as
   `file_id`. In `records.items`, each span names the fields and the first
   and last revisions that held the file. A null `last` means the record
   still holds it. In `runs.items`, `param` means the file was supplied as
   a parameter, `input` means an input record held it, and `made` means
   the run produced it. Use each page's cursor to read further.
3. If the question needs the CSV's measurements, check `file.scan`
   in the `get_file` result. A pending scan can be polled with `get_file`.
   A quarantined file needs review by a Quarantine Admin. When
   `file.scan.usable` is true, call `get_file_download_link` with the same ID.
   Fetch the returned
   `url` without an Authorization header and verify the bytes against
   `sha256` before parsing the CSV. Request a fresh link if it expires.
   Cite the file ID and SHA-256 in the result so a colleague can identify
   the same data after the link expires.

The [MCP tool reference](../../server/README.md#file-tools) lists all filters and
sort orders. For a notebook or script, see the [Python SDK file methods](../../sdk/python/README.md#files).
