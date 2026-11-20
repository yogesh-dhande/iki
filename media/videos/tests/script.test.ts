// Scripts: the schema, the rules, the plan's timing and the captions it writes.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { stringify } from "yaml";
import { toSrt, toVtt, wrapCaption } from "../src/system/script/captions.ts";
import { scriptJsonSchema } from "../src/system/script/json-schema.ts";
import { ScriptError, loadScript } from "../src/system/script/load.ts";
import { partsOf } from "../src/system/script/parts.ts";
import { planVideo } from "../src/system/script/plan.ts";
import { chapterProblems, chaptersOf, publishing } from "../src/system/script/publish.ts";
import { duration, stagger, stepDuration } from "../src/system/theme/motion.ts";
import { pacing, readingSeconds, wordCount } from "../src/system/theme/pacing.ts";
import { caption } from "../src/system/theme/theme.ts";
import { planFor, readVideo, textOutputs, videos } from "../tools/lib.ts";
import { staleQuotes } from "../tools/quotes.ts";

const root = join(import.meta.dirname, "..");

/** A small valid script, to bend in each test. */
function script(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "sample",
    title: "Sample",
    series: "Iki concepts",
    target: 20,
    publish: { description: "What entities are.", tags: ["Iki"] },
    scenes: [
      {
        id: "card",
        layout: "title",
        chapter: "Entities",
        slots: { eyebrow: "Iki concepts", title: "Sample" },
        beats: [{ id: "intro", say: "A sample." }],
      },
      {
        id: "graph",
        layout: "graph",
        slots: {
          nodes: [
            { id: "a", kind: "Antibody", title: "Trastuzumab", hrid: "ABY-000001" },
            { id: "b", kind: "Protein lot", title: "Lot 1", hrid: "PLOT-000001" },
          ],
          edges: [{ from: "a", to: "b", label: "lot of it" }],
        },
        beats: [
          {
            id: "link",
            caption: "Entities link to what was made from them.",
            do: [{ together: [{ show: "edge:a>b", as: "draw" }, { show: "node:b" }] }],
          },
        ],
      },
    ],
    ...overrides,
  };
}

function load(value: Record<string, unknown>) {
  return loadScript(stringify(value), "sample.yaml");
}

function problems(value: Record<string, unknown>): string[] {
  try {
    load(value);
    return [];
  } catch (error) {
    if (error instanceof ScriptError) return error.problems;
    throw error;
  }
}

