import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import { expect, SCENARIOS, test } from "./console";

/** Every view, with something on screen that shows it has finished loading. */
const VIEWS = [
  { name: "overview", route: "#/", ready: "ul.scenarios" },
  { name: "changed scenario", route: `#/scenario/${SCENARIOS.refund}`, ready: ".decision" },
  { name: "recorded-only scenario", route: `#/scenario/${SCENARIOS.pending}`, ready: ".conversation" },
  { name: "runs", route: "#/runs", ready: "table.runs" },
  { name: "run timeline", route: `#/run/candidate/${SCENARIOS.refund}`, ready: "ol.timeline" },
  { name: "compare, empty", route: "#/compare", ready: ".pickers" },
  {
    name: "compare",
    route: `#/compare?a=baseline:${SCENARIOS.refund}&b=candidate:${SCENARIOS.refund}`,
    ready: ".diff",
  },
] as const;

async function show(page: Page, view: (typeof VIEWS)[number]): Promise<void> {
  await page.goto(`/${view.route}`);
  await expect(page.locator(view.ready)).toBeVisible();
}

/** How far the page scrolls sideways; 0 when everything fits the viewport. */
function overflow(page: Page): Promise<number> {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

test.describe("At phone width", () => {
  test.use({ viewport: { width: 375, height: 812 } });

  for (const view of VIEWS) {
    test(`${view.name} does not scroll sideways`, async ({ page }) => {
      await show(page, view);
      if (view.name === "run timeline") await page.getByRole("button", { name: "Expand all" }).click();
      if (view.name === "compare") await page.locator(".diff-toggle").first().click();
      expect(await overflow(page)).toBe(0);
    });
  }
});

test.describe("Accessibility", () => {
  for (const view of VIEWS) {
    test(`${view.name} has landmarks, one h1 and no serious axe violations`, async ({ page }) => {
      await show(page, view);
      await expect(page.getByRole("banner")).toHaveCount(1);
      await expect(page.getByRole("navigation", { name: "Views" })).toBeVisible();
      await expect(page.getByRole("main")).toHaveCount(1);
      await expect(page.getByRole("contentinfo")).toHaveCount(1);
      await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
      await expect(page.getByRole("radiogroup", { name: "Workspace" })).toBeVisible();

      // Includes button-name, link-name and label: every control has an accessible name.
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      const serious = results.violations
        .filter((v) => v.impact === "serious" || v.impact === "critical")
        .map((v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(" ")).join(", ")})`);
      expect(serious).toEqual([]);
    });
  }

  test("the current view is marked in the navigation", async ({ page }) => {
    await page.goto("/#/runs");
    const nav = page.getByRole("navigation", { name: "Views" });
    await expect(nav.getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
    await expect(nav.getByRole("link", { name: "Overview" })).not.toHaveAttribute("aria-current", "page");
  });
});
