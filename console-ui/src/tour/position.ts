// Where a callout goes next to the element it points at. Pure geometry in viewport pixels.

export interface Rect {
  top: number;
  left: number;
  width: number;
  height: number;
}

export interface Size {
  width: number;
  height: number;
}

export type Side = "top" | "bottom" | "left" | "right";

export interface Placement {
  side: Side;
  top: number;
  left: number;
  /**
   * Distance of the arrow from the callout's top or left corner, along the edge that faces the
   * target. Null when the callout had to overlap the target, so there is nothing to point across.
   */
  arrow: number | null;
}

export interface PlaceOptions {
  /** Space between the target and the callout. */
  gap: number;
  /** Space kept between the callout and the viewport edges. */
  margin: number;
  /** Closest the arrow may come to a corner of the callout. */
  arrowInset: number;
  /** Sides to try, in order of preference. */
  sides: Side[];
}

const DEFAULTS: PlaceOptions = { gap: 14, margin: 16, arrowInset: 24, sides: ["bottom", "top", "right", "left"] };

function clamp(value: number, min: number, max: number): number {
  return max < min ? min : Math.min(max, Math.max(min, value));
}

function room(side: Side, target: Rect, viewport: Size, margin: number): number {
  switch (side) {
    case "top":
      return target.top - margin;
    case "bottom":
      return viewport.height - (target.top + target.height) - margin;
    case "left":
      return target.left - margin;
    case "right":
      return viewport.width - (target.left + target.width) - margin;
  }
}

function needed(side: Side, popover: Size, gap: number): number {
  return (side === "top" || side === "bottom" ? popover.height : popover.width) + gap;
}

/**
 * Place a callout of `popover` size beside `target`. The first preferred side with room wins.
 * When none has room (a target taller than the screen, a phone in landscape), the callout goes
 * on the roomier of top and bottom and is pulled inside the viewport, covering part of the target.
 */
export function place(target: Rect, popover: Size, viewport: Size, options: Partial<PlaceOptions> = {}): Placement {
  const {
    gap = DEFAULTS.gap,
    margin = DEFAULTS.margin,
    arrowInset = DEFAULTS.arrowInset,
    sides = DEFAULTS.sides,
  } = options;
  const fits = sides.find((s) => room(s, target, viewport, margin) >= needed(s, popover, gap));
  const side =
    fits ?? (room("bottom", target, viewport, margin) >= room("top", target, viewport, margin) ? "bottom" : "top");

  const bottom = target.top + target.height;
  const right = target.left + target.width;
  const maxTop = viewport.height - margin - popover.height;
  const maxLeft = viewport.width - margin - popover.width;

  if (side === "top" || side === "bottom") {
    const ideal = side === "bottom" ? bottom + gap : target.top - gap - popover.height;
    const top = clamp(ideal, margin, maxTop);
    const left = clamp(target.left + target.width / 2 - popover.width / 2, margin, maxLeft);
    const clear = side === "bottom" ? top >= bottom : top + popover.height <= target.top;
    const arrow = clamp(target.left + target.width / 2 - left, arrowInset, popover.width - arrowInset);
    return { side, top, left, arrow: clear ? arrow : null };
  }

  const ideal = side === "right" ? right + gap : target.left - gap - popover.width;
  const left = clamp(ideal, margin, maxLeft);
  const top = clamp(target.top + target.height / 2 - popover.height / 2, margin, maxTop);
  const clear = side === "right" ? left >= right : left + popover.width <= target.left;
  const arrow = clamp(target.top + target.height / 2 - top, arrowInset, popover.height - arrowInset);
  return { side, top, left, arrow: clear ? arrow : null };
}

/**
 * How far to scroll so the target sits in the visible area below `inset` (a sticky header):
 * centred when it fits, aligned `margin` below the inset when it is too tall to centre.
 */
export function scrollDelta(target: Rect, viewportHeight: number, inset: number, margin = 16): number {
  const visible = viewportHeight - inset;
  if (target.height > visible - 2 * margin) return target.top - inset - margin;
  return target.top + target.height / 2 - (inset + visible / 2);
}

export function sameRect(a: Rect | null, b: Rect | null): boolean {
  if (a === null || b === null) return a === b;
  return a.top === b.top && a.left === b.left && a.width === b.width && a.height === b.height;
}
