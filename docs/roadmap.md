# Roadmap to M1

Iki reaches its first release, M1, in about 50 commits. Each one is a
complete slice, like a pull request. It has its tests, docs and UI states,
passes CI on its own, and leaves `main` deployable. Larger steps take several
commits. Steps marked with a letter, such as 34c, were added along the way.

This page lists every step, what it delivers, and the commits that did it
(`git show <commit>`). Architecture decisions are in [the ADRs](adr/README.md).

Status: **done**, **in progress**, or **planned**.

## Stage 0: Foundations

| Step | What                                                                                                                   | Status | Commits             |
| ---- | ---------------------------------------------------------------------------------------------------------------------- | ------ | ------------------- |
| 1    | Monorepo scaffold, Apache-2.0 license, toolchains (uv, pnpm, cargo), CI skeleton, the first ADRs                       | done   | `457ad72`           |
| 2    | Dev cluster: kind, Tilt, Helm, CloudNativePG, SeaweedFS, Keycloak; Django with health checks, JSON logs, OpenTelemetry | done   | `6b0addc` `7851daa` |
| 3    | Supply chain: signed images and binaries, SBOMs, SLSA provenance, vulnerability and license scans                      | done   | `ca9cfee`           |

## Stage 1: Identity and access

| Step | What                                                                                          | Status | Commits   |
| ---- | --------------------------------------------------------------------------------------------- | ------ | --------- |
| 4    | Sign-in through the organization's OIDC provider, hardened sessions and headers, setup wizard | done   | `92a8a37` |
| 5    | Teams, projects, memberships and roles, decided by one permission service                     | done   | `957cdbd` |
| 6    | Append-only audit log; the app runs as a database role that can't change the schema           | done   | `2b2bf09` |
| 7    | SAML, and SCIM 2.0 provisioning with groups mapped to teams                                   | done   | `5239ec8` |
| 8    | REST API at `/api/v1` (errors, pagination, rate limits); service accounts                     | done   | `bc70566` |
| 9    | OAuth 2.1 server: PKCE, app registration and approval, scopes, consent, device flow           | done   | `8eb6e42` |

## Stage 2: UI shell and real time

| Step | What                                                                                    | Status | Commits             |
| ---- | --------------------------------------------------------------------------------------- | ------ | ------------------- |
| 10   | React app on the API: design system, accessibility checks, generated client, Playwright | done   | `d2ab0d5`           |
| 11   | Live updates over SSE; notifications in Iki, by email, and as signed webhooks           | done   | `ee23a79` `50cbaf2` |

## Stage 3: Plugins and entities

| Step | What                                                                                      | Status | Commits                                           |
| ---- | ----------------------------------------------------------------------------------------- | ------ | ------------------------------------------------- |
| 12   | The `iki/v1` spec (plugins, entity types, fields, UCUM units) and `iki plugin validate`   | done   | `4ebf18e`                                         |
| 13   | Plugins compiled to Django apps, with HRIDs, identity hashes, history and analyst views   | done   | `5eb0558` `c59b53f`                               |
| 14   | Plugins pinned in a lockfile and built into each deployment's image; GitOps rollbacks     | done   | `5a63a68` `6c8f934`                               |
| 15   | Entities: append-only revisions, provenance, edit rules, duplicates; one typed REST API   | done   | `f6c3c97` `dbb77ac`                               |
| 16   | Files: uploads checked by SHA-256 and stored once, file fields, files registered in place | done   | `1d30741` `3b8c2d0` `91a282c`                     |
| 17   | Entities in the web app: type browser, grids, entity pages, forms, uploads                | done   | `3b594dd` `679e723` `670abf8`                     |
| 18   | Search by typed conditions, saved searches, collections                                   | done   | `2bd4b87` `d733af2` `adaad69` `a46c72f` `2adf25b` |
| 19   | The dna plugin, and the template labs start their plugins from                            | done   | `a13ceb9`                                         |

## Stage 4: Actions and the first vertical slice

