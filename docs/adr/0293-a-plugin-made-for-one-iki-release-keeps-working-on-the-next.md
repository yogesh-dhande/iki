# 0293. A plugin made for one Iki release keeps working on the next

- Status: Accepted
- Date: 2026-11-27
- Amends: [0016](0016-versioned-specs-compatibility-and-lts.md) (adds a
  provisional tier, and says what of its upgrade path is built),
  [0230](0230-plugins-write-export-formats-in-a-wasm-sandbox.md) (a range
  of writer ABIs)
- Supersedes in part: [0095](0095-a-plugin-says-which-iki-it-works-with-and-versions-its-changes.md)
  (`iki:` is required)
- Refines: [0033](0033-plugin-codegen-and-migrations.md) (HRID prefixes
  per deployment)

## Context

At M1 the plugin manifest becomes a public contract. Labs write plugins
Iki's developers never see, keep them in repositories Iki can't edit, and
upgrade on their own schedule (ADR 0016). After M1, each of these would
break a lab's plugin, or mislead its author, on an ordinary Iki release:

- **A key from a newer Iki.** A plugin that uses a key added in 0.3 and
  leaves out `iki:` fails on 0.2 with "`review` isn't allowed here. Did
  you mean `refines`?", when what's wrong is the release. ADR 0095 made
  `iki:` optional, by the precedent of npm's `engines`; that precedent is
  for packages whose host rarely changes their contract, and Iki's does at
  every minor release.
- **A deprecation.** ADR 0016 promises one minor release of warning before
  a key goes, and codemods in `iki plugin upgrade`. Neither exists: no
  schema property is marked deprecated, the validator has no warning for
  one, and there is no `upgrade` command. The REST API sends no
  `Deprecation` or `Sunset` header either.
- **Everything freezes at once.** Some blocks have one consumer, and
  their shape hasn't met a second one: `stock_catalogue` (reagents),
  `work_item` (requests), `draw_purposes` (lots). At M1 they'd be as
  fixed as `fields`.
- **No room for a lab's own metadata.** Every kind refuses unknown keys,
  `x-` ones included, so an owner, a GxP classification or a validation
  status ends up in `description`.
- **HRID prefixes are one global space.** Iki's own plugins use 118 of
  them, most three letters. A release that adds a first-party type with
  `BUF` refuses to build in a deployment where a lab's plugin has `BUF`,
  and HRID sequences are never dropped, so the lab can't rename its way
  out.
- **One writer ABI.** The host refuses any writer whose `iki_abi()` isn't
  exactly 1, and ADR 0230 makes any new request field ABI 2. The first
  such field would stop every lab's writer.

Precedent: Kubernetes keeps alpha and beta fields apart from GA ones, keeps
`annotations` on every object and ignores them, and marks deprecated fields
in its OpenAPI; OpenAPI allows `x-` extensions on every object; JSON Schema
has `deprecated`; RFC 9745 and RFC 8594 give HTTP `Deprecation` and
`Sunset`; Benchling lets a tenant's admin set each schema's registry ID
prefix.

## Decision

**`iki:` is required.** Every `plugin.yaml` says which Iki releases it
works with. The template has always set it. Leaving it out is an error
(`missing-iki`). When a plugin's range excludes this Iki, its unknown keys
are reported with that: "this plugin is made for Iki >=0.3, and this is
0.2; it may use keys 0.2 doesn't have", not as typos.

**Each schema property says when it came.** A property added after the
first release carries `x-iki-since: "0.3"`; one without it has been there
since its kind was. The validator checks every key a manifest uses against
the plugin's `iki:`: a range that admits a release before the key's is an
error (`iki-range`), "`review` needs Iki 0.3 or later, and `iki:` admits
0.2: raise it to `>=0.3`". So a plugin can't claim a release it can't load
on.

