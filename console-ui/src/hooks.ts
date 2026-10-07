import { useCallback, useEffect, useRef, useState } from "react";
import { parseRoute, type Route } from "./router";

export type Async<T> =
  | { state: "loading" }
  | { state: "error"; error: string }
  | { state: "ready"; data: T };

/** Run an async loader whenever its dependencies change. */
export function useAsync<T>(load: () => Promise<T>, deps: unknown[]): Async<T> {
  const [value, setValue] = useState<Async<T>>({ state: "loading" });
  useEffect(() => {
    let alive = true;
    setValue({ state: "loading" });
    load().then(
      (data) => alive && setValue({ state: "ready", data }),
      (err: unknown) => alive && setValue({ state: "error", error: err instanceof Error ? err.message : String(err) }),
    );
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return value;
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

function typingInField(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

/**
 * Keyboard navigation over a list: j / ↓ next, k / ↑ previous, Enter or o activates,
 * g / G first / last. The active row is scrolled into view (rows carry data-nav-index).
 */
export function useKeyNav(
  count: number,
  onActivate?: (index: number) => void,
  initial: number | null = null,
): [number, (i: number) => void] {
  const [active, setActive] = useState<number>(initial ?? 0);
  const activateRef = useRef(onActivate);
  activateRef.current = onActivate;

  useEffect(() => {
    if (initial !== null) setActive(initial);
  }, [initial]);

  useEffect(() => {
    if (active >= count && count > 0) setActive(count - 1);
  }, [count, active]);

  const scrollTo = useCallback((i: number) => {
    const el = document.querySelector<HTMLElement>(`[data-nav-index="${i}"]`);
    el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (count === 0 || typingInField(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
      let next: number | null = null;
      if (e.key === "j" || e.key === "ArrowDown") next = Math.min(count - 1, active + 1);
      else if (e.key === "k" || e.key === "ArrowUp") next = Math.max(0, active - 1);
      else if (e.key === "g") next = 0;
      else if (e.key === "G") next = count - 1;
      else if ((e.key === "Enter" || e.key === "o") && activateRef.current) {
        e.preventDefault();
        activateRef.current(active);
        return;
      }
      if (next !== null) {
        e.preventDefault();
        setActive(next);
        scrollTo(next);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [count, active, scrollTo]);

  return [active, setActive];
}