| Step | What                                                                                                | Status | Commits                                                     |
| ---- | --------------------------------------------------------------------------------------------------- | ------ | ----------------------------------------------------------- |
| 20   | Actions and the environments they run in; machines described in `compute.yaml`                      | done   | `18e5865` `4e36afd`                                         |
| 21   | Runs and jobs: fenced leases, retries, all-or-nothing results, quotas                               | done   | `21f0aa2` `1d9ae95` `ac9eb66` `30f4ba9` `61470c5`           |
| 22   | `iki-worker`: lease, stage, run, upload, report; `run --local`; protocol types generated            | done   | `e0eb66d` `656721a` `2fd859a` `960e372`                     |
| 23   | Backend tier: workers in the cluster with each deployment's plugins; the first end-to-end run       | done   | `5a24a07` `eed98aa` `cffaf81` `1bc3ef5` `71e1d9a` `6d67627` |
| 24   | Runs in the web app: launch, live logs, cancel, batches from the grid, lineage graph                | done   | `c1576c2` `c11617a` `86454f6` `fb798b6` `49b9331` `aee5e38` |
| 25   | MCP server: agents read, change entities and start runs; costly runs wait for the person's approval | done   | `8284599` `a55364d` `eca638e`                               |
| 26   | The Python SDK, `ikisci`                                                                            | done   | `07ff559`                                                   |
| 27   | Imports (FASTA, GenBank, NCBI), declared secrets, egress allowlists                                 | done   | `d11d031` `63bf076` `ca40dec`                               |
| 28   | Batches packed into one job; imports of millions of entities, resumable                             | done   | `8cfce68` `da9232a`                                         |
| 29   | Result reuse, reproducing a run exactly, seeds                                                      | done   | `6d109e2`                                                   |

## Stage 5: All compute tiers

| Step | What                                                                                                             | Status  | Commits                                 |
| ---- | ---------------------------------------------------------------------------------------------------------------- | ------- | --------------------------------------- |
| 30   | Client tier: workers on people's computers, running only their runs, updating themselves                         | done    | `7811654` `2de4fd7` `9b5cbb9`           |
| 31   | Container tier: pods per run, warm pools, only signed or approved images admitted                                | done    | `9c4ba09` `eafed8a` `254e391`           |
| 32   | The protein plugin: translation, ProtParam, UniProt and PDB imports, structures, DSSP. ESMFold waits for step 36 | done    | `3a1d9b0` `316f1ce` `abd151d` `3d352ad` |
| 32b  | Project isolation: a run reads only its project; a project can keep its entities to itself                       | done    | `a87a4b9` `25164bf`                     |
| 33   | Run artifacts with retention; checkpointed runs resume                                                           | done    | `40e395e` `fefae69`                     |
| 34   | What runs cost, and budgets that warn and can stop spending                                                      | done    | `98b21ff` `a2f3304`                     |
| 34b  | Tests run in parallel                                                                                            | done    | `34b2610`                               |
| 34c  | An agent's changes name both the person and the agent                                                            | done    | `6ca056c`                               |
| 34d  | Unattended agents act for a person by delegation (RFC 8693 token exchange)                                       | done    | `c4ef170`                               |
| 35   | VM tier: AWS EC2, ephemeral and persistent; spot interruptions resume from checkpoints                           | done    |                                         |
| 36   | Managed volumes and environment setup builds (snapshots, FSx, data versions in provenance); ESMFold              | planned |                                         |
| 37   | The structure_prediction plugin: AlphaFold2, ColabFold, Boltz                                                    | planned |                                         |

## Stage 6: Orchestration

| Step | What                                                                                                              | Status  |
| ---- | ----------------------------------------------------------------------------------------------------------------- | ------- |
| 38   | Workflows: DAGs, fan-out and fan-in, conditions, approval gates, UI                                               | planned |
| 39   | Triggers with loop protection, including scheduled ones run as a service account                                  | planned |
| 39b  | Linking LIMS and ELN records through plugins: unique external IDs, imports that update, lookup, opt-in write-back | planned |

## Stage 7: Components, views and apps

| Step | What                                                                                | Status  |
| ---- | ----------------------------------------------------------------------------------- | ------- |
| 40   | Component framework: Web Components bundled at deploy time, slots, linked selection | planned |
| 41   | Views and viewers: Mol\*, sequence and track viewers, parsers in WebAssembly        | planned |
| 42   | Apps: composed pages with an expression language, and MCP tools to build them       | planned |

## Stage 8: Remaining plugins and lifecycle

| Step | What                                                                    | Status  |
| ---- | ----------------------------------------------------------------------- | ------- |
| 43   | The antibody plugin                                                     | planned |
| 44   | The small_molecule plugin, with a 2D molecule component                 | planned |
| 45   | Detaching and purging plugins, with tombstones                          | planned |
| 46   | `iki plugin upgrade` codemods, a migration test harness, version checks | planned |
| 47   | Exporting and importing bundles between deployments                     | planned |

## Stage 9: Production readiness

| Step | What                                                                                                                      | Status  |
| ---- | ------------------------------------------------------------------------------------------------------------------------- | ------- |
| 48   | OpenTofu modules for AWS, and production Helm values                                                                      | planned |
| 49   | Dashboards and alerts, compute admin pages, a disaster-recovery runbook with an automated restore test                    | planned |
| 50   | Hardening and release: authorization fuzzing, a cross-browser CSP suite, load tests, the docs site, the signed M1 release | planned |