**Deprecation is marked, warned of and dated.** A deprecated property has
JSON Schema's `deprecated: true`, the release it was deprecated in
(`x-iki-deprecated-since`) and what to write instead (`x-iki-use`).
Using it is a warning (`deprecated-key`) that names both; the key keeps
working for one minor release (ADR 0016), then goes. Editors that read the
published schemas strike it through. A test refuses a deprecated property
without its release and replacement. A REST operation that's deprecated
says so in the OpenAPI document and sends `Deprecation` (the release it
was deprecated in, as RFC 9745's date) and `Sunset` (when it goes).

**Provisional keys may change in a minor release.** A property marked
`x-iki-provisional: true` isn't frozen at M1: a minor release may change
its shape, with a codemod and a release note, and without a deprecation
window. The first ones are those with one consumer: an entity type's
`stock_catalogue`, `work_item` and `draw_purposes`. A lab plugin that
uses one is warned (`provisional-key`); Iki's own plugins, which are
released with Iki, aren't. A key leaves the tier when a second consumer
has used it; a GA key never goes back. `draw_target`, which three plugins
use, isn't provisional.

**What of ADR 0016's upgrade path is built.** The metadata it needs is:
`x-iki-since`, `deprecated` with its release and replacement, and
`x-iki-provisional`, in every published schema, and the validator's checks
of them. The codemod runner, `iki plugin upgrade` (roadmap 46c), isn't
built yet. No key may be deprecated, and no provisional key changed,
until it is: the first such change ships the runner and its codemod
together. The validator's hint for an older `apiVersion` no longer names a
command that doesn't exist.

**Every kind takes `annotations:` and `x-` keys.** `annotations` is a map
of names (`acme.com/owner`, `gxp_classification`) to text, on the top
level of every kind a person writes. Iki keeps it in the compiled
manifest, shows a plugin's and an entity type's on their pages, returns
them in the API, and never reads them. A key starting `x-` is allowed
beside them, at the top level of every kind, for tools: the validator
ignores it and the build drops it. Neither changes what a plugin does, so
neither counts as a schema change (ADR 0095). They're allowed at the top
level only, as Kubernetes keeps annotations in an object's metadata:
that's where a lab's metadata about a manifest goes, and allowing them
deeper later is additive.

**A deployment may give a type its own HRID prefix.** A plugin's prefix
is a default. The deployment's `plugins.lock.yaml` may map types to
prefixes, `hrids: { iki/reagents.Buffer: IBUF }`, which the build uses in
place of the plugin's. The build's refusal of two plugins with one prefix
names both types and shows the override that settles it. An override
applies only to a type with no entities under another prefix: HRIDs are
printed on labels, so a type's records keep one prefix. Iki publishes the
prefixes its own plugins use, and those it reserves for them
(`iki_spec/reserved_hrid_prefixes.json`); a lab plugin that takes one is
warned (`reserved-hrid-prefix`). A lab list needs no prefix (ADR 0295).

**The writer host accepts a range of ABIs.** The host keeps an encoder
for each ABI from the oldest its LTS line supports to the newest, and
builds each request for the ABI the module says it reads, so a module
keeps getting the bytes it was written for. A request field is still a
new ABI (each ABI's request for a revision never changes, as ADR 0230
needs for exports to be reproducible); what changes is that an older
module keeps working. The `iki-writer` crate is published per release, and
a plugin's writer pins it in its `Cargo.lock` with a checksum; the writer
builder image is pinned on its own, so an Iki release doesn't change a
writer's digest unless the plugin moves its pins. When a writer's digest
isn't the reviewed one, the install says which input differs, the source
or the SDK and builder it was built with.

## Consequences

- A plugin made for 0.3 is refused on 0.2 by its release, with both
  numbers, and a plugin that claims 0.2 while using a 0.3 key is caught
  when it's validated, not on a lab's deployment.
- A deprecation has a warning, a date and a replacement, in the
  validator, the editor and the HTTP headers.
- Single-consumer shapes can still be fixed after M1; labs that use them
  are told so.
- Labs record their own metadata where Iki keeps it, rather than in
  descriptions.
- A first-party release can't strand a lab on a prefix: the build says
  which override to add.
- New writer request fields no longer stop old writers. The host carries
  one encoder per supported ABI.
- Negative: until `iki plugin upgrade` exists, nothing can be deprecated
  and no provisional key can change. That's deliberate: the first change
  pays for the runner.
- Negative: `iki:` is one more line authors must keep right; the
  `iki-range` check keeps it honest.

## Alternatives considered

- **Keep `iki:` optional and improve the unknown-key message.** An older
  Iki can't know a key a newer one added, so without the range it can't
  say the problem is the release.
- **Only `x-` keys, or only `annotations`.** `x-` keys are for tools and
  never shown, as in OpenAPI; annotations are for people and shown, as in
  Kubernetes. One for both would show tool noise to people, or hide a
  lab's metadata.
- **Namespaced HRID prefixes (`acme:BUF`).** Prefixes are printed on
  labels and read aloud; a namespace makes every label longer for a
  collision a deployment can settle once.
- **One exact writer ABI, bumped rarely.** Every additive field would
  wait for a breaking release, or break every writer.
