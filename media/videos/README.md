# Iki's explainer videos

Short videos that explain Iki's concepts: entities first, then actions, triggers, plugins,
compute, analysis and the audit trail. Each one is a script, rendered with
[Motion Canvas](https://motioncanvas.io) (MIT) through a shared design system, so every
video looks, moves and reads the same, whoever makes it.

- **Write or change a video:** follow [AUTHORING.md](AUTHORING.md). It covers the concept,
  the script, the layouts, the voice, the accuracy rule and the reviewer's checklist.
- **A coding agent** can follow [SKILL.md](SKILL.md), which walks through the same
  workflow end to end.

## Render a video

```sh
make video VIDEO=entities          # out/entities.mp4, out/entities.vtt, out/entities.json
make video-check                   # every script, its captions, and a still of each scene
```

You need Node 24, pnpm, FFmpeg on your `PATH` (or set `FFMPEG=/path/to/ffmpeg`), and
headless Chromium for Playwright. `make setup` installs Chromium; if it's missing, run:

```sh
pnpm --filter @ikisci/videos exec playwright-core install chromium-headless-shell
```

Rendering is headless and repeatable, with no clicks in an editor:

1. `tools/render.ts` starts Vite, with Motion Canvas's plugin, on a free local port.
2. Headless Chromium (Playwright) opens `render.html?video=<video>`. That page
   (`src/system/render/page.ts`) runs Motion Canvas's own `Renderer`, with its
   image-sequence exporter, and writes every frame as a PNG to a temporary folder.
3. FFmpeg encodes the frames as H.264 (BT.709, `yuv420p`, `+faststart`), with no
   timestamps or encoder strings in the file.
4. The same plan that timed the video writes `out/<video>.vtt`, and `out/<video>.json`
   records the duration and a hash of everything the render was made from.

The Entities video renders in about a minute and a half on an M-series Mac.

To watch a video while you work on it, run `pnpm --filter @ikisci/videos editor` and open
the address it prints: Motion Canvas's editor previews and scrubs it. Don't render from the
editor: `make video` is the only way the committed renders are made.

## Review a render

```sh
pnpm --filter @ikisci/videos frames entities /tmp/entities-frames
```

This writes a frame near the end of every beat, when its steps are done and its caption is
still up, plus one in the middle of every transition. Look at each one against the
checklist in [AUTHORING.md](AUTHORING.md#the-reviewers-checklist).

## What's here

| Path                                       | What it is                                                                                                                       |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| `src/<video>/script.yaml`                  | The video: its scenes, their layouts and slots, and every caption line. Most of a video is this file.                            |
| `src/<video>/custom.ts`                    | The video's own steps and transitions, only when no layout or named step can show something. Each is a candidate for the system. |
| `src/<video>/scene.tsx`, `project.ts`      | The same few lines in every video: load the script and play it.                                                                  |
| `src/system/theme/`                        | The one source of truth: colours, type, spacing, radii, strokes, shadows, motion, pacing and captions.                           |
| `src/system/theme/tokens.generated.ts`     | The web app's design tokens, generated from `server/src/iki_server/web/static/iki/iki.css`. A test fails if they drift.          |
| `src/system/components/`                   | Record card, field list, HRID badge, timeline, revision chip, signature mark, graph node, connector, plugin inset, and the rest. |
| `src/system/layouts/`                      | The fixed set of scene layouts, each with named slots.                                                                           |
| `src/system/script/`                       | The script's schema, its rules, the timing plan and the captions.                                                                |
| `src/system/runtime/`                      | What plays a script: scenes, beats, steps and transitions.                                                                       |
| `script.schema.json`                       | The script's JSON Schema, for editors' autocompletion. Generated: `pnpm --filter @ikisci/videos schema`.                         |
| `tools/`                                   | `render`, `check`, `captions`, `frames`, `tokens` and `schema`.                                                                  |
| `out/`                                     | The renders. MP4s are in Git LFS; the `.vtt` and `.json` files are plain text.                                                   |
| `src/<video>/*.meta`, `src/*/project.meta` | Motion Canvas's own settings files. Keep them; nothing in them is ours.                                                          |

## After product changes

The videos show the product as it is, so they go stale when it changes. When a pull request
changes something a video shows, such as a label, a message, an HRID prefix, a field or a
type's name, update the video in the same pull request:

1. Find the videos that show it: `grep -rn "<the old wording>" media/videos/src`. Each
   script's comments name the ADRs and files its lines rest on.
2. Change the script. Run `make video-check`.
3. Render it again with `make video VIDEO=<video>` and review its frames.
4. Commit the script, `out/<video>.mp4`, `.vtt` and `.json` together.

When the design tokens change in `iki.css`, run `pnpm --filter @ikisci/videos tokens`, then
render every video again. `make video-check` lists the videos whose renders are older than
their sources.

## Captions and voice-over

Each video has one script, and that script drives everything that's timed:

- **Burned-in captions:** each beat's `caption`, wrapped to two lines of at most 42
  characters.
- **The `.vtt` sidecar:** written from the same timing plan, so it matches the burned-in
  captions to the millisecond.
- **The voice-over, later:** each beat's `say`, or its caption if it has no `say`.

There's no audio yet. To add a voice-over through the ElevenLabs API later:

1. For each beat, generate its line: the text is `say ?? caption`. File the audio as
   `src/<video>/voice/<beat id>.mp3`, so edits to other beats keep their audio.
2. Write `src/<video>/voice.json`, mapping each beat id to its audio's length in seconds.
   The plan (`src/system/script/plan.ts`) times each line to its audio plus a short pause,
   and never shorter than reading needs. A test covers this.
3. Lay the audio track out at each beat's planned start (FFmpeg's `adelay` and `amix`) and
   mux it into the MP4 as AAC. That step and the API call don't exist yet.

## Dependencies

Everything is pinned to an exact version at least 3 days old. The pnpm workspace refuses
newer ones.

- **Motion Canvas** `@motion-canvas/core`, `2d`, `ui` and `vite-plugin` 3.17.2: MIT.
- **Vite** 6.4.6: MIT. Motion Canvas's plugin asks for Vite 4 or 5, but Vite 5 has
  advisories fixed only in 6.4, and the plugin works on 6, as `make video-check` proves.
  pnpm warns about the peer range.
- **playwright-core** 1.65.0: Apache-2.0. It drives headless Chromium, and has no
  dependencies. Keep it at the same version as the Python `playwright` in `uv.lock`, so
  the browsers `make setup` installs serve both.
- **yaml** 2.9.1 (ISC) reads the scripts. **zod** 4.6.7 (MIT) checks them.
- **esbuild's install script** is refused in `pnpm-workspace.yaml`. It only checks the
  platform binary, which arrives as an optional dependency.

Text is set in the app's own font stacks: `system-ui` and the system monospace. A render
made on macOS uses SF Pro and SF Mono, and that's what the committed renders are made with.
A render made on Linux uses the fonts there, so its text may wrap differently.
