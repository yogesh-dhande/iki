# iki-spec

The versioned specs for Iki plugins, and the Python package that reads and
checks them. Version `iki/v1` covers plugins, entity types, extensions,
fields, units, settings, actions, environments and workflows; the run
protocol, triggers and components arrive in later versions of this document
as they land.

- **JSON Schemas** (draft 2020-12), in [`src/iki_spec/schemas/`](src/iki_spec/schemas/).
  Each `<name>/v1.json` is published at `https://ikisci.org/schemas/<name>/v1`.
  Use [`manifest/v1`](src/iki_spec/schemas/manifest/v1.json) for any manifest;
  it picks the right schema by `kind`.
- **The validator** behind `iki plugin validate` (see [`cli/`](../cli/README.md)),
  which also checks what a schema can't: units, references across files and
  plugins, table and index rules, identities, settings, and what actions read,
  write and run in.
- **Units** ([`iki_spec.units`](src/iki_spec/units/__init__.py)): parsing,
  checking and converting [UCUM](https://ucum.org) codes, used by the
  validator now and by the server when it stores quantities.

## A plugin

A plugin is a git repository (or a directory in one):

```text
plugin.yaml          # kind: Plugin — exactly one, at the root
types/               # kind: EntityType and EntityTypeExtension
  protein.yaml       # any number of files, each with one or more documents (---)
  structure.yaml
actions/             # kind: Action
environments/        # kind: Environment
workflows/           # kind: Workflow
migrations/          # made by `iki plugin makemigrations`, committed and reviewed
```

Every manifest starts with `apiVersion: iki/v1` and a `kind`.

```yaml
apiVersion: iki/v1
kind: Plugin
name: protein # lower case; types are protein.Name, tables protein_name
version: 3.0.0 # semantic version, for people (deployments pin the git commit)
title: Proteins
license: Apache-2.0
dependencies:
  dna:
    types:
      DNASequence: ">=2,<4" # schema versions this plugin works with
settings:
  uniprot_mirror: { type: string, format: uri, default: "https://rest.uniprot.org" }
```

## Entity types

```yaml
apiVersion: iki/v1
kind: EntityType
name: Protein
schema_version: 3 # increase it whenever the fields change
extends: dna.Sequence # optional parent type (multi-table inheritance)
hrid: { prefix: PRT, digits: 6 } # PRT-000123, never reused
identity: [sequence] # what makes two entities the same
on_duplicate: reuse # reuse (default) | warn | allow
fields:
  sequence: { type: string, format: protein_sequence, required: true, immutable: true }
  molecular_weight: { type: quantity, unit: u, display_unit: ku, computed: true, indexed: true }
  organism: { type: string, max_length: 200, indexed: true }
  source: { type: ref, to: core.Entity } # any entity
display:
  title: "{{ name or hrid }}"
```

| Key                     | Meaning                                                                                             |
| ----------------------- | --------------------------------------------------------------------------------------------------- |
| `name`                  | PascalCase. Elsewhere the type is `plugin.Name`; in the same plugin, `Name` is enough.              |
| `label`, `label_plural` | What people see. Default: the name split into words, and that plus "s".                             |
| `schema_version`        | A whole number from 1. Dependencies and actions name the versions they work with.                   |
| `extends`               | A parent type. Entities of this type are also entities of the parent, with its fields.              |
| `abstract`              | Only subtypes have entities. Abstract types have no `hrid`; every concrete type must have one.      |
| `hrid`                  | Human-readable IDs: a 2–8 character prefix (capitals and digits), zero-padded to `digits` (3–12).   |
| `identity`              | Fields that make an entity the same as another. Each must be `required` and `immutable`. See below. |
| `on_duplicate`          | When someone creates a duplicate: `reuse` the existing entity, `warn` and let them choose, `allow`. |
| `display.title`         | Text with `{{ a or b or "text" }}` parts, naming fields or `hrid`.                                  |

### Identities

Iki stores a hash of each entity's identity values, to find duplicates.
Subtypes share the identity of the type that declares it. The hash is the
SHA-256, in lower-case hex, of UTF-8 JSON with sorted keys and no spaces:

```json
{ "type": "dna.DNASequence", "v": 1, "values": { "bases": "ACGT" } }
```

Here written with spaces for reading; the hashed form is
`{"type":"dna.DNASequence","v":1,"values":{"bases":"ACGT"}}`. `type` is the
type that declares the identity. Each value is encoded by its field's type:

| Type                     | Encoded as                                |
| ------------------------ | ----------------------------------------- |
| `string`, `text`, `enum` | The text, in Unicode NFC                  |
| `int`                    | A JSON integer                            |
| `bool`                   | `true` or `false`                         |
| `date`                   | `"YYYY-MM-DD"`                            |
| `datetime`               | UTC: `"YYYY-MM-DDTHH:MM:SS.ffffffZ"`      |
| `ref`                    | The entity's UUID, lower case             |
| `file`                   | The SHA-256 of the file's content, in hex |

[`iki_spec.identity`](src/iki_spec/identity.py) implements it.

### Extensions

A plugin adds fields to another plugin's type with an extension. The fields
live in the extending plugin's own table
(`structure_prediction__protein_proteinstructure`).

```yaml
apiVersion: iki/v1
kind: EntityTypeExtension
extends_type: protein.ProteinStructure
fields:
  plddt_mean: { type: float, computed: true, indexed: true, min: 0, max: 100 }
```

Extension fields can't be `required` (existing entities, and the owning
plugin, wouldn't have them), can't reuse a name the type already has, and
`core.Entity` can't be extended.

## Fields

Every field has a `type`, and may have `label`, `description`, `required`,
`immutable` (set once, never changed), `computed` (set only by actions),
`indexed` (a database index for fast filtering and sorting; indexed strings
are also fast to search for text they contain), `default` and
`removed`.

A field is never deleted, because its history is kept. To stop using one,
mark it `removed: true`: its column is dropped, and its values stay in the
type's history. Identity fields can't be removed.

| Type       | Options                                         | Notes                                                                      |
| ---------- | ----------------------------------------------- | -------------------------------------------------------------------------- |
| `string`   | `format`, `pattern`, `min_length`, `max_length` | One line. Never empty unless `min_length: 0`. Indexed: `max_length` ≤ 600. |
| `text`     | `max_length`                                    | Long text. Not indexable: searched by its words (full text) instead.       |
| `int`      | `min`, `max`                                    | 64-bit.                                                                    |
| `float`    | `min`, `max`                                    | Double precision.                                                          |
| `bool`     |                                                 |                                                                            |
| `enum`     | `values` (required)                             | Letters, digits and `_ . + -`.                                             |
| `date`     |                                                 | Defaults are written `YYYY-MM-DD`.                                         |
| `datetime` |                                                 | Always with a time zone. No default.                                       |
| `quantity` | `unit` (required), `display_unit`, `min`, `max` | Stored in `unit`, a UCUM code. `min`/`max` are in that unit.               |
| `file`     | `media_types`, `max_size`                       | Such as `[application/json, image/*]` and `2GiB` (at most 5 TiB).          |
| `ref`      | `to` (required)                                 | Another entity, of type `to` or its subtypes; `core.Entity` for any.       |
| `list`     | `items` (required), `min_items`, `max_items`    | `items` is a type and its options, without the flags. No lists of lists.   |
| `json`     | `schema`                                        | Opt-in. `schema` is a JSON Schema the value must match. Not indexable.     |

String formats, checked by the database too: `dna_sequence`,
`rna_sequence`, `protein_sequence` (IUPAC codes, either case), `email` and
`uri`. A `pattern` must work in both Python and PostgreSQL, so it can't use
lookarounds, named groups, backreferences, inline flags or possessive
quantifiers.

Field names are lower case with single underscores. Names every entity
already has (`id`, `hrid`, `type`, `project`, `created_at`, `revision`,
`archived_at` and the like), names ending in `_ptr`, Python keywords and
Django model methods are reserved.

## Tables

`iki plugin build` compiles a plugin into a Django app with these tables
(see [ADR 0033](../docs/adr/0033-plugin-codegen-and-migrations.md)). Every
name must fit PostgreSQL's 63 characters.

| Table                                            | Holds                                                     |
| ------------------------------------------------ | --------------------------------------------------------- |
| `protein_protein`                                | `protein.Protein`'s own fields, keyed by `entity_id`      |
| `protein_protein__history`                       | Its values at each revision, removed fields included      |
| `protein_proteinstructure__chains`               | A list of references or files: one row per item, in order |
| `structure_prediction__protein_proteinstructure` | The fields `structure_prediction` adds to that type       |

Other lists are arrays. PostgreSQL checks every rule it can (formats,
patterns, enums, bounds, lengths, list sizes) with CHECK constraints, so bad
data is refused however it is written. History tables are append-only, for
every role.

At deploy time, `iki-server sync_plugins` creates each prefix's HRID
sequence, comments every table and column from the manifests, and builds a
read-only view per type in the `iki_views` schema, such as
`iki_views.protein_protein`. Each view joins the type's fields, its parents'
and those other plugins add. Plugins installed together can't share an HRID
prefix, a table, or a field they add to the same type.

## Units

Quantities use [UCUM](https://ucum.org) codes, the units standard of lab
systems, HL7 and FHIR. They are case-sensitive: `mg` is a milligram, `Mg` a
megagram. Some codes people expect are spelled differently:

| You might write | UCUM                          |
| --------------- | ----------------------------- |
| `Da`, `kDa`     | `u`, `ku` (or `g/mol`)        |
| `°C`            | `Cel`                         |
| `µg`            | `ug`                          |
| `mM`, `nM`      | `mmol/L`, `nmol/L`            |
| `hr`            | `h`                           |
| `IU`, `ppm`     | `[IU]`, `[ppm]`               |
| `bp`            | `{bp}` (a counted annotation) |

The validator suggests these. Special units (`Cel`, `[degF]`, `[pH]`,
decibels) can't be multiplied, divided or raised to a power; use `K` for
temperature differences and rates.

Iki reads the official UCUM table (version 2.2), which ships unmodified in
[`src/iki_spec/units/`](src/iki_spec/units/) under the
[UCUM License](src/iki_spec/units/UCUM-LICENSE.md). The parser and
converter are Iki's own (Apache-2.0).

## Settings

```yaml
settings:
  ncbi_email: { type: string, format: email, required: true, description: "…" }
  batch_size: { type: int, min: 1, default: 100 }
```

Settings take `string`, `text`, `int`, `float`, `bool`, `enum` and `list`
(of those), with the same options as fields. A setting is either `required`
or has a `default`. Each deployment sets the values in its config
repository; check them with `iki plugin validate --settings FILE`. Settings
aren't secret, so names that look like secrets (`api_key`, `password`,
`token`) get a warning: declare secrets on the actions that use them.

## Actions

An action is something a plugin does to entities: compute their properties,
create new entities from them, or import entities from elsewhere. Each run
records what it read, what it wrote, and the code and machine it ran on.

```yaml
apiVersion: iki/v1
kind: Action
name: predict_structure
inputs: # leave out for actions that import or fetch
  protein: { type: protein.Protein, schema: ">=2" } # cardinality: one (default) or many
params:
  model_preset: { type: enum, values: [monomer, multimer], default: monomer }
effects: # what a run may write; anything else is refused
  creates: [{ type: protein.ProteinStructure, cardinality: many, derived_from: [protein] }]
  updates: [] # e.g. [{ input: protein, fields: [molecular_weight] }]
runs_on: { tier: vm, environment: alphafold-gpu, lifecycle: ephemeral, resources: { memory: 96Gi } }
command: [python, /app/run.py] # a list of arguments, no shell; ./path is a file in the plugin
deterministic: true # the same inputs give the same results, so runs can be reused
checkpointable: true # saves progress in $IKI_CHECKPOINT_DIR and resumes
batchable: 1 # more than 1 packs up to that many runs of a batch into one job (one input only)
artifacts: { keep: ["msas/**", ranking_debug.json], retention: 365d }
network: [] # hosts a run may reach; none by default
secrets: [] # e.g. [LIMS_TOKEN, { name: NCBI_API_KEY, required: false, description: ... }]
timeout: 8h # 1h by default, at most 7d
retries: { max: 2, on: [worker_lost, interrupted] }
```

The tiers are `client` (the person's own machine), `backend` (Iki's isolated
worker pods), `container` (the plugin's image on Kubernetes) and `vm`. On the
container and vm tiers, `lifecycle` says what machine a run needs: `ephemeral`
(the default), a fresh one that runs nothing else, or `persistent`, one that
runs one job after another and is kept warm, for quick actions that run often.
A persistent machine starts each run clean, but other projects' runs have run
on it. The validator checks that:

- inputs, created types and updated fields exist, and the fields aren't
  removed or `immutable`; updates reach inherited fields and fields this
  plugin adds to the type
- every `derived_from` and `updates` names an input
- params follow the rules for settings, and also take `date`, `quantity`
  and `file` (one file, such as one to import, with `media_types` and
  `max_size`; never a default, and never a reference, which is an input)
- the environment is one of this plugin's and runs on the action's tier
- client-tier actions declare no `secrets`, `network` or `resources`, since
  Iki can't keep secrets from, or limit, a person's own machine
- files the command names with `./` are in the plugin, and aren't symbolic links

A run keeps as artifacts the files in its output directory (`$IKI_OUT_DIR`)
that one of the `artifacts.keep` patterns matches, whether the command
succeeded or not, for people to download from the run's page. Patterns match
a path relative to that directory one segment at a time: `*` and `?` within
a segment, `[...]` a set of characters, and a `**` segment any number of
segments. Each attempt keeps at most 1,000. They're deleted once
`retention` is over (the deployment's default if it's left out, 90 days
unless it says otherwise, and never less than its minimum). Batchable
actions can't keep artifacts.

A run gets the secrets its action declares under `secrets` as environment
variables, and no others. Each deployment keeps them in its own secret
manager, for this plugin alone (`dna.NCBI_API_KEY` is the dna plugin's), so a
secret is never another plugin's even under the same name. Their values are
never in `input.json`, and `***` stands for them in the log. A required secret
the deployment hasn't set keeps the action from running, and says so; give
optional ones (`required: false`) a `description` that tells administrators
what they're for and where to get one.

A run reaches the network only if its action names the hosts under
`network` (`*.example.com` covers subdomains, not example.com itself), and
only over HTTPS on port 443, through the deployment's egress proxy. The
command finds the proxy, with its attempt's credentials, in `HTTPS_PROXY`,
which Python's `urllib`, `requests`, `httpx` and `curl` use as they are.
Hosts it doesn't name, plain HTTP and private addresses are refused, and the
run's page lists each host an attempt reached or was refused. On a deployment
without the proxy, such actions can't run.

Runs whose machine went away (`worker_lost`, `interrupted`) are retried twice
by default. Actions that reach the network aren't retried unless they say so,
since a retry could repeat what they did elsewhere, such as placing an order.

## Environments

An environment is what a plugin's actions need, wherever they run. Each
deployment binds it to compute templates of its own.

```yaml
apiVersion: iki/v1
kind: Environment
name: alphafold-gpu # kebab-case
tiers: [vm, container]
resources: { cpu: 8, memory: 64Gi, gpu: { count: 1, min_memory: 40Gi }, scratch_disk: 200Gi }
image: ghcr.io/acme/alphafold@sha256:9f86d0… # by digest, for container and vm
volumes:
  af-db:
    size: 3Ti
    access: read_only_many # or read_write_once
    performance: high_iops # standard, high_iops or high_throughput
    populated_by: setup
    mount: /data/af
setup: { script: setup/download_dbs.sh, timeout: 12h, requires_approval: true }
```

The backend tier builds a Python or conda environment from a lockfile in the
plugin; the client tier does too, or runs the image with Docker or Podman:

```yaml
python: { version: "3.12", requirements: requirements.txt }
# or
conda: { lockfile: conda.lock }
```

Everything an environment installs is pinned, so a run can be reproduced
exactly: images by digest, every Python package with `==` and its
`--hash=sha256:` (as `uv pip compile --generate-hashes` writes them), and
conda packages in an explicit lockfile with their hashes. Requirements can't
include other files or editable packages. Volumes mount on the container and
vm tiers only, at absolute paths that don't overlap.

## Workflows

A workflow runs several of the plugin's actions as one (ADR 0079). Each step
runs an action, by its name in the plugin, and says where each of its inputs
comes from: the workflow's input (`$inputs.<name>`), the entities a step
created of a type (`$steps.<id>.created.<Type>`), or what a step was given
(`$steps.<id>.inputs.<name>`), once that step has run. A param takes a value,
or the workflow's param (`$params.<name>`); a value that starts with `$` is
written `$$`.

```yaml
apiVersion: iki/v1
kind: Workflow
name: translate_and_characterize # snake_case; qualified as protein.translate_and_characterize
label: Translate and characterize
inputs:
  sequence: { type: dna.DNASequence, schema: ">=1" } # as an action's inputs
params:
  frame: { type: int, min: 1, max: 3, default: 1 } # as an action's params
steps:
  translate:
    action: translate
    inputs: { sequence: $inputs.sequence }
    params: { frame: $params.frame }
  properties:
    action: compute_properties
    inputs: { protein: $steps.translate.created.Protein }
    # needs: [other_step]  # wait for a step it doesn't take from
```

A step starts once the steps it takes from, and those it `needs`, have
succeeded; steps that don't wait for each other run side by side. The build
checks each step against its action: every input it needs comes from
somewhere, of a type it takes and, where it takes one entity, not from
something that can be many; every required param is given, each value fits,
and a workflow param given to a step's param takes no value the step's
doesn't. No step may come before itself.

## Dependencies

A plugin lists the plugins whose types it uses, with the schema versions it
works with. To check those references, pass the dependencies' directories
with `--with`. Without them, the validator says it can't check them rather
than guessing. Core types (`core.Entity`, `core.Sequence`, `core.Collection`)
need no dependency.

## The core plugin

Every deployment has the core plugin, released with Iki. Its manifests are
in [`iki_spec/core`](src/iki_spec/core). Other plugins use its types without
declaring a dependency, and no other plugin can be named `core`:

- `core.Entity`: any entity. It's the server's own model, so it isn't generated.
- `core.Sequence`: anything with a sequence of residues (abstract). Plugins'
  sequence types extend it, so one search finds DNA and proteins alike.
- `core.Collection`: a named set of entities. A static collection lists its
  `members`; a dynamic one has a `search`, run as whoever reads it
  ([ADR 0038](../docs/adr/0038-core-plugin-app-and-collections.md)).

The server ships the core plugin's app, generated like any plugin's. After
changing these manifests, run `make core` to regenerate it and make its
migrations.

## A deployment's lockfile

Each deployment's config repository pins the plugins it installs in
`plugins.lock.yaml` ([schema](src/iki_spec/schemas/plugin-lock/v1.json)):

```yaml
apiVersion: iki/v1
kind: PluginLock
plugins:
  - name: dna
    git: https://github.com/acme/iki-plugins.git
    path: dna # the plugin's directory in the repository, if not its root
    sha: 4f1c2b9e0d6a7c3b8e5f1a2d9c0b7e6f5a4d3c2b
    state: active
    settings: settings/dna.yaml # relative to the lockfile
```

`sha` is a full commit, never a branch or tag, so the same lockfile always
installs the same plugins. `git ls-remote <repository> <tag>` prints a
tag's commit. In development, `local: ../path/to/plugin` takes a directory
instead. `iki deploy build` builds the image's plugins from the lockfile
(see [`cli/`](../cli/README.md) and ADR 0034).

## A deployment's compute

Beside its lockfile, a deployment's directory can hold `compute.yaml`: the
machines it runs actions on (compute templates) and which environments run
on which (bindings). Compute admins own it; changes go through pull requests
like the lockfile's. Point editors at
[`compute/v1`](src/iki_spec/schemas/compute/v1.json).

```yaml
apiVersion: iki/v1
kind: ComputeTemplate
name: a100-spot
provisioner: aws_ec2 # backend_pool, k8s_job, k8s_deployment or aws_ec2
lifecycle: ephemeral # aws_ec2 only; k8s_job is ephemeral, k8s_deployment persistent
capacity:
  { cpu: 96, memory: 1152Gi, gpu: { count: 8, memory: 40Gi, model: A100 }, scratch_disk: 8Ti }
aws:
  instance_type: p4d.24xlarge
  # Docker, and for GPUs NVIDIA's drivers and container toolkit
  image: resolve:ssm:/aws/service/ecs/optimized-ami/amazon-linux-2023/gpu/recommended/image_id
  spot: true
  fallback: on_demand # or none: wait for spot capacity
  region: us-east-1
  subnets: [subnet-0a1b2c3d4e5f60718, subnet-0f1e2d3c4b5a69788] # private, one or more zones
  security_groups: [sg-0a1b2c3d4e5f60718] # no inbound; outbound to Iki, storage, registry
  instance_profile: iki-vm-worker # needs no permissions
  # root_disk: 9Ti (scratch_disk and 40Gi unless set), root_device: /dev/xvda,
  # kms_key: alias/iki-runs, tags: { cost-center: rd-42 }
volumes: { af-db: { strategy: clone_from_snapshot, fast_snapshot_restore: true } }
limits: { max_concurrent: 10 } # persistent ones also take min_warm and idle_shutdown (10m)
price: { per_hour: 32.77, currency: USD }
---
apiVersion: iki/v1
kind: EnvironmentBinding
environment: structure_prediction.alphafold-gpu # plugin.environment
templates:
  - { template: a100-spot } # priority 0 is tried first
  - { template: a100-on-demand, priority: 1 }
```

For each job, Iki picks among the bound templates on the action's tier that
it fits and whose lifecycle the action asks for: the lowest priority number
first, then the smallest. A persistent template keeps `min_warm` machines
running for each environment bound to it, and stops others once they've had
nothing to do for `idle_shutdown` (ten minutes unless it says). Kubernetes
templates take `kubernetes`, with `namespace`, `node_selector`,
`tolerations` and `runtime_class` (such as `gvisor`). `aws_ec2` templates
take `aws`: each instance boots `image`, which needs Docker (and for GPUs,
NVIDIA's drivers and container toolkit), in one of `subnets`, with
`security_groups` and `instance_profile`. With `spot`, Iki tries spot capacity
in each subnet's zone first, then on-demand with `fallback: on_demand`; spot
instances AWS takes back are retried, and checkpointable runs resume (ADR
0074). `iki deploy build` checks that:

- every binding names an environment of an installed plugin, and templates
  in this file on that environment's tiers
- every bound template is at least the environment's minimum, and every
  action fits at least one template on its tier
- each environment volume has a strategy on every template it's bound to
  (`clone_from_snapshot`, `attach_exclusive`, `shared_filesystem` or
  `object_mount`), and volumes written by one run at a time are attached
  exclusively

Actions whose environments aren't bound on their tier can't run on that
deployment; the build warns about each. Client-tier actions need no
template.

A volume an environment's setup fills gets its data from a setup build
(ADR 0075): a Compute admin asks for one, the setup script fills a fresh
disk on a machine of its own, and Iki snapshots it as a new version. On
Kubernetes, with `clone_from_snapshot`, each run pod gets a read-only disk
made from the newest version's snapshot; the template's `storage_class` and
`snapshot_class` name the StorageClass and VolumeSnapshotClass to use (the
cluster's defaults unless set):

```yaml
volumes:
  af-db:
    strategy: clone_from_snapshot
    storage_class: ebs-gp3
    snapshot_class: ebs-snapshots
```

On `aws_ec2`, each instance gets a new EBS disk from the snapshot, mounted
read-only (ADR 0076). With `fast_snapshot_restore`, a version is ready only
once its snapshot starts disks at full speed in each of the template's
zones, at an hourly cost for each; `iops` and `throughput` (MiB/s) set its
disks' performance, gp3's baseline unless set:

```yaml
volumes:
  af-db:
    strategy: clone_from_snapshot
    fast_snapshot_restore: true
    iops: 16000
    throughput: 1000
```

On Kubernetes, with `shared_filesystem`, each version is a directory of its
own on a file system many pods mount at once, such as FSx for Lustre, EFS or
NFS (ADR 0077). `claim` names its PersistentVolumeClaim, in the namespace
runs go in; Iki keeps versions under `path` in it (`iki` unless set), which
must exist, writable by runs' user (UID 10001). A setup build writes its
version's directory, nothing is copied, and each run pod mounts the newest
version's directory read-only:

```yaml
volumes:
  af-db:
    strategy: shared_filesystem
    claim: fsx-reference
    path: iki
```

On `aws_ec2`, machines mount the file system themselves: name an NFS export
(`nfs`, such as EFS's) or a Lustre file system (`lustre`, such as FSx for
Lustre's, whose client the machine image needs). A small on-demand machine
(`aws.cleanup_instance_type`, `t3.small` unless set) removes retired
versions:

```yaml
volumes:
  af-db:
    strategy: shared_filesystem
    lustre: fs-0123.fsx.us-east-1.amazonaws.com@tcp:/abcdefgh
```

On Kubernetes, with `object_mount`, each version is a directory (a prefix)
of its own in a bucket that the Mountpoint for Amazon S3 CSI driver mounts
(ADR 0078). `claim` and `path` are as for a shared file system. The bucket
must be mounted with `uid=10001`, `gid=10001` and `allow-delete`. A bucket
can't be written in place, so a setup build's script fills a disk
(`storage_class`, the cluster's default unless set). Iki then copies the
disk into the version's directory. Symbolic links can't be copied, and a
build that leaves one fails, saying which.

```yaml
volumes:
  af-db:
    strategy: object_mount
    claim: s3-reference
    path: iki
```

On `aws_ec2`, name the `bucket` instead, in the template's region. The
machine image needs Mountpoint for Amazon S3 (`mount-s3`), and the template's
role must be able to read the bucket and to write and delete under `path`. A
setup build's machine fills a disk, then copies it into the bucket before it
shuts down:

```yaml
volumes:
  af-db:
    strategy: object_mount
    bucket: acme-reference
    path: iki
```

Until a version is ready, the environment's runs wait, saying why; bound to
templates in several places, they go to the first where one is ready. Each
attempt records the version it read.

A template's `price` is what one of its machines costs an hour, up or idle
(ADR 0070). Iki records what each run's machine time cost at that price,
shows it on the run and in cost reports, and estimates what a run will cost
before it's launched. All of a deployment's prices are in one currency
(`mixed-currencies`), and the build warns about a container or VM template
without one (`unpriced-template`): its runs show machine time but no cost.
Backend and client tiers have no per-run cost.

## What a command reads

When a worker leases a job, it gets the run input and writes it to
`input.json` for the action's command
([`run-input/v1`](src/iki_spec/schemas/run-input/v1.json)):

```json
{
  "apiVersion": "iki/v1",
  "kind": "RunInput",
  "run": {
    "id": "…",
    "hrid": "RUN-000123",
    "attempt": 1,
    "action": "protein.compute_properties",
    "plugin": { "name": "protein", "version": "1.2.0", "sha": "…" },
    "project": "…"
  },
  "params": { "ph": 7.4 },
  "settings": { "ncbi_email": "lab@example.com" },
  "inputs": {
    "protein": [
      {
        "id": "…",
        "hrid": "PRT-000042",
        "type": "protein.Protein",
        "schema_version": 1,
        "revision": 3,
        "values": { "sequence": "MKV…" }
      }
    ]
  },
  "files": {
    "…": {
      "name": "abl.pdb",
      "media_type": "chemical/x-pdb",
      "size": 81234,
      "sha256": "…",
      "path": "/…/inputs/…/abl.pdb"
    }
  },
  "command": ["python", "scripts/properties.py"],
  "environment": "protein.protein-tools",
  "job": { "…": "the lease, for iki-worker" }
}
```

Each input is a list, even when the input takes one entity. Values are the
ones at the revision the run was launched on, as the REST API shows them,
with references and files given as IDs. A file param's value is the
file's ID too. `files` describes every file the inputs hold and every file
param names, by ID. The worker downloads each one before the command
starts, checks its size and SHA-256, and adds its `path`. `settings` holds
the plugin's settings as the deployment set them when the run was launched.
An action with no inputs (an import) gets `"inputs": {}`.

The command runs from its plugin's directory, and finds its files through
the environment ([ADR 0046](../docs/adr/0046-how-iki-worker-runs-a-command.md)):

| Variable                        | What it is                                   |
| ------------------------------- | -------------------------------------------- |
| `IKI_INPUT`                     | `input.json`                                 |
| `IKI_OUTPUT`                    | where to write `output.json`                 |
| `IKI_OUT_DIR`                   | a directory for files it makes               |
| `TMPDIR`, `HOME`                | the job's own, removed when it ends          |
| `IKI_RUN_ID`, `IKI_RUN_ATTEMPT` | the run, and which attempt this is           |
| `IKI_SEED`                      | a seed for its random numbers (0 to 2³¹ − 1) |
| `IKI_CHECKPOINT_DIR`            | where a checkpointable action saves progress |

What it prints goes to the run's log. A line like
`::progress 0.4 Folding chain A` reports progress (a fraction, or `40%`).
It should exit 0 once it has written `output.json`; any other exit fails
the attempt. Before failing, it can say why with a line like
`::error Record 3 (pUC19) has no bases`: the last one is what the run's
page shows. On SIGTERM (a cancel, its timeout, or the machine stopping)
it has 10 seconds to stop.

A `checkpointable` action saves its progress in `IKI_CHECKPOINT_DIR`
([ADR 0069](../docs/adr/0069-checkpointable-runs-resume.md)). While the command
runs, the worker saves the directory every 10 minutes (as the deployment sets
it) if anything in it changed, and once more if the attempt ends in a way the
run is retried for: interrupted, say, or the machine stopping. A later attempt
finds the newest checkpoint in the directory before it starts, and
`IKI_RUN_ATTEMPT` says which attempt it is. So:

- on start, resume from what's in the directory, if anything is
- write each file under a temporary name, then rename it into place, so a
  save never holds half a file: a file that changes as it's saved is left for
  the next save
- on SIGTERM, save what you can within the 10 seconds, then exit

Only regular files and directories are saved, not links. A checkpoint is
deleted once a newer one is saved, and when the run ends. Batchable actions
can't be checkpointable.

The command writes `output.json`
([`run-output/v1`](src/iki_spec/schemas/run-output/v1.json)). It can only
change what its action declares:

```json
{
  "apiVersion": "iki/v1",
  "kind": "RunOutput",
  "updates": {
    "protein": [{ "entity": "…", "values": { "molecular_weight": 45123.4 } }]
  },
  "creates": [
    {
      "type": "protein.ProteinStructure",
      "values": { "method": "predicted", "structure": "…" },
      "derived_from": ["…"]
    },
    {
      "type": "dna.DNASequence",
      "values": { "name": "pUC19", "bases": "TCGCGCGTTT…" },
      "origin": {
        "source": "NCBI Nucleotide",
        "id": "L09137",
        "version": "L09137.2",
        "url": "https://www.ncbi.nlm.nih.gov/nuccore/L09137.2",
        "retrieved_at": "2026-11-11T09:30:00Z",
        "sha256": "…"
      }
    }
  ],
  "metrics": { "plddt": 91.2 },
  "messages": [{ "level": "info", "text": "Used model 3 of 5." }]
}
```

`updates` are keyed by input name and may set only the declared fields of
entities given there. `creates` may make only the declared types. Leave out
`derived_from` when the inputs the type is derived from hold one entity.
An imported entity says where it came from in `origin`: a database's record
(its `version`, an `https` `url`, when it was `retrieved_at`, and the
`sha256` of what was read), or a record in a file the run was given (`file`,
one of its file params). Iki keeps it with the entity, for whoever reads
it, and again each time an import finds the same entity. A file field takes a file the inputs already had, by ID, or one the
command made:

```json
{ "structure": { "$file": "relaxed.pdb", "media_type": "chemical/x-pdb" } }
```

`$file` is a path inside `IKI_OUT_DIR`; `name` defaults to its last part,
and `media_type` to a guess from its extension. The worker uploads it for
this run and puts the new file's ID in its place, so only files this run
uploaded can be attached. Iki writes all of it in one transaction, or none.
A message can name the input `entity` it's about. One at level `error`
fails that entity's run, which then writes nothing.

A new entity can refer to one made with it, such as a structure to its
chains ([ADR 0064](../docs/adr/0064-a-runs-new-entities-can-refer-to-one-another.md)).
Give the earlier create a `key`, unique in `creates`, and the later one's ref
field (or an item of a list of refs) `{"$create": "<key>"}`:

```json
[
  { "type": "protein.Protein", "key": "A", "values": { "sequence": "MVLSPADKTN…" } },
  {
    "type": "protein.ProteinStructure",
    "values": { "structure": { "$file": "1abc.cif" }, "chains": [{ "$create": "A" }] }
  }
]
```

It becomes the ID of the entity that create made, or the one it reused. A
create can refer only to those before it, and, in a job that runs several
runs at once, only to its own run's.

### Creates in a file

`creates` holds at most 10,000, and `output.json` at most 2.5 MB. A command
that makes more (an import of a million sequences, say) writes them to a file
in `IKI_OUT_DIR`, one create per line as an item of `creates` is (JSON
Lines), and names it instead of giving `creates`
([ADR 0056](../docs/adr/0056-bulk-imports.md)):

```json
{ "apiVersion": "iki/v1", "kind": "RunOutput", "creates_file": { "$file": "creates.ndjson" } }
```

The worker uploads it with the run's other files. Iki then checks every line,
a chunk at a time, going on from where it was if it's interrupted, and writes
them all in one transaction, or none: nobody sees half an import. A line's
problems point at `#/creates_file/<line>`, counting from 1. Lines can't
attach files or refer to one another (give those in `creates`), and a job that runs several runs at
once can't give a creates file. Webhooks hear of an import once, as
`entity.imported`, not `entity.created` for each entity. A file holds at most
100 million lines, each at most 256 MiB.

### Batches and packing

Launching an action on several entities at once makes a batch
(`BAT-000123`) of runs, one for each
([ADR 0055](../docs/adr/0055-batches-and-packing.md)). An action that's
`batchable: N` runs up to N of a batch's runs in one job: its one input
lists all their entities, and `pack` says which is whose:

```json
"pack": [
  { "run": "…", "hrid": "RUN-000123", "entity": "…" },
  { "run": "…", "hrid": "RUN-000124", "entity": "…" }
]
```

Iki splits the output back into each run's own: updates by their `entity`,
new entities by their `derived_from` (which a packed job must give), and
messages by their `entity` (one without goes to every run). Metrics go to
every run. Each run's part is then checked and written on its own, so one
entity's bad result fails only its run. To fail one entity and go on with
the rest, report an `error` message about it.

### Seeds, reuse and Reproduce exactly

Every run gets a seed, as `IKI_SEED` and `run.seed` in `input.json`
([ADR 0057](../docs/adr/0057-reuse-reproduce-exactly-and-seeds.md)). A
command that uses random numbers should seed them from it: a run reproduced
exactly gets the same seed again. A packed job's command gets its first
run's.

An action that's `deterministic: true` promises that its results depend only
on its plugin's commit, its params, its plugin's settings and its inputs at
the revisions it reads. Its runs can then be reused: launching one identical
to a run that succeeded, which the launcher can read, ends at once with that
run's results (`reused_from`), and runs nothing. Launch with `reuse: false`
to run it anyway. Don't declare it for an action that fetches what may
change, or whose results depend on the time or the machine.

`POST /runs/{ref}/reproduce` runs any run again exactly: the same commit,
params, settings, seed and input revisions. It writes nothing: its
`reproduction` says whether its results (creates, updates, the creates file
and metrics; files by their content) and environment build match the
original's, and where they differ.

## YAML

Manifests are read as YAML 1.2: only `true` and `false` are booleans, so
`no`, `on` and `yes` stay text, and `010` is ten. Duplicate keys, aliases
(`*name`), merge keys (`<<`), tags (`!!python/…`) and files over 1 MiB are
refused, so what is reviewed is what Iki reads.

## Problem codes

Every problem has a stable `code`, shown in brackets and in `--format json`:

| Code                                                                                                                                                                                                                                                                                                                                                          | What's wrong                    |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- |
| `yaml-syntax`, `yaml-duplicate-key`, `yaml-alias`, `yaml-merge-key`, `yaml-tag`, `yaml-key`                                                                                                                                                                                                                                                                   | The YAML itself                 |
| `file-too-large`, `not-utf8`, `unreadable-file`, `symlink`, `not-a-mapping`                                                                                                                                                                                                                                                                                   | The file                        |
| `missing-plugin-manifest`, `duplicate-plugin-manifest`, `wrong-file`, `not-a-directory`                                                                                                                                                                                                                                                                       | Where manifests are             |
| `missing-api-version`, `unsupported-api-version`, `missing-kind`, `unknown-kind`                                                                                                                                                                                                                                                                              | A manifest's header             |
| `schema`                                                                                                                                                                                                                                                                                                                                                      | Anything the JSON Schemas catch |
| `reserved-name`, `name-too-long`, `duplicate-table`, `column-conflict`                                                                                                                                                                                                                                                                                        | Names                           |
| `duplicate-type`, `unknown-type`, `extends-cycle`, `field-shadows-parent`                                                                                                                                                                                                                                                                                     | Types                           |
| `missing-hrid`, `hrid-on-abstract`, `duplicate-hrid-prefix`                                                                                                                                                                                                                                                                                                   | HRIDs                           |
| `unknown-field`, `identity-type`, `identity-not-fixed`, `identity-removed`, `on-duplicate-without-identity`                                                                                                                                                                                                                                                   | Identities                      |
| `computed-with-default`, `computed-required`, `invalid-default`, `min-above-max`, `invalid-pattern`                                                                                                                                                                                                                                                           | Field options                   |
| `unsupported-index`, `index-needs-max-length`                                                                                                                                                                                                                                                                                                                 | Indexes                         |
| `unknown-unit`, `display-unit-mismatch`                                                                                                                                                                                                                                                                                                                       | Units                           |
| `file-too-large`, `invalid-json-schema`, `display-title`                                                                                                                                                                                                                                                                                                      | Files, JSON fields and titles   |
| `extends-builtin`, `extension-of-own-type`, `duplicate-extension`, `extension-field-conflict`, `extension-field-required`                                                                                                                                                                                                                                     | Extensions                      |
| `undeclared-dependency`, `dependency-not-available`, `dependency-invalid`, `dependency-is-self`, `dependency-cycle`, `dependency-version`, `dependency-unpinned` (warning)                                                                                                                                                                                    | Dependencies                    |
| `required-with-default`, `setting-looks-secret` (warning), `unknown-setting`, `missing-setting`, `invalid-setting`, `settings-file`                                                                                                                                                                                                                           | Settings                        |
| `missing-lockfile`, `not-a-lockfile`, `lockfile-documents`, `duplicate-plugin`, `core-is-builtin`, `missing-source`, `mixed-source`, `missing-sha`, `path-outside`, `absolute-path`, `missing-settings-file`                                                                                                                                                  | Lockfiles                       |
| `duplicate-action`, `duplicate-name`, `param-looks-secret` (warning), `max-needs-many`, `input-schema-unsatisfied`, `batch-needs-one-input`, `batch-keeps-artifacts`, `batch-checkpoints`, `timeout-too-long`                                                                                                                                                 | Actions                         |
| `creates-abstract`, `unknown-input`, `duplicate-updates`, `updates-immutable`, `action-does-nothing`                                                                                                                                                                                                                                                          | What actions write              |
| `unknown-environment`, `tier-not-supported`, `lifecycle-wrong-tier`, `gpu-wrong-tier`, `client-resources`, `client-secrets`, `client-network`, `reserved-secret`, `absolute-path-on-backend`, `absolute-path-on-client`, `missing-file`                                                                                                                       | Where actions run               |
| `deterministic-with-network`, `retries-deterministic-failure` (warnings)                                                                                                                                                                                                                                                                                      | Reuse and retries               |
| `workflow-unknown-action`, `workflow-unknown-input`, `workflow-unknown-param`, `workflow-input-missing`, `workflow-param-missing`, `workflow-unknown-reference`, `workflow-unknown-step`, `workflow-wrong-type`, `workflow-many-into-one`, `workflow-invalid-param`, `workflow-cycle`, `duplicate-workflow`                                                   | Workflows                       |
| `duplicate-environment`, `environment-needs-image`, `environment-needs-packages`, `unused-packages`, `unused-image` and `unused-environment` (warnings)                                                                                                                                                                                                       | Environments                    |
| `requirements-unpinned`, `requirements-include`, `conda-not-explicit`, `conda-unpinned`                                                                                                                                                                                                                                                                       | Pinned packages                 |
| `volumes-wrong-tier`, `overlapping-mounts`, `volume-needs-setup`, `setup-fills-nothing` (warning)                                                                                                                                                                                                                                                             | Volumes and setup               |
| `duplicate-template`, `duplicate-binding`, `unknown-kind`, `wrong-provisioner`, `gpu-wrong-tier`, `limit-needs-persistent`, `fallback-without-spot`, `root-disk-too-small`, `volume-strategy-unsupported`, `fast-restore-wrong-strategy`, `volume-option-kubernetes-only`, `snapshot-class-wrong-strategy`, `mixed-currencies`; `unpriced-template` (warning) | Compute templates               |
| `unknown-environment`, `unknown-template`, `template-wrong-tier`, `template-too-small`, `action-does-not-fit`, `volume-strategy-missing`, `volume-strategy-access`; `lifecycle-unbound`, `unbound-environment` and `unused-template` (warnings)                                                                                                               | Bindings                        |
