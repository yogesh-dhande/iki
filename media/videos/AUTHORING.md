# Making a video

A video is a script. You pick a layout for each scene, fill its slots with what Iki
really shows, and write the lines. The design system decides how things look, where they
go, how they move and how long they stay. Two authors, or two models, writing the same
script get the same video.

The steps, in order:

1. [Propose the concept](#1-propose-the-concept).
2. [Check every fact against the product](#2-check-every-fact).
3. [Write the script](#3-write-the-script).
4. [Render it and review the frames](#4-render-and-review).
5. [Hand it to a reviewer](#the-reviewers-checklist).

## 1. Propose the concept

Write a short concept before any script, and get it approved. It has:

- **The title**, as "Concept: its promise". For example, "Entities: one record, its whole
  history".
- **A target length.** Most concepts take 60 to 90 seconds.
- **Five to eight scenes.** For each: what the viewer sees, and the one line it says.
- **The last line**, which names the next video in the series.

A scene teaches one idea. If a scene needs two captions to make two points, it's two
scenes.

## 2. Check every fact

**Everything on screen must be true of Iki today.** Every name, label, message, HRID,
field, number and relationship. Check each one against the product before you draw it:

- **The UI's own words:** messages and labels are in `web/app/src`. Quote them exactly,
  with their typographic apostrophes (’) and quotes (“ ”).
- **Types and their fields:** `plugins/<plugin>/types/*.yaml`. Field labels, enum values,
  HRID prefixes, states, and which states need signatures.
- **Decisions:** `docs/adr/`. When a line rests on one, name the ADR in a comment above
  the scene, so whoever updates the video later can check it again.
- **Science:** real sequences and values, from the repo's tests and seeds, or a primary
  source such as KEGG, UniProt or the PDB. Trastuzumab's chains are in
  `plugins/antibody/tests/assemble.yaml`.

If the concept says something the product doesn't do, change the line, and tell whoever
approved the concept what you changed and why. Don't round the product up. "Nothing is
lost" isn't true if a plugin can be purged. The History tab's own line is: "Revisions can’t
be changed or removed, by anyone."

Invented examples are fine where the product would make them: people's names, project
names, dates, HRID numbers. Keep them plausible, and the same across a video.

## 3. Write the script

Make `src/<video>/script.yaml`. Copy `scene.tsx`, `project.ts` and an empty `custom.ts`
from `src/entities/`, changing the script's path in `scene.tsx`. Your editor checks the
YAML as you type, through the schema named on its first line.

```yaml
# yaml-language-server: $schema=../../script.schema.json
id: actions
title: "Actions: what Iki does to entities"
series: Iki concepts
target: 75 # seconds; the check notes it when the video runs 15% over or under
scenes:
  - id: title
    layout: title
    slots: { eyebrow: Iki concepts, title: Actions, subtitle: What Iki does to entities }
    beats:
      - id: title
        say: "Actions: what Iki does to entities."
        do:
          - together:
              - show: title
                as: rise
              - show: subtitle
                as: rise
  - id: inputs
    layout: graph
    slots:
      nodes:
        - { id: antibody, kind: Antibody, title: Trastuzumab, hrid: ABY-000001, focus: true }
        - { id: run, kind: Run, title: Predict structure with ImmuneBuilder, hrid: RUN-000007 }
      edges:
        - { from: antibody, to: run, label: input }
    beats:
      - id: inputs-run
        caption: An action takes entities in, and a run records what it did.
        do:
          - show: edge:antibody>run
            as: draw
          - show: node:run
            as: rise
```

### Scenes and beats

- **A scene** is one layout, filled through its slots. It arrives by a `transition`:
  `fade` (the default), `slide` or `cut`.
- **A beat** is one caption line and what happens while it's up. Its `id` must stay the
  same through edits: the voice-over's audio is filed under it.
- **`do`** lists steps in order. Each starts when the one before it finishes. Steps under
  `together` start at once, each a short stagger after the last.
- **A step** acts on a named part of the scene:
  - `show`, with an entrance `as`: `fade`, `rise`, `pop` or `draw`;
  - `hide`, with an exit `as`: `fade` or `sink`;
  - `emphasize`: the part grows a little and settles back;
  - `dim`: it fades back, there but not the point.

  A part a step shows starts hidden. Every other part is there when the scene arrives.

- **`hold`** adds time after a line: `short`, `medium` or `long`. There are no seconds
  anywhere in a script. Timing comes from the words and the theme.

### Which layout to use

| Layout         | Use it for                                                                           | Slots                                                                                            | Parts                                                                                   |
| -------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------- |
| `title`        | The first scene of every video.                                                      | `eyebrow`, `title`, `subtitle`                                                                   | `brand`, `eyebrow`, `title`, `subtitle`                                                 |
| `focus`        | One thing, on its own: a record, a message, a file.                                  | `heading`, `object` (a `record`, `notice` or `fragment`)                                         | `heading`, `object`, `field:<label>`                                                    |
| `gallery`      | Several of the same kind of thing: copies, versions, examples (2 to 6).              | `heading`, `items`                                                                               | `heading`, `item:<n>` (from 1), `item:<n>.field:<label>`                                |
| `split`        | Two things compared: new and existing, before and after.                             | `heading`, `left` and `right` (each a `label` and `content`), `link` (the arrow's label), `note` | `heading`, `left`, `right`, `left.field:<label>`, `right.field:<label>`, `link`, `note` |
| `record-inset` | A record, and the plugin that defines its type.                                      | `record`, `inset` (a plugin), `link`                                                             | `record`, `field:<label>`, `inset`, `link`                                              |
| `timeline`     | A record's history: revisions, oldest first (2 to 5).                                | `heading`, `subject`, `revisions`                                                                | `heading`, `subject`, `axis`, `revision:<n>`, `signature:<n>`                           |
| `graph`        | How things connect: lineage, entries and records, inputs and results (2 to 6 nodes). | `heading`, `direction` (`right` or `down`), `nodes`, `edges`                                     | `heading`, `node:<id>`, `signature:<id>`, `edge:<from>><to>`                            |
| `closing`      | The last scene of every video: the takeaway, and what's next.                        | `statement`, `next`                                                                              | `statement`, `next`, `brand`                                                            |

The layouts place everything: inside the title-safe area, above the caption band, at the
theme's sizes. You never give a position or a size. If none of them can show what you need,
see [custom code](#when-the-layouts-arent-enough).

The content a slot takes, in the app's own terms:

- **`record`:** an entity as its page shows it: `type` (its label, such as "Antibody"),
  `hrid`, `name` and up to 6 `fields`. Each field has a `label`, a `value`, a `kind`
  (`text`, `sequence` or `id`) and an optional quieter `detail`. Leave out `hrid` for a
  draft that isn't registered yet: it's drawn dashed, marked "New". Sequences are cut to
  an excerpt.
- **`notice`:** a message as the app shows it: a `tone` (`neutral`, `accent`, `warning`,
  `error` or `success`), its `text`, and the HRID it names as a `link`.
- **`fragment`:** a spreadsheet or notebook page outside Iki: `file`, `columns`, up to 4
  `rows`, and `highlight`ed cells that disagree with the other copies.
- **A plugin**, for an inset: its `name` as installed (`iki/antibody`), what it
  `provides`, and the `fields` it defines.
- **A revision:** its `number`, `what` happened (the History tab's words: Created, Edited,
  State set…), `who`, `when` (with how: "in the web app", "through the API") and `why`,
  if a reason was given. A reason is optional in Iki, so leave some out.
- **A signature:** its `verb` (Approved, Reviewed…), the `meaning` its state gives it,
  `by` and `when`. Only states that need signatures have them: check the type's YAML.

### Lines: voice and caption style

- **Plain and short.** One idea a line, in the words a scientist uses. Prefer "register"
  to "persist", and "the same antibody" to "a duplicate identity".
- **At most two lines of 42 characters.** The check refuses longer lines: split the beat.
- **Accurate before catchy.** If a line needs a qualifier to be true, keep it ("and any
  reason given").
- **The UI's words for the UI's things:** "registry entry", "record", "revision",
  "Lineage", "lot". Use typographic apostrophes (it’s, can’t).
- **Say HRIDs the way people do.** Captions show `ABY-000001`; give the beat a `say`, such
  as "A B Y one", for the voice-over.
- **Cards have no captions.** The title and closing cards carry their text on screen. Give
  their beats a `say` instead, which the `.vtt` file and the voice-over use.

### Pacing

The theme (`src/system/theme/pacing.ts`) sets it, and the plan applies it:

- A line stays up for its words at about 160 a minute, plus 0.6 s, and never less than
  2 s. That reads at about 15 characters a second, under the 17 that subtitle guides allow.
- A beat lasts as long as its line or its steps, whichever is longer, plus its `hold`.
- A scene holds 0.5 s after its last line before the next transition; the last scene holds
  1.5 s.
- Transitions take 0.7 s. The first scene has none.

With a voice-over, each line lasts as long as its audio plus 0.35 s, and never less than
reading needs.

### When the layouts aren't enough

Some motion is particular to one video, such as copies collapsing into one record. Write it
in `src/<video>/custom.ts` as a custom step (`{ custom: <name>, length: <duration> }`) or a
custom transition (`transition: { custom: <name>, length: <duration> }`). A custom function
gets the scene's parts, the previous scene's parts, and `length`, the seconds it must
finish in. The runtime fails the render if it runs over.

Even custom code takes every number, colour and duration from the theme: ESLint refuses
literals in a video's folder, beyond 0 and 1. When a second video needs the same custom
code, move it into the system as a named step, transition or layout.

## 4. Render and review

```sh
make video-check                    # the scripts, their captions, a still of every scene
make video VIDEO=<video>            # the MP4, the .vtt and the manifest
pnpm --filter @ikisci/videos frames <video> /tmp/<video>-frames
```

Look at every frame. Then watch the whole video once at full size, and once on a phone.

## The reviewer's checklist

**Accuracy**

- [ ] Every label, message, field, HRID prefix and type name matches the product today,
      and the script names the ADRs or files its lines rest on.
- [ ] Nothing claims more than Iki does. Optional things, such as a revision's reason, are
      shown as optional.
- [ ] Sequences and values are real, from the repo or a primary source.
- [ ] Examples are plausible and consistent: the same people, projects and dates
      throughout.

**Script**

- [ ] Each scene teaches one idea, through the layout made for it.
- [ ] Lines are plain, short and in the UI's words. No caption is more than two lines.
- [ ] Every beat but a card's has a caption. HRIDs have a `say`.
- [ ] Custom code only where no layout or named step could show it.

**Frames**

- [ ] Nothing overlaps, is clipped, or runs out of its card or the safe area.
- [ ] Nothing sits in the caption band, except captions.
- [ ] Every caption is up while its scene shows what it says, and never covers content.
- [ ] Text reads on a phone. Nothing is smaller than the theme's label size.
- [ ] Motion explains: each move shows where something came from, what changed, or what
      belongs together. Nothing moves for decoration.

**Files**

- [ ] `make video-check` passes, with no stale renders listed.
- [ ] `out/<video>.mp4` (in Git LFS), `.vtt` and `.json` are committed with the script.
