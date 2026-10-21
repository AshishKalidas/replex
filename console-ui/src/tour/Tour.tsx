import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties, type RefObject } from "react";
import { place, sameRect, scrollDelta, type Placement, type Rect, type Size } from "./position";
import { advance, progress, type Direction, type TourStep } from "./steps";
import "./tour.css";

/** Space between a target and the edge of its spotlight. */
const SPOTLIGHT_PADDING = 8;
/** How long a step waits for its target to render before showing the callout on its own. */
const TARGET_TIMEOUT_MS = 4000;

function reducedMotion(): boolean {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}

interface Found {
  element: HTMLElement | null;
  /** The target never appeared: show the callout without pointing at anything. */
  missing: boolean;
}

/** The element matching `selector`, waiting for it while the page renders its data. */
function useTarget(selector: string | null): Found {
  const [found, setFound] = useState<Found>({ element: null, missing: false });
  useEffect(() => {
    setFound({ element: null, missing: false });
    if (!selector) return;
    const observer = new MutationObserver(() => look());
    const timer = window.setTimeout(() => {
      observer.disconnect();
      setFound({ element: null, missing: true });
    }, TARGET_TIMEOUT_MS);
    const stop = () => {
      observer.disconnect();
      window.clearTimeout(timer);
    };
    const look = () => {
      const element = document.querySelector<HTMLElement>(selector);
      if (element) {
        stop();
        setFound({ element, missing: false });
      }
      return element;
    };
    if (!look()) observer.observe(document.body, { childList: true, subtree: true });
    return stop;
  }, [selector]);
  return found;
}

interface Layout {
  /** The spotlight: the target's box plus padding, in viewport pixels. */
  rect: Rect;
  viewport: Size;
}

/** Where the target is on screen, kept current through scrolling, resizing and reflow. */
function useLayout(element: HTMLElement | null): Layout | null {
  const [layout, setLayout] = useState<Layout | null>(null);
  useLayoutEffect(() => {
    if (!element) {
      setLayout(null);
      return;
    }
    let frame = 0;
    const measure = () => {
      frame = 0;
      const box = element.getBoundingClientRect();
      const rect = {
        top: box.top - SPOTLIGHT_PADDING,
        left: box.left - SPOTLIGHT_PADDING,
        width: box.width + 2 * SPOTLIGHT_PADDING,
        height: box.height + 2 * SPOTLIGHT_PADDING,
      };
      const viewport = { width: document.documentElement.clientWidth, height: window.innerHeight };
      setLayout((prev) =>
        prev && sameRect(prev.rect, rect) && prev.viewport.width === viewport.width && prev.viewport.height === viewport.height
          ? prev
          : { rect, viewport },
      );
    };
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(measure);
    };
    measure();
    const observer = new ResizeObserver(schedule);
    observer.observe(element);
    observer.observe(document.body);
    window.addEventListener("resize", schedule);
    window.addEventListener("scroll", schedule, true);
    return () => {
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener("resize", schedule);
      window.removeEventListener("scroll", schedule, true);
    };
  }, [element]);
  return layout;
}

/** The callout's rendered size, so it can be placed before it is painted. */
function useSize(ref: RefObject<HTMLElement>, key: string, present: boolean): Size | null {
  const [size, setSize] = useState<Size | null>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) {
      setSize(null);
      return;
    }
    const measure = () =>
      setSize((prev) =>
        prev && prev.width === element.offsetWidth && prev.height === element.offsetHeight
          ? prev
          : { width: element.offsetWidth, height: element.offsetHeight },
      );
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref, key, present]);
  return size;
}

