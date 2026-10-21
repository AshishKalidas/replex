import { expect, SCENARIOS, test } from "./console";

test.describe("Runs", () => {
  test("search and filters narrow the table", async ({ page }) => {
    await page.goto("/#/runs");
    const rows = page.locator("table.runs tbody tr");
    await expect(rows).toHaveCount(7);
    await expect(page.locator(".stat")).toHaveText(["4Recorded runs", "3Latest runs", "2Behaviour changed"]);

    const search = page.getByRole("searchbox", { name: "Search runs" });
    await search.fill("search_docs");
    await expect(rows.locator("code")).toHaveText([SCENARIOS.ineligible, SCENARIOS.refund]);

    await search.fill("mock-mimo");
    await expect(rows).toHaveCount(3);

    await search.fill("");
    const filters = page.getByRole("radiogroup", { name: "Filter runs" });
    await filters.getByRole("radio", { name: "Latest runs" }).click();
    await expect(rows).toHaveCount(3);
    await expect(rows.locator(".kind")).toHaveText(["Latest run", "Latest run", "Latest run"]);

    await filters.getByRole("radio", { name: "Recorded" }).click();
    await expect(rows).toHaveCount(4);

    await search.fill("no such run");
    await expect(page.getByRole("heading", { name: "Nothing matches." })).toBeVisible();
  });

  test("a search can be linked to", async ({ page }) => {
    await page.goto("/#/runs?q=refund");
    await expect(page.getByRole("searchbox", { name: "Search runs" })).toHaveValue("refund");
    await expect(page.locator("table.runs tbody tr")).toHaveCount(4);
  });

  test("keyboard: / focuses search, j and k move, Enter opens", async ({ page }) => {
    await page.goto("/#/runs");
    const rows = page.locator("table.runs tbody tr");
    const search = page.getByRole("searchbox", { name: "Search runs" });
    await expect(rows).toHaveCount(7);

    await page.keyboard.press("/");
    await expect(search).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(search).not.toBeFocused();

    await expect(rows.nth(0)).toHaveClass(/row-active/);
    await page.keyboard.press("j");
    await page.keyboard.press("j");
    await expect(rows.nth(2)).toHaveClass(/row-active/);
    await page.keyboard.press("k");
    await expect(rows.nth(1)).toHaveClass(/row-active/);
    await expect(rows.nth(1).locator("code")).toHaveText(SCENARIOS.status);

    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(new RegExp(`#/run/baseline/${SCENARIOS.status}$`));
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(SCENARIOS.status);
  });

  test("keyboard: the overview opens a scenario with j and Enter", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator("ul.scenarios > li")).toHaveCount(4);
    await page.keyboard.press("j");
    await expect(page.locator("ul.scenarios > li").nth(1)).toHaveClass(/row-active/);
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(new RegExp(`#/scenario/${SCENARIOS.refund}$`));
  });
});

test.describe("Run timeline", () => {
  test("steps expand one at a time, by keyboard, or all at once", async ({ page }) => {
    await page.goto(`/#/run/candidate/${SCENARIOS.refund}`);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(SCENARIOS.refund);
    await expect(page.locator(".panel")).toContainText("get_order›search_docs›create_refund_request");

    const steps = page.locator("ol.timeline .step-head");
    await expect(steps).toHaveCount(7);
    await expect(page.locator(".step-body")).toHaveCount(0);

    await steps.nth(1).click();
    await expect(steps.nth(1)).toHaveAttribute("aria-expanded", "true");
    await expect(page.locator(".step-body")).toHaveCount(1);
    await expect(page.locator(".step-body")).toContainText("Arguments");

    await page.keyboard.press("j");
    await page.keyboard.press("Enter");
    await expect(steps.nth(2)).toHaveAttribute("aria-expanded", "true");

    await page.getByRole("button", { name: "Expand all" }).click();
    await expect(page.locator(".step-body")).toHaveCount(7);
    await page.getByRole("button", { name: "Collapse all" }).click();
    await expect(page.locator(".step-body")).toHaveCount(0);
  });

  test("links to the comparison with the recorded run", async ({ page }) => {
    await page.goto(`/#/run/candidate/${SCENARIOS.refund}`);
    await page.getByRole("link", { name: "Compare with the recorded run" }).click();
    await expect(page).toHaveURL(/#\/compare\?a=baseline%3Arefund_request&b=candidate%3Arefund_request$/);
    await expect(page.getByRole("list", { name: "Aligned steps" })).toBeVisible();
  });
});

test.describe("Compare", () => {
  test("pickers choose two runs and their steps line up", async ({ page }) => {
    await page.goto("/#/compare");
    await expect(page.getByRole("heading", { name: "Choose two runs." })).toBeVisible();

    await page.getByLabel("Before").selectOption(`baseline:${SCENARIOS.refund}`);
    await page.getByLabel("After").selectOption(`candidate:${SCENARIOS.refund}`);
    await expect(page).toHaveURL(/#\/compare\?a=baseline%3Arefund_request&b=candidate%3Arefund_request$/);

    const aligned = page.getByRole("list", { name: "Aligned steps" });
    await expect(aligned.getByRole("listitem")).toHaveCount(8);
    await expect(page.locator(".legend .status")).toHaveText(["3 unchanged", "3 changed", "1 added", "1 removed"]);
    await expect(page.getByRole("link", { name: "Breaks the policy" })).toBeVisible();

    const removed = aligned.getByRole("button", { name: /^removed: check_refund_policy/ });
    const added = aligned.getByRole("button", { name: /^added: search_docs/ });
    await expect(removed).toBeVisible();
    await expect(added).toBeVisible();

    await removed.click();
    await expect(removed).toHaveAttribute("aria-expanded", "true");
    const details = aligned.locator(".diff-removed .diff-details");
    await expect(details).toContainText("Not in the second run");

    await page.keyboard.press("j");
    await page.keyboard.press("Enter");
    await expect(added).toHaveAttribute("aria-expanded", "true");
    await expect(aligned.locator(".diff-added .diff-details")).toContainText("Not in the first run");
  });
});
