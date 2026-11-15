# 0095. A plugin says which Iki it works with, and versions its changes

- Status: Accepted
- Date: 2026-11-15
- Refines: [0094](0094-a-plugins-upgrade-is-tested-from-an-earlier-commit.md)

## Context

Plugins are made apart from Iki, and Iki will change under them: a new
manifest key, a run-input field, a page a component may use. A deployment
that upgrades Iki and keeps a plugin made for an older release finds out at
the first run that uses what changed, if anyone notices then. Package
managers precedent is for a package to say which host it works with
(`engines` in npm, `Requires-Python`, `requires.vscode` for VS Code
extensions), checked when it's installed.

Plugins also depend on each other's types by schema version (`types: {
DNASequence: ">=2,<4" }`). That only works if an author increases a type's
`schema_version` when its fields change. Nothing checked that they did. A
plugin's own `version` is for people, so two builds with different fields
and the same version can't be told apart in a deployment's history or a
validation record.

## Decision

**`iki:` in plugin.yaml says which Iki releases the plugin works with**:
comparisons with release numbers, separated by commas, such as
`iki: ">=0.1,<0.3"`. `iki plugin validate`, `build` and `deploy` check it,
and a plugin that doesn't fit is an error (`iki-version`) with both
versions: "dna works with Iki >=0.2, and this is Iki 0.1.0." The build
keeps it in the plugin's manifest, and the deployment's migrate Job checks
it again against the server's own release, before it migrates anything: an
image built by a newer CLI can't put a plugin on an Iki it wasn't made for.
The plugin's page in Iki shows the range.

A build of Iki counts as its release: `0.2.0.dev3` is `0.2.0` here, so a
plugin made against what's coming loads on the builds before it. `==0.2`
means `0.2.0`, as in PEP 440. The key is optional, and leaving it out
says nothing either way; the template has it, so new plugins do.

**The upgrade test checks the versions** (ADR 0094). Before it makes the
scratch database, it compares each type at the earlier commit with the
same type now:

- if what its entities may hold changed, and its `schema_version` didn't
  increase, the test fails, naming each change;
- if its `schema_version` went down, the test fails.

"What its entities may hold" is each field's type and rules: whether it's
required, unique, immutable or computed, its default, format, pattern,
bounds, values, unit, media types and size, what it links to and its
items. Also the type's `extends`, `abstract` and `identity`. A field's
label, description, display unit, link and index change only how it's
shown or found, and don't count. A field added, removed or renamed does.

If the plugin's migrations changed and its `version` didn't, the test says
so, and passes: a pull request can leave the release number to whoever
releases it.

## Consequences

- A deployment can't load a plugin onto an Iki it wasn't made for, and
  is told why.
- A dependency's schema-version constraint means what it says, for
  plugins whose CI runs the upgrade test, which the template's does.
- The Iki range is the author's word. Iki doesn't check a plugin really
  works across it; its action and upgrade tests, run with each Iki
  release, are how an author finds out.
