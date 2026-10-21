// A guided tour: an ordered list of steps, each pointing at one element on one page.

import type { Side } from "./position";

export interface TourStep {
  /** Stable id; a new id gives the callout a fresh mount, so screen readers announce it. */
  id: string;
  /** The page the step belongs to. The tour asks to be taken there before showing the step. */
  page: string;
  /** CSS selector of the element the callout points at. */
  target: string;
  title: string;
  body: string;
  /** Sides to try for the callout, in order of preference. */
  sides?: Side[];
}

export type Direction = "next" | "back";

/** The index after one move, or null when moving forward from the last step ends the tour. */
export function advance(index: number, direction: Direction, count: number): number | null {
  if (direction === "back") return Math.max(0, index - 1);
  return index + 1 < count ? index + 1 : null;
}

export function progress(index: number, count: number): string {
  return `${index + 1} of ${count}`;
}

/** The part of Web Storage the tour needs, so tests can pass a plain object. */
export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

function browserStorage(): KeyValueStore | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/**
 * Whether the tour stored under `key` has been shown. Storage that cannot be read counts as
 * seen: without a memory, offering the tour on every visit would be worse than not offering it.
 */
export function tourSeen(key: string, store: KeyValueStore | null = browserStorage()): boolean {
  if (!store) return true;
  try {
    return store.getItem(key) !== null;
  } catch {
    return true;
  }
}

export function rememberTour(key: string, store: KeyValueStore | null = browserStorage()): void {
  try {
    store?.setItem(key, "seen");
  } catch {
    // Full storage: the tour is offered again on the next visit, which is harmless.
  }
}
