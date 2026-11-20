---
name: iki-explainer-video
description: Make or update one of Iki's explainer videos (media/videos) with its design system. It covers checking every fact against the product, writing the YAML script, rendering headless, reviewing frames and committing the render. Use it when asked to make a concept video, change one, or bring one up to date after a product change.
---

# Making an Iki explainer video

You're making a video from a script, with a fixed design system. Consistency comes from
following the files, not from taste. Don't position, size, colour or time anything
yourself: pick layouts, fill slots, write lines. Read `media/videos/AUTHORING.md` once
before you start. This is the workflow, end to end.

## 1. Agree the concept

If you don't have an approved concept, write one: a title ("Concept: its promise"), a
target length, five to eight scenes (what's seen, and the one line said), and a last line
naming the next video. Stop and get it approved before writing the script.

## 2. Check every fact before you draw it

For each thing on screen, find where Iki defines it, and note the file:

- **UI wording:** `git grep -n "<phrase>" web/app/src`. Copy messages exactly, with ’ and “ ”.
- **Types, fields, HRID prefixes, states, signatures:** `plugins/<plugin>/types/*.yaml`.
- **Behaviour:** `docs/adr/`. Search the titles: `ls docs/adr | grep -i <topic>`.
- **Sequences and values:** the repo's tests and seeds (`git grep -il trastuzumab`), or a
  primary source.

If an approved line isn't true of Iki today, rewrite it, and list each change, and why, in
your report. Common traps:

- Saying something is refused when Iki reuses it instead.
- Showing a signature on a type that has no states needing one.
- Treating optional fields, such as a revision's reason, as required.
- Saying "nothing is lost". The History tab's line is "Revisions can’t be changed or
  removed, by anyone."

## 3. Write the script

1. `git ls-files media/videos/src` shows the existing videos. Copy `scene.tsx` and
   `project.ts` from `src/entities/` into `src/<video>/`, and change the script path in
   `scene.tsx`.
2. Write `src/<video>/script.yaml`, starting with the schema comment line. Use
   `src/specimen/script.yaml`, which shows every layout once, and `src/entities/script.yaml`
   as models. Choose layouts from the table in AUTHORING.md.
3. Rules the check enforces:
   - every beat has a stable `id`, and a `caption` of at most two 42-character lines (title
     and closing cards have a `say` instead);
   - steps name parts the layout has;
   - timing is only `hold: short | medium | long`, and step `length`s are the theme's
     duration names.
4. Give each HRID in a caption a `say` that reads it aloud ("A B Y one").
5. Name the ADRs and files behind each scene in a YAML comment above it.
6. Keep every product name, HRID, label and quoted message in the script, never in code.
   List the product's own words it shows under `quotes`, each with the folder the product
   says it in, so a later rename flags the video.
7. Give scenes `chapter`s (at least 3, each at least 10 seconds: join short scenes), and
   write `publish`: a description held to the accuracy rule, and tags.
8. Run `make video-check`. Fix everything it reports.

To show several things becoming one (copies into a record), use the `merge` transition:
`transition: { merge: { into: <part> } }`. Don't write custom code for it.

Custom code goes in `src/<video>/custom.ts` only when no layout or named step can show the
idea. It must take every number from the theme: ESLint refuses literals in a video's
folder. List each custom step in your report as a candidate for the system.

## 4. Render and review

```sh
make video VIDEO=<video>
media/videos/tools/container.sh node tools/frames.ts <video> review/frames
```

Both run in the render image and need Docker; never commit a render made outside it
(`IKI_VIDEO_NATIVE=1`). Run heavy renders through the machine's test-slot wrapper, if it has
one. Open every frame in `media/videos/review/frames/`, and the thumbnail, and check them
against "The reviewer's checklist" in AUTHORING.md:

- overlaps and clipping;
- nothing in the caption band;
- each caption matching what's on screen;
- legibility;
- accuracy.

Fix the script, or the system when the problem is the system's, render again, and look
again.

## 5. Commit

Commit `src/<video>/` and every `out/<video>.*` file together: both MP4s (Git LFS: run
`git lfs install --local` once per clone), the `.vtt` and `.srt`, the thumbnail, the
publishing files and the manifest. Check `git lfs ls-files` lists both MP4s.

When the product changes under a video (a quote test fails, or `make video-check` says a
render is stale), the fix is the same: edit the script if the product's words changed, run
`make video VIDEO=<video>`, review, and commit everything together.

Before committing, run:

- `pnpm lint`;
- `pnpm --filter @ikisci/videos typecheck`;
- `pnpm --filter @ikisci/videos test`;
- `make video-check`.

## When you change the design system

- Change `src/system/theme/` first. Components and layouts take everything from it.
- After `iki.css` changes, run `pnpm --filter @ikisci/videos tokens`.
- After schema changes, run `pnpm --filter @ikisci/videos schema`.
- A new layout needs its slots in `script/schema.ts`, its parts in `script/parts.ts` (the
  runtime fails if the built parts differ), a builder in `layouts/`, a row in
  AUTHORING.md's table, and a scene in `src/specimen/script.yaml`.
- Render every published video again, and review each one.
