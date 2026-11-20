# Iki's explainer videos

Short videos that explain Iki's concepts: entities first, then actions, triggers, plugins,
compute, analysis and the audit trail. Each one is a script, rendered with
[Motion Canvas](https://motioncanvas.io) (MIT) through a shared design system, so every
video looks, moves and reads the same, whoever makes it, and can be made again whenever
the product changes.

- **Write or change a video:** follow [AUTHORING.md](AUTHORING.md). It covers the concept,
  the script, the layouts, the voice, the accuracy rule and the reviewer's checklist.
- **A coding agent** can follow [SKILL.md](SKILL.md), which walks through the same
  workflow end to end.

## Render a video

```sh
make video VIDEO=entities          # everything in out/ for that video
make video-check                   # every script, its outputs, its render, a still of each scene
```

Both run in the videos' render image (`Dockerfile`): Playwright's image, pinned by digest,
with FFmpeg. You need Docker; the first run builds the image. Every render comes out of
that one image, so it's the same on any machine, CI included: the same fonts, the same
Chromium, the same rasterizer. That matters because each platform's Chromium draws text its
own way (CoreText on macOS, FreeType on Linux), even with the same font files.

Inside the image, rendering is headless and repeatable, with no clicks in an editor:

1. `tools/render.ts` starts Vite, with Motion Canvas's plugin, on a free local port.
2. Headless Chromium (Playwright) opens `render.html?video=<video>`. That page
   (`src/system/render/page.ts`) runs Motion Canvas's own `Renderer`, with its
   image-sequence exporter, and writes every frame as a PNG.
3. It does that twice: without captions, then with them burned in.
4. FFmpeg encodes each as an MP4 (see [Encoding](#encoding)), and takes the thumbnail from
   the clean frames.
5. The same plan that timed the video writes the captions and the publishing files.

The Entities video renders in about two and a half minutes on an M-series Mac, both renders
included.

To watch a video while you work on it, run `pnpm --filter @ikisci/videos editor` and open
the address it prints: Motion Canvas's editor previews and scrubs it, with the same fonts.
For a quick still on your own machine, `IKI_VIDEO_NATIVE=1 media/videos/tools/container.sh
node tools/check.ts --stills review/stills` skips the image. Never commit a render made
outside the image: its text is antialiased differently.

## What a render makes

Everything is in `out/`, named for the video:

| File                    | What it's for                                                                                               |
| ----------------------- | ----------------------------------------------------------------------------------------------------------- |
| `<video>.mp4`           | The video without captions: the website's `<video>` and YouTube's upload. Viewers turn captions on and off. |
| `<video>.captioned.mp4` | The same with the captions burned in, for social clips, where players often show no captions.               |
| `<video>.vtt`           | The captions as WebVTT, for the website's `<track kind="captions" srclang="en">`.                           |
| `<video>.srt`           | The same captions as SubRip, for YouTube's caption upload.                                                  |
| `<video>.thumbnail.png` | 1280×720, YouTube's size: the script's chosen beat, the title card by default.                              |
| `<video>.publish.json`  | Title, description with chapters, tags, duration and file names, for a website build or an upload script.   |
| `<video>.publish.md`    | The same, to read and paste into YouTube: the description already has the chapters in YouTube's format.     |
| `<video>.json`          | The manifest: duration, size, and a hash of everything the render was made from.                            |

Captions as sidecars, rather than burned in, can be switched off, are read by screen
readers and search engines, and can be translated later: a translated `.vtt` sits next to
the English one, and the video needn't change.

## Hosting

**Serve the videos from the website, not from this repository.** The MP4s are in Git LFS,
and GitHub's free LFS quota is small, for storage and for downloads alike, and every
download of a file counts against it. Each render is a few MB, so the repository can hold
many, but a page that streams them from GitHub would use up the download quota fast. Have the website's build copy `out/<video>.mp4`,
the `.vtt` and the thumbnail into its own static hosting or CDN. CI never needs the MP4s:
its checkout skips LFS.

## Encoding

Both MP4s suit YouTube's recommended upload settings and streaming on the web:

- H.264, High profile, 4:2:0 (`yuv420p`), progressive, 1920×1080 at 30 fps.
- BT.709 colour, as HD video expects, tagged in the file.
- Closed GOPs of 2 seconds (60 frames) with two B-frames: the usual keyframe interval for
  streaming on the web, so a player can seek to within 2 seconds of any point. YouTube
  suggests half a second, but it re-encodes every upload, and half a second makes these
  mostly still frames more than twice the size (10 MB rather than 4.5 MB for Entities),
  which costs LFS quota and the website's bandwidth for no gain a viewer would see.
- `+faststart`: the index is at the front, so the browser starts playing while it loads.
- Constant quality (CRF 16, tuned for flat graphics): the files stay small, because most
  frames barely change.
- No audio yet. When a voice-over comes, the encoder muxes it as AAC-LC, 48 kHz stereo,
  192 kb/s, as YouTube recommends: `encode()` in `tools/render.ts` takes the audio track.
- No timestamps or encoder strings in the file.

## Review a render

```sh
media/videos/tools/container.sh node tools/frames.ts entities review/frames
```

This writes a frame near the end of every beat, from the captioned render, when its steps
are done and its caption is still up, plus one in the middle of every transition. Look at
each one against the checklist in [AUTHORING.md](AUTHORING.md#the-reviewers-checklist).
`review/` is ignored by git. CI also uploads a still of every scene, as the `video-stills`
artifact, so a pull request can be reviewed without Git LFS.

## What's here

| Path                                       | What it is                                                                                                                             |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| `src/<video>/script.yaml`                  | The video: its scenes, their layouts and slots, every caption line, its chapters, how it's published, and the product words it quotes. |
| `src/<video>/scene.tsx`, `project.ts`      | The same few lines in every video: load the script and play it.                                                                        |
| `src/<video>/custom.ts`                    | Only if the video needs motion no layout or named step has. None does yet.                                                             |
| `src/system/theme/`                        | The one source of truth: colours, type, fonts, spacing, radii, strokes, shadows, motion, pacing and captions.                          |
| `src/system/theme/tokens.generated.ts`     | The web app's design tokens, generated from `server/src/iki_server/web/static/iki/iki.css`. A test fails if they drift.                |
| `src/system/components/`                   | Record card, field list, HRID badge, timeline, revision chip, signature mark, graph node, connector, plugin inset, and the rest.       |
| `src/system/layouts/`                      | The fixed set of scene layouts, each with named slots.                                                                                 |
| `src/system/script/`                       | The script's schema, its rules, the timing plan, the captions and the publishing files.                                                |
| `src/system/runtime/`                      | What plays a script: scenes, beats, steps and transitions, `merge` among them.                                                         |
| `fonts/`                                   | Inter 4.1 and JetBrains Mono 2.304, both under the SIL Open Font License 1.1 (`OFL.txt` beside each).                                  |
| `Dockerfile`, `tools/container.sh`         | The render image, and what runs a command in it on this checkout.                                                                      |
| `script.schema.json`                       | The script's JSON Schema, for editors' autocompletion. Generated: `pnpm --filter @ikisci/videos schema`.                               |
| `tools/`                                   | `render`, `check`, `captions`, `frames`, `tokens`, `schema` and `container.sh`.                                                        |
| `out/`                                     | The renders and their sidecars. MP4s are in Git LFS; the rest is plain text, or a small PNG.                                           |
| `src/<video>/*.meta`, `src/*/project.meta` | Motion Canvas's own settings files. Keep them; nothing in them is ours.                                                                |

## After product changes

The videos show the product as it is, so they go stale when it changes. Two checks catch
that:

- **Quoted words.** Each script lists the product's words it shows (`quotes`): labels,
  messages, type names. A test, part of every code change's checks, fails when one no longer
  appears in the product's source, and names the video.
- **Stale renders.** `make video-check` fails when a committed render was made from
  anything other than what's in the repository now: its script, the theme, the components,
  the fonts, the encoder settings or Motion Canvas's version. Its manifest keeps the hash.
  Edits that don't change a frame, such as the description, tags, chapters or comments,
  don't count.

When either fails, or a pull request changes something a video shows, update the video in
the same pull request:

1. Change the script. Names, HRIDs, labels and quoted text live only there, so a rename is a
   script edit.
2. Run `make video VIDEO=<video>`, and review its frames.
3. Commit the script and everything in `out/` for that video, together.

When the design tokens change in `iki.css`, run `pnpm --filter @ikisci/videos tokens`, then
render every video again.

## Captions and voice-over

Each video has one script, and that script drives everything that's timed:

- **Burned-in captions** (the captioned MP4): each beat's `caption`, wrapped to two lines
  of at most 42 characters.
- **The `.vtt` and `.srt` sidecars:** written from the same timing plan, so they match the
  burned-in captions to the millisecond.
- **The voice-over, later:** each beat's `say`, or its caption if it has no `say`.

There's no audio yet. To add a voice-over through the ElevenLabs API later:

1. For each beat, generate its line: the text is `say ?? caption`. File the audio as
   `src/<video>/voice/<beat id>.mp3`, so edits to other beats keep their audio.
2. Write `src/<video>/voice.json`, mapping each beat id to its audio's length in seconds.
   The plan (`src/system/script/plan.ts`) times each line to its audio plus a short pause,
   and never shorter than reading needs. A test covers this.
3. Lay the audio track out at each beat's planned start (FFmpeg's `adelay` and `amix`), and
   pass it to `encode()`. That step and the API call don't exist yet.

## Dependencies

Everything is pinned to an exact version at least 3 days old. The pnpm workspace refuses
newer ones.

- **Motion Canvas** `@motion-canvas/core`, `2d`, `ui` and `vite-plugin` 3.17.2: MIT.
- **Vite** 6.4.6: MIT. Motion Canvas's plugin asks for Vite 4 or 5, so pnpm warns about the
  peer range, but the plugin works on 6, as `make video-check` and every render show. We
  keep 6 because Vite 5.4.21, the last 5.x, has eight advisories fixed only in 6.4:
  GHSA-4w7w-66w2-5vf9, GHSA-587h-7wmj-h5pm, GHSA-698v-4r3x-3vcv, GHSA-9jrq-w75r-8gcw,
  GHSA-fx2h-pf6j-xcff, GHSA-r825-w8mm-5pxw, GHSA-rq7h-c2jc-7f22 and GHSA-v6wh-96g9-6wx3.
  They're in Vite's dev server, which here only ever listens on 127.0.0.1, but
  `make check-deps` rightly refuses them, and there's no reason to carry exceptions. Move
  back inside the plugin's range when Motion Canvas supports Vite 6 or later.
- **playwright-core** 1.65.0: Apache-2.0. It drives headless Chromium, and has no
  dependencies. Keep it at the same version as the render image's tag and the Python
  `playwright` in `uv.lock`, so the browsers match.
- **yaml** 2.9.1 (ISC) reads the scripts. **zod** 4.6.7 (MIT) checks them.
- **esbuild's install script** is refused in `pnpm-workspace.yaml`. It only checks the
  platform binary, which arrives as an optional dependency.
- **The render image:** `mcr.microsoft.com/playwright:v1.65.0-noble`, by digest (Apache-2.0),
  with Ubuntu's FFmpeg 6.1.1 (LGPL and GPL, with libx264), used only as a tool, never
  shipped. Dependabot bumps the image.
- **Fonts:** Inter 4.1 (rsms/inter) and JetBrains Mono 2.304 (JetBrains/JetBrainsMono), from
  their official releases, unmodified, under the SIL Open Font License 1.1. The web app uses
  the system's fonts, which differ by machine, so the videos bundle their own.
