// Easing: things arrive decelerating, leave accelerating, and move with both.
import {
  easeInCubic,
  easeInOutCubic,
  easeOutBack,
  easeOutCubic,
  type TimingFunction,
} from "@motion-canvas/core";

export const easing: Record<"enter" | "exit" | "move" | "settle", TimingFunction> = {
  enter: easeOutCubic,
  exit: easeInCubic,
  move: easeInOutCubic,
  settle: easeOutBack,
};
