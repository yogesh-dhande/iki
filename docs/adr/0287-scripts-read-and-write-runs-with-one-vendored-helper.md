# 0287. Scripts read and write runs with one vendored helper

- Status: Accepted
- Date: 2026-11-27
- Extends: [0046](0046-how-iki-worker-runs-a-command.md),
  [0093](0093-plugin-action-tests-run-fixtures-through-the-worker.md)
- Extended by: [0288](0288-plugins-share-readers-from-the-runtime-each-release-ships.md), the readers it leaves out come in a
  runtime each release ships

## Context

An action's command reads `input.json` from `IKI_INPUT` and writes
`output.json` to `IKI_OUTPUT` (ADR 0046). It says why it failed in a
`::error` line, and its output must stay within what the worker reads:
2.5 MB, 100 messages and 100 metrics. The 35 first-party and example
plugins with scripts, and the plugin template, did this by hand:

- Each script parsed and wrote the JSON itself. A few checked the output's
  size; most didn't. Some refused NaN, which JSON has no number for, and
  others would have written it. Two printed their `::error` to stderr.
  Several capped their messages by cutting the list, errors and all.
- Eight defined their own `Refused`, `Stop` or `RefusedError`, and four
  their own `run_input()` and `write_output()`, copied from plugin to
  plugin.
- Five named a registered input as `entry_id or id`, and a sixth as
  `entry_id` defaulting to `id`, which gives `None` for an unregistered
  record.
- Four compared an entity's type by its name (`part["type"] ==
"iki/protein.Protein"`). A lab's own subtype, which launch accepts
  (ADR 0042), fails that comparison.

A plugin author outside the repository copied whichever script they started
from. A library on PyPI would fix the copying, but it doesn't fit how actions
run. A command runs in its environment's image, and many of those are images
Iki doesn't build: a lab's own, or a vendor's under the lab's licence, such as
CRISPResso2's or Cell Ranger's. A run can't install a package into them. So
the helper has to travel with the plugin.

GitHub Actions does the same for its toolkit: `@actions/core` is bundled into
each action rather than installed on the runner. Bazel and Go vendor what a
build must not fetch, and they check the copy by its digest.

## Decision

**`iki_action.py` is one file, vendored.** Iki's action helper is a single
module of Python's standard library, for Python 3.8 or later, published with
`iki-spec`. A plugin keeps an unchanged copy at `scripts/iki_action.py`, and a
script imports it as `import iki_action as iki`. It provides:

- `iki.main(fn)`, or `iki.main({"name": fn, …})` to choose by the command's
  first argument. It reads the run, calls `fn(run)` and writes what it returns:
  an `Output`, run output's fields as a dict, or nothing.
- `Run` and `Entity`. They are still the JSON's dicts, with names beside them:
  `run.input(name)` (exactly one, or the run is refused), `run.inputs`,
  `run.params`, `run.settings`, `run.file(id)`, `run.out_dir`, and
  `entity.hrid` and `entity.revision`. No name hides a dict's own method:
  `entity["values"]` stays the JSON's, and `entity.values()` stays the
  dict's.
- `Output`: `update`, `create` (which qualifies a type of the plugin's own),
  `info`, `warning`, `error` and `metric`.
- `iki.Refused`, whose reason becomes the run's `::error` line. `refuse=`
  names other exception classes to treat the same way, such as a parser's
  `ValueError`.
- `iki.is_a(entity, type)`, which reads the entity's `types` lineage that the
  run input carries, and `iki.ref(entity)`, which names a registered input by
  its entry.
- `iki.progress` and `iki.fail`.

The helper enforces the worker's limits in one place. It refuses output over
2.5 MB, and numbers that JSON can't hold. It refuses a run input of another
version. It keeps every error message and then up to 100 messages in all,
saying how many more there were. It writes output in compact form.

**Its copies are checked by digest.** Every version Iki publishes is listed
with its SHA-256. `iki plugin validate` refuses any `iki_action.py` in a
plugin that isn't a published version (`action-helper-changed`). It warns of
an earlier version (`action-helper-outdated`), which still runs. A change to
the helper is a new version; old versions stay valid, so a plugin pinned at an
older Iki keeps validating. `iki plugin vendor [--check]` writes or checks the
CLI's own version. A lab can trust a copy without reading it, and an edited
copy can't pass as Iki's.

**Every script uses it.** Every first-party and example script reads and
writes runs with the helper, and the plugin template and the `iki-write-plugin`
skill teach it. A first-party test fails if a script reads `IKI_INPUT`,
writes `IKI_OUTPUT`, or compares an entity's type by name, or if a plugin's
copy isn't this Iki's helper. Tests load the helper from `iki-spec`'s source,
which every copy matches byte for byte.

**An action test reads absent as empty.** Run output leaves out what's empty,
so an expected `updates: {sample: []}` now passes for output with no updates.
It still fails for an output that has some.

## Consequences

- A plugin author gets the contract's details right by default. A new limit
  or field changes one file and its published version, not 59 scripts.
- Scripts are shorter: the I/O, error and size handling at the end of each one
  is now one line.
- A test that loads a script by its path, outside Iki's own tests, puts the
  script's directory on `sys.path` first, as the worker's `python
scripts/x.py` does. `python -I` leaves that directory out, so an image's
  self-check runs a script with `-E -s` instead.
- A type check that a lab's subtypes must pass has one obvious spelling, and
  a test catches the other one.
- Each plugin carries a copy that `validate` checks, rather than a dependency
  that every image must install.
- A script's own exception is still its own. Only those passed to `refuse=`,
  or raised as `iki.Refused`, become the run's error. Anything else is a bug
  and fails with its traceback, as before.
- Shared readers and data, such as FASTQ, AIRR, gzip-detection, atomic weights
  and HELM, aren't in the helper: it stays small enough to vendor and read.
  They are ADR 0288's.