describe("a script", () => {
  it("loads when it's valid", () => {
    expect(load(script()).scenes).toHaveLength(2);
  });

  it("names a layout from the fixed set", () => {
    const value = script();
    (value["scenes"] as Record<string, unknown>[])[1] = {
      id: "x",
      layout: "freeform",
      slots: {},
      beats: [],
    };
    expect(problems(value).join()).toMatch(/layout/);
  });

  it("can't time things in raw seconds", () => {
    const value = script();
    const scene = (value["scenes"] as { beats: Record<string, unknown>[] }[])[1];
    if (scene?.beats[0]) scene.beats[0]["hold"] = 1.5;
    expect(problems(value).join()).toMatch(/hold/);
  });

  it("only acts on parts its layout has", () => {
    const value = script();
    const scene = (value["scenes"] as { beats: Record<string, unknown>[] }[])[1];
    if (scene?.beats[0]) scene.beats[0]["do"] = [{ show: "node:c" }];
    expect(problems(value).join()).toMatch(/"node:c" isn't a part of a graph scene/);
  });

  it("keeps captions to two lines", () => {
    const value = script();
    const scene = (value["scenes"] as { beats: Record<string, unknown>[] }[])[1];
    if (scene?.beats[0]) scene.beats[0]["caption"] = "word ".repeat(30).trim();
    expect(problems(value).join()).toMatch(/keep it to 2/);
  });

  it("captions every beat but a card's", () => {
    const value = script();
    const scene = (value["scenes"] as { beats: Record<string, unknown>[] }[])[1];
    if (scene?.beats[0]) delete scene.beats[0]["caption"];
    expect(problems(value).join()).toMatch(/give it a caption/);
  });

  it("joins edges to nodes that exist", () => {
    const value = script();
    const scene = (value["scenes"] as { slots: { edges: unknown[] } }[])[1];
    scene?.slots.edges.push({ from: "a", to: "nowhere" });
    expect(problems(value).join()).toMatch(/"nowhere", which isn't a node/);
  });

  it("names its parts the way the layouts build them", () => {
    const [, graph] = load(script()).scenes;
    expect(graph && partsOf(graph)).toEqual(["node:a", "node:b", "edge:a>b"]);
  });
});

describe("pacing", () => {
  it("counts an HRID as one word", () => {
    expect(wordCount("Iki finds ABY-000001, not a duplicate.")).toBe(6);
  });

  it("gives a line the time to read it aloud, and never less than the minimum", () => {
    expect(readingSeconds("Hi.")).toBe(pacing.minLine);
    const words = "one two three four five six seven eight nine ten eleven twelve";
    expect(readingSeconds(words)).toBeCloseTo(12 / pacing.wordsPerSecond + pacing.linePad);
  });
});

describe("the plan", () => {
  it("runs scenes back to back, each beat as long as its line or its steps", () => {
    const plan = planVideo(load(script()));
    const [card, graph] = plan.scenes;
    expect(card?.start).toBe(0);
    expect(card?.transitionLength).toBe(0);
    expect(graph?.start).toBeCloseTo(card?.end ?? 0);
    expect(graph?.ready).toBeCloseTo((graph?.start ?? 0) + duration.transition);
    const beat = graph?.beats[0];
    expect(beat?.steps.map((step) => step.at)).toEqual([0, stagger]);
    expect((beat?.end ?? 0) - (beat?.start ?? 0)).toBeCloseTo(
      Math.max(
        readingSeconds("Entities link to what was made from them."),
        stagger + stepDuration.show,
      ),
    );
    expect(plan.duration).toBeCloseTo((beat?.end ?? 0) + pacing.endHold);
  });

  it("times a line to its voice-over when there is one", () => {
    const plan = planVideo(load(script()), { link: 7 });
    const beat = plan.scenes[1]?.beats[0];
    expect((beat?.end ?? 0) - (beat?.start ?? 0)).toBeCloseTo(7 + pacing.voicePad);
  });
});

describe("captions", () => {
  it("wraps into lines no longer than the limit, evened out", () => {
    const lines = wrapCaption(
      "Register the same chains again, and Iki finds ABY-000001, not a duplicate.",
    );
    expect(lines).toHaveLength(2);
    for (const line of lines) expect(line.length).toBeLessThanOrEqual(caption.maxCharsPerLine);
  });

  it("writes WebVTT cues timed as the plan shows them, cards' spoken lines included", () => {
    const vtt = toVtt(planVideo(load(script())));
    expect(vtt.startsWith("WEBVTT\n\nintro\n00:00:00.000 --> ")).toBe(true);
    expect(vtt).toContain("link\n00:00:");
    expect(vtt).toContain("Entities link to what was made from them.");
  });
});

describe("the videos", () => {
  it("have a JSON Schema for editors that matches the Zod schema", () => {
    expect(
      readFileSync(join(root, "script.schema.json"), "utf8"),
      "Run `pnpm --filter @ikisci/videos schema`.",
    ).toBe(scriptJsonSchema());
  });

  it.each(videos())(
    "%s: its script is valid, and the product still says what it quotes",
    (video) => {
      const script = readVideo(video);
      expect(script.id).toBe(video);
      expect(staleQuotes(script)).toEqual([]);
    },
  );

  it.each(videos().filter((video) => !readVideo(video).specimen))(
    "%s: its committed captions and publishing files match its script, and YouTube takes its chapters",
    (video) => {
      for (const [name, text] of Object.entries(textOutputs(video))) {
        expect(
          readFileSync(join(root, "out", name), "utf8"),
          `out/${name}: run make video VIDEO=${video}`,
        ).toBe(text);
      }
      expect(chapterProblems(planFor(video))).toEqual([]);
    },
  );
});

describe("a merge transition", () => {
  const gallery = {
    id: "copies",
    layout: "gallery",
    slots: {
      items: [
        { fragment: { kind: "spreadsheet", file: "a.xlsx", columns: ["Name"], rows: [["T"]] } },
        { fragment: { kind: "spreadsheet", file: "b.xlsx", columns: ["Name"], rows: [["T"]] } },
      ],
    },
    beats: [{ id: "copies", caption: "Two copies." }],
  };
  const record = (transition: unknown) => ({
    id: "one",
    layout: "focus",
    transition,
    slots: { object: { record: { type: "Antibody", hrid: "ABY-000001", name: "T" } } },
    beats: [{ id: "one", caption: "One record." }],
  });
  const withScenes = (transition: unknown) =>
    script({ scenes: [{ ...gallery, chapter: "Copies" }, record(transition)] });

  it("merges the gallery's items into a part of the next scene, taking its time from the theme", () => {
    const video = load(withScenes({ merge: { into: "object" } }));
    expect(planVideo(video).scenes[1]?.transitionLength).toBe(duration.deliberate);
  });

  it("merges only into a part the scene has, from parts the scene before has", () => {
    expect(problems(withScenes({ merge: { into: "record" } }))).toEqual([
      expect.stringContaining('merges into "record"'),
    ]);
    expect(problems(withScenes({ merge: { into: "object", from: ["item:3"] } }))).toEqual([
      expect.stringContaining('merges "item:3"'),
    ]);
  });
});

describe("publishing", () => {
  it("needs a description and tags, and the first scene to start a chapter", () => {
    const value = script();
    delete value["publish"];
    expect(problems(value)).toEqual([expect.stringContaining("publish:")]);
    const scenes = script()["scenes"] as Record<string, unknown>[];
    delete scenes[0]?.["chapter"];
    expect(problems(script({ scenes }))).toEqual([expect.stringContaining("chapter")]);
  });

  it("keeps the tags within YouTube's 500 characters, and the thumbnail on a beat", () => {
    const tags = Array.from({ length: 30 }, (_, i) => `a long search term ${String(i)}`);
    expect(problems(script({ publish: { description: "D", tags } }))).toEqual([
      expect.stringContaining("publish.tags"),
    ]);
    expect(
      problems(script({ publish: { description: "D", tags: ["Iki"], thumbnail: "nope" } })),
    ).toEqual([expect.stringContaining("publish.thumbnail")]);
  });

  it("quotes only what the script shows", () => {
    expect(problems(script({ quotes: [{ text: "Lot of" }] }))).toEqual([
      expect.stringContaining("quotes.0"),
    ]);
    expect(load(script({ quotes: [{ text: "lot of it" }] })).quotes).toEqual([
      { text: "lot of it", in: "web/app/src" },
    ]);
  });

  it("lists chapters in YouTube's format, from 0:00, and says when YouTube won't show them", () => {
    const video = load(script());
    const plan = planVideo(video);
    expect(chaptersOf(plan)).toEqual([{ start: 0, time: "0:00", title: "Entities" }]);
    expect(chapterProblems(plan)).toEqual([
      expect.stringContaining("1 chapters"),
      expect.stringContaining('Chapter "Entities" runs'),
    ]);
    expect(publishing(video, plan).description).toBe(
      "What entities are.\n\nChapters\n0:00 Entities",
    );
  });

  it("writes SubRip captions, numbered, with commas before the milliseconds", () => {
    const srt = toSrt(planVideo(load(script())));
    expect(srt).toMatch(/^1\n00:00:00,000 --> 00:00:\d\d,\d{3}\nA sample\.\n\n2\n/);
    expect(srt).toContain("Entities link to what was made from them.");
  });
});
