# 0270. A lab curates its own lists

- Status: Accepted
- Date: 2026-11-26
- Refines: [0115](0115-registries-keep-identities-projects-keep-records.md),
  [0143](0143-a-registry-fact-can-be-fixed.md)

## Context

Many fields hold a word from a vocabulary: how a lot was made, which
purification a prep had, a deviation's category, a sample's matrix. A plugin
declares such a field as an `enum`, so its values are the plugin's: a lab
that purifies by a method the plugin didn't think of can't say so. Plugins
have worked around it with a pair of fields, an enum with `other` and a typed
name beside it (#306). That gives every lab a second, free-text vocabulary
nobody curates: "Ni-NTA", "NiNTA" and "IMAC (Ni)" end up as three methods,
and a search or a trend by method misses two of them.

LIMS and ELNs settle this with lists the lab keeps itself. Benchling's
Dropdowns are named lists of options an administrator edits; a field refers
to the dropdown, archived options stay on the records that have them, and a
record's dropdown offers only active ones. LabWare's list tables (`LIST`
and `LIST_ENTRY`) do the same, each entry with a stable name and a
description; changing one is audited. Under 21 CFR Part 11 and EU GMP
Annex 11 a change to such a list is a change to what records mean: it needs
who, when and why, and a value already on a record must keep meaning what
it meant.

Not every enum is vocabulary. Some values drive a workflow: a state, a
result that a release guard reads (`result == "pass"`), a unit a calculation
depends on. A lab renaming or removing one of those would break the plugin's
rules, so those stay enums.

## Decision

**A list is an entity type marked as a list.** `list: true`, or `list:` with
starting options, makes a type a lab list. Its options are registry entries
(ADR 0115), and a field that uses it is an ordinary reference,
`{ type: ref, to: PurificationMethod }`, which forms show as a dropdown.
Every option has four fields of its own, which the marker adds:

- `code`: its identity, unique in its list in a registry, which never
  changes, such as `protein_a`. Exports give it, so a relabel doesn't change
  what a download says. A wrong code is superseded by the right one
  (ADR 0115, ADR 0143); it isn't edited.
- `label`: what people see and pick, unique in its list in any case.
- `description`.
- `position`: its place in the list's order.

The label, description and position are registry facts. A curator changes
them with a required reason, and each change is a revision of the option and
an audit event. A list's type may declare other fields, which are its
options' facts too, such as a method's SOP; a reference among them names
another list, never a record. Plugins that extend a list type add facts
only.

A list has no records, rules, states, identity of its own choosing,
subtypes or extensions that add record fields: it isn't a record type, and
the validator refuses those keys on it. A reference to a list names that
list alone.

**Options belong to the registry.** Every project in a registry reads its
lists' options, and picks from them; another registry's projects see its
own. Nothing about an option is any project's, so reading one reveals
nothing of another project's work. Who uses an option is never shown: its
curators see how many records name it, in every project, and not which.

**Registrars and list curators curate; members pick.** A registry's
registrars curate every list in it. Org Admins appoint a list's curators,
person by person, for that list: a purification lead for purification
methods, a QC lead for deviation categories. An Org Admin isn't one by
being an Org Admin, and a project's members only pick. Curators add options,
relabel them, change their other facts, reorder the list, archive and
restore options, and supersede a wrong code. Tokens need the `admin` scope
for it; agents read lists but don't change them (there's no MCP tool for it).

The role is registry-scoped, as a registrar is, because a list is: its
options are every project's in the registry, so a project's roles,
including lab roles (ADR 0116), which are a project's, can't say who
changes it. A project Admin curating a shared vocabulary would change other
projects' records' meaning, which isn't theirs to change. It's a role
granted a list at a time, rather than registrar alone, because the people
who own a lab's vocabulary aren't its registrars: a registrar curates
identities across the registry, while a purification lead knows the
purification methods. And Org Admins, who manage access, don't curate the
lab's science by their role, as they read no project's data by it
(ADR 0003).

**Forms offer the active options, in order; records keep theirs.** A
dropdown offers the list's active options in its order, and lets you search
it once it's long. A list may have 10,000 options, so a search shows its
first fifty matches and says how many there are, and the list's page shows
its options a hundred at a time, finds one by its label or code, and moves
one to a position in the order by keyboard. An archived option, or a superseded one, isn't offered,
and a new value can't name it; a record that names it already keeps it
through any other edit, and it's still in search, filters, exports and the
API, marked as such. A superseded option's referring projects' Admins are
told, and each project's Editors move their own records to the replacement:
a curator can't rewrite records in projects they can't read.

**A plugin ships starting options.** `list: { options: [...] }` gives each
option's code, label and description and its other facts, a reference by
the code of the other list's option. Installing the plugin seeds every
registry with them, and a registry made later is seeded as it's made. An
upgrade adds the starting options a registry hasn't got, by code, at the end
of the list. It never changes what a lab did: an option the lab relabelled,
reordered or archived stays so, and one whose label the lab gave another
option already isn't added. Each seeded option's first revision is the
system's, and says which plugin version it came from, as the deploy's other
work on a plugin's data is; the audit trail has the people's changes.

**Values that drive a workflow stay enums.** A list is for descriptive
vocabulary: what a person reads and a search groups by. Values a state,
guard, rule, calculation or worker reads are the plugin's, as an enum.

**Moving data says which option it means.** A bundle carries the options its
records name as entries; the importing registry's list must have each code,
or the plan has a conflict saying which option its curators need to add.
A spreadsheet import takes an option's code or its label, in any case.

## Consequences

- A lab says what it does in its own words, once, and every project uses
  the same words: searches and trends by a list's value find every record.
- Every change to what an option says has who, when and why, and the
  records that name it keep meaning what they meant.
- A plugin's enums need sorting: the ones that are vocabulary become lists,
  and the "Other + typed name" pairs go.
- A list's options are a registry's, so two registries' "Protein A" are two
  options; a bundle maps them by code.
- Seeding writes rows at deploy and when a registry is made, so a new
  registry starts with the plugins' lists, not empty.

## Alternatives considered

- **Enum values a lab can add to, in settings.** It keeps the enum's value
  as the stored data, so a relabel would rewrite records or leave a value
  with two meanings, and there'd be nowhere for an option's history,
  description or other facts.
- **Lists per project.** Two projects would each keep "Protein A", and a
  search across projects would split them. Labs curate one vocabulary.
- **Free text with suggestions.** It's what the "Other" pairs do already:
  every spelling is a value.
- **Lab roles for curators.** Lab roles are a project's (ADR 0116), and a
  list is the registry's.
- **Webhooks for list changes.** The audit trail and each option's history
  are the record of them; a webhook can follow when an integration needs
  one.
