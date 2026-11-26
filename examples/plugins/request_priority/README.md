# Request priority

A sequencing core serves three projects: Oncology discovery, Antibody discovery
and Assay methods development. Its coordinators decide who goes first after each
portfolio review. This plugin lets the core keep that decision as its own
records. Each request sent to the core takes the rank of the project that asked,
and the core's queue sorts by rank, then priority, then the date each request is
needed by.

Iki doesn't rank projects itself. This plugin is one core's rule, built from
general primitives
([ADR 0232](../../../docs/adr/0232-a-service-orders-its-own-work-from-primitives.md)).
Change the rule to suit your core. A core that ranks by hand needs no plugin: its
coordinators set each request's `priority`.

## What it adds

- **Project served** (`ServedProject`, `SRV-`) is one of the core's records for
  each project it serves. It holds the project's slug, its name, a rank (1 is
  served first) and why. Anyone in the core can propose a rank. A coordinator
  agrees it, which locks the record. Changing an agreed rank takes the record
  back to Proposed, and only a coordinator can do that. Only agreed ranks count.
- **Sequencing request** (`SequencingRequest`, `SEQ-`) extends Plain request
  with `project_rank`, one of the service's fields. The asking project never
  sets it.
- **Rank requests by project** (`rank_requests`) gives each request it's given
  the agreed rank of the project that asked, or no rank. It reads the core's
  whole ranking itself (`from_project`), so nobody picks the ranking when
  launching it.
- **Rank requests as they arrive** (`rank_arriving_requests`) runs it on each
  Sequencing request as it's sent to the core (`on: request.received`).

## Set up the core

1. Install the plugin with `iki/requests` and `iki/lab_roles`. In the core's
   project, under **Settings → Request intake**, accept Sequencing requests
   from the projects it serves.
2. Give the core's coordinators the Coordinator lab role in its project.
3. Make a Project served record for each project. **Project slug** is the project's
   slug, the last part of its address in Iki: `oncology-discovery` in
   `/projects/oncology-discovery/`. A slug never changes, even if the project
   is renamed. A coordinator sets each record to **Agreed**.
4. Add a service account to the core's project as a Runner. Under
   **Automation → Triggers**, an Admin turns on **Rank requests as they
   arrive** and chooses that account. The trigger's runs are the account's, on
   the Admin's authority. The trigger turns itself off if either of them can no
   longer run it.
5. On the **Requests** tab, choose Sequencing request. Sort by **Project rank,
   lowest first**. Then add **Then by → Priority, urgent first** and **Then
   by → Needed by, oldest first**. Save the search to keep that order.

## Re-rank after a review

A change to the ranking doesn't change requests that have already arrived.
After the coordinators agree the new ranks, re-rank the open requests:

1. On the Requests tab, filter Sequencing requests by Status: Submitted,
   Accepted and In progress.
2. Select them, and run **Rank requests by project**.

The run reads the ranking as it is now. A project that is no longer ranked
loses its rank, and its requests sort after the ranked ones.

## Adapt it

- To rank by something else, such as a contract tier or a due date, change
  `rank` and `scripts/rank.py`. The run is also given the request's own values
  and both of its sides (`request.asking_project`, `request.service`).
- To let a rank set the priority, add `priority` to the action's `updates` and
  set it in the script. Coordinators can still change it by hand afterwards.
- `project_rank` is `computed`, so only the action sets it. To let coordinators
  override a rank on one request, make it a plain service field. The next
  re-rank then overwrites the override.

## Test it

From the root of Iki's checkout:

```sh
uv run --locked python scripts/ci/check_plugin_examples.py examples/plugins/request_priority
```

`tests/rank.yaml` runs the action on requests from the three projects. It covers
a proposed rank that doesn't count yet, a re-rank, and a project that was taken
out of the ranking.
