import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { place, sameRect, scrollDelta, type Rect } from "../src/tour/position";
import { SAMPLE_TOUR } from "../src/tour/SampleTour";
import { advance, progress, rememberTour, tourSeen, type KeyValueStore, type TourStep } from "../src/tour/steps";
import { TourCallout } from "../src/tour/Tour";

function memoryStore(): KeyValueStore & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return { data, getItem: (k) => data.get(k) ?? null, setItem: (k, v) => void data.set(k, v) };
}

const broken: KeyValueStore = {
  getItem: () => {
    throw new Error("SecurityError");
  },
  setItem: () => {
    throw new Error("QuotaExceededError");
  },
};

describe("tour steps", () => {
  it("moves forward, back, and ends after the last step", () => {
    expect(advance(0, "next", 3)).toBe(1);
    expect(advance(1, "next", 3)).toBe(2);
    expect(advance(2, "next", 3)).toBeNull();
    expect(advance(2, "back", 3)).toBe(1);
    expect(advance(0, "back", 3)).toBe(0);
    expect(progress(1, 5)).toBe("2 of 5");
  });

  it("remembers that a tour was seen", () => {
    const store = memoryStore();
    expect(tourSeen("t", store)).toBe(false);
    rememberTour("t", store);
    expect(tourSeen("t", store)).toBe(true);
    expect(tourSeen("other", store)).toBe(false);
  });

  it("treats storage it cannot use as seen, and never throws", () => {
    expect(tourSeen("t", null)).toBe(true);
    expect(tourSeen("t", broken)).toBe(true);
    expect(() => rememberTour("t", broken)).not.toThrow();
    expect(() => rememberTour("t", null)).not.toThrow();
  });

  it("walks the sample from the overview into a scenario", () => {
    expect(SAMPLE_TOUR.length).toBeGreaterThanOrEqual(4);
    expect(SAMPLE_TOUR.length).toBeLessThanOrEqual(6);
    expect(new Set(SAMPLE_TOUR.map((s) => s.id)).size).toBe(SAMPLE_TOUR.length);
    const pages = SAMPLE_TOUR.map((s) => s.page);
    expect(pages[0]).toBe("overview");
    expect(pages[pages.length - 1]).toBe("scenario");
    expect(pages.indexOf("scenario")).toBe(pages.lastIndexOf("overview") + 1);
  });
});

describe("callout placement", () => {
  const viewport = { width: 1280, height: 900 };
  const popover = { width: 360, height: 200 };
  const target = (r: Partial<Rect>): Rect => ({ top: 100, left: 400, width: 200, height: 60, ...r });

  it("goes below the target, centred, with the arrow at the target's middle", () => {
    expect(place(target({}), popover, viewport)).toEqual({ side: "bottom", top: 174, left: 320, arrow: 180 });
  });

  it("flips above when there is no room below", () => {
    const p = place(target({ top: 700 }), popover, viewport);
    expect(p.side).toBe("top");
    expect(p.top).toBe(700 - 14 - 200);
    expect(p.arrow).toBe(180);
  });

  it("follows the preferred sides", () => {
    const p = place(target({}), popover, viewport, { sides: ["right"] });
    expect(p).toEqual({ side: "right", top: 30, left: 614, arrow: 100 });
  });

  it("stays inside the viewport and keeps the arrow pointing at the target", () => {
    const p = place(target({ left: 0, width: 40 }), popover, viewport);
    expect(p.left).toBe(16);
    expect(p.arrow).toBe(24);
    const narrow = place(target({ left: 10, width: 370 }), { width: 358, height: 200 }, { width: 390, height: 844 });
    expect(narrow.left).toBe(16);
    expect(narrow.left + 358).toBeLessThanOrEqual(390 - 16);
  });

  it("overlaps a target taller than the screen, without an arrow", () => {
    const p = place(target({ top: 60, height: 820, left: 0, width: 1280 }), popover, viewport);
    expect(p.side).toBe("top");
    expect(p.top).toBe(16);
    expect(p.arrow).toBeNull();
  });

  it("works out how far to scroll a target into view below a header", () => {
    expect(scrollDelta(target({ top: 1000, height: 100 }), 900, 52)).toBe(1050 - (52 + 424));
    expect(scrollDelta(target({ top: 1000, height: 2000 }), 900, 52)).toBe(1000 - 52 - 16);
    expect(sameRect(target({}), target({}))).toBe(true);
    expect(sameRect(target({}), target({ top: 1 }))).toBe(false);
    expect(sameRect(null, null)).toBe(true);
    expect(sameRect(null, target({}))).toBe(false);
  });
});

describe("tour callout", () => {
  const step: TourStep = { id: "s", page: "overview", target: "h1", title: "What the customer wrote", body: "Same message." };
  const render = (index: number, count = 3) =>
    renderToStaticMarkup(
      <TourCallout
        step={step}
        index={index}
        count={count}
        placement={{ side: "bottom", top: 10, left: 20, arrow: 40 }}
        onBack={() => undefined}
        onNext={() => undefined}
        onSkip={() => undefined}
      />,
    );

  it("is a labelled modal dialog with a counter", () => {
    const html = render(0);
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="true"');
    expect(html).toMatch(/aria-labelledby="[^"]+ [^"]+"/);
    expect(html).toContain("aria-describedby=");
    expect(html).toContain("1 of 3");
    expect(html).toContain("tour-callout-bottom");
    expect(html).toContain("left:40px");
  });

  it("offers Skip and Next first, Back in the middle, Done at the end", () => {
    expect(render(0)).toContain("Skip tour");
    expect(render(0)).not.toContain("Back");
    expect(render(1)).toContain("Back");
    const last = render(2);
    expect(last).toContain("Done");
    expect(last).not.toContain("Skip tour");
  });

  it("centres itself without an arrow when there is nothing to point at", () => {
    const html = renderToStaticMarkup(
      <TourCallout step={step} index={0} count={1} placement={null} onBack={() => undefined} onNext={() => undefined} onSkip={() => undefined} />,
    );
    expect(html).toContain("tour-callout-center");
    expect(html).not.toContain("tour-arrow");
  });
});
