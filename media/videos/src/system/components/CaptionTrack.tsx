import { Rect, Txt } from "@motion-canvas/2d";
import { all, createRef, type ThreadGenerator } from "@motion-canvas/core";
import { easing } from "../theme/easing.ts";
import { duration } from "../theme/motion.ts";
import { caption, font, frame, ink, radius, stroke } from "../theme/theme.ts";

/**
 * Burned-in captions: one line of the script at a time, on a plate at the bottom of the
 * frame, in the caption band that layouts leave free.
 */
export class CaptionTrack {
  private readonly plate = createRef<Rect>();
  private readonly text = createRef<Txt>();
  private showing = false;

  node(): Rect {
    const role = caption.type;
    return (
      <Rect
        ref={this.plate}
        layout
        y={frame.height / 2 - caption.centerFromBottom}
        paddingLeft={caption.plate.padX}
        paddingRight={caption.plate.padX}
        paddingTop={caption.plate.padY}
        paddingBottom={caption.plate.padY}
        radius={radius.card}
        fill={caption.plate.fill}
        stroke={caption.plate.line}
        lineWidth={stroke.hairline}
        opacity={0}
      >
        <Txt
          ref={this.text}
          text=""
          fill={ink.text}
          fontFamily={font.text}
          fontSize={role.size}
          fontWeight={role.weight}
          lineHeight={role.size * role.lineHeight}
          textAlign="center"
        />
      </Rect>
    ) as Rect;
  }

  /** Shows these lines (a caption, wrapped), replacing whatever is showing. */
  *show(lines: string[]): ThreadGenerator {
    const half = duration.instant / 2;
    if (this.showing) {
      yield* this.text().opacity(0, half, easing.exit);
    }
    this.text().text(lines.join("\n"));
    if (this.showing) {
      yield* this.text().opacity(1, half, easing.enter);
    } else {
      this.text().opacity(1);
      yield* this.plate().opacity(1, duration.instant, easing.enter);
      this.showing = true;
    }
  }

  *clear(): ThreadGenerator {
    if (!this.showing) return;
    this.showing = false;
    yield* all(this.plate().opacity(0, duration.instant, easing.exit));
  }
}