/** Keep Tab and Shift+Tab cycling through the callout's controls. */
function trapFocus(event: KeyboardEvent, container: HTMLElement | null): void {
  const controls = container ? Array.from(container.querySelectorAll<HTMLElement>("button:not(:disabled)")) : [];
  const first = controls[0];
  const last = controls[controls.length - 1];
  if (!first || !last) {
    event.preventDefault();
    return;
  }
  const active = document.activeElement;
  if (!container?.contains(active)) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  } else if (event.shiftKey && active === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

interface CalloutProps {
  step: TourStep;
  index: number;
  count: number;
  /** Null shows the callout centred on screen, pointing at nothing. */
  placement: Placement | null;
  onBack: () => void;
  onNext: () => void;
  onSkip: () => void;
  boxRef?: RefObject<HTMLDivElement>;
  primaryRef?: RefObject<HTMLButtonElement>;
}

/** One step of a tour: counter, title, explanation and the Back / Next / Skip controls. */
export function TourCallout({ step, index, count, placement, onBack, onNext, onSkip, boxRef, primaryRef }: CalloutProps) {
  const countId = useId();
  const titleId = useId();
  const bodyId = useId();
  const last = index === count - 1;
  const position: CSSProperties | undefined = placement ? { top: placement.top, left: placement.left } : undefined;
  const vertical = placement?.side === "top" || placement?.side === "bottom";
  return (
    <div
      ref={boxRef}
      className={placement ? `tour-callout tour-callout-${placement.side}` : "tour-callout tour-callout-center"}
      style={position}
      role="dialog"
      aria-modal="true"
      aria-labelledby={`${countId} ${titleId}`}
      aria-describedby={bodyId}
    >
      {placement && placement.arrow !== null && (
        <span className="tour-arrow" style={vertical ? { left: placement.arrow } : { top: placement.arrow }} aria-hidden="true" />
      )}
      <p className="tour-count" id={countId}>
        {progress(index, count)}
      </p>
      <h2 className="tour-title" id={titleId}>
        {step.title}
      </h2>
      <p className="tour-body" id={bodyId}>
        {step.body}
      </p>
      <div className="tour-controls">
        {!last && (
          <button type="button" className="link tour-skip" onClick={onSkip}>
            Skip tour
          </button>
        )}
        <span className="tour-moves">
          {index > 0 && (
            <button type="button" className="button button-secondary" onClick={onBack}>
              Back
            </button>
          )}
          <button ref={primaryRef} type="button" className="button" onClick={onNext}>
            {last ? "Done" : "Next"}
          </button>
        </span>
      </div>
    </div>
  );
}

interface TourProps {
  /** At least one step. */
  steps: [TourStep, ...TourStep[]];
  /** The page on screen, compared with each step's `page`. */
  page: string;
  /** Take the visitor to `step.page`. Return false when that page cannot be reached; the tour then ends. */
  onNavigate: (step: TourStep) => boolean;
  onClose: () => void;
  /** Selector of a sticky header that targets must not be scrolled behind. */
  header?: string;
}

/**
 * A modal walk through the page: each step dims the screen except for its target and explains
 * it in a callout beside it. Steps can span pages; the tour asks to be taken there and waits for
 * the target to render. Leaving the tour's page any other way (the browser's Back button) ends it.
 */
export function Tour({ steps, page, onNavigate, onClose, header }: TourProps) {
  const [index, setIndex] = useState(0);
  const step = steps[index] ?? steps[0];
  const navigating = useRef(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const primaryRef = useRef<HTMLButtonElement>(null);

  const { element, missing } = useTarget(page === step.page ? step.target : null);
  const layout = useLayout(element);
  const showing = element !== null || missing;
  const size = useSize(boxRef, step.id, showing);

  const go = useCallback(
    (direction: Direction) => {
      const to = advance(index, direction, steps.length);
      if (to === null) {
        onClose();
        return;
      }
      const next = steps[to];
      if (!next || to === index) return;
      if (next.page !== page) {
        if (!onNavigate(next)) {
          onClose();
          return;
        }
        navigating.current = true;
      }
      setIndex(to);
    },
    [index, steps, page, onNavigate, onClose],
  );

  useEffect(() => {
    if (page === step.page) navigating.current = false;
    else if (!navigating.current) onClose();
  }, [page, step.page, onClose]);

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    return () => {
      if (opener?.isConnected) opener.focus();
    };
  }, []);

  useEffect(() => {
    if (showing) primaryRef.current?.focus({ preventScroll: true });
  }, [showing, step.id]);

  useEffect(() => {
    if (!element) return;
    const inset = header ? (document.querySelector(header)?.getBoundingClientRect().bottom ?? 0) : 0;
    const top = scrollDelta(element.getBoundingClientRect(), window.innerHeight, Math.max(0, inset));
    window.scrollBy({ top, behavior: reducedMotion() ? "auto" : "smooth" });
  }, [element, header]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        go("next");
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        go("back");
      } else if (event.key === "Tab") {
        trapFocus(event, boxRef.current);
      }
      // The tour is modal: the page's own shortcuts (j, k, Enter, /) must not act behind it.
      event.stopPropagation();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [go, onClose]);

  const placement = layout && size ? place(layout.rect, size, layout.viewport, { sides: step.sides }) : null;
  const unplaced = layout !== null && placement === null;

  return (
    <div className="tour">
      <div className={layout ? "tour-blocker" : "tour-blocker tour-dim"} />
      {layout && (
        <div
          className="tour-spotlight"
          style={{ top: layout.rect.top, left: layout.rect.left, width: layout.rect.width, height: layout.rect.height }}
          aria-hidden="true"
        />
      )}
      {showing && (
        <TourCallout
          key={step.id}
          step={step}
          index={index}
          count={steps.length}
          placement={unplaced ? { side: "bottom", top: 0, left: 0, arrow: null } : placement}
          onBack={() => go("back")}
          onNext={() => go("next")}
          onSkip={onClose}
          boxRef={boxRef}
          primaryRef={primaryRef}
        />
      )}
    </div>
  );
}
