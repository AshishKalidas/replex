import { copyFileSync } from "node:fs";
import { expect, SCENARIOS, test } from "./console";

test.describe("Overview", () => {
  test("says how many scenarios need review and lists each with its verdict", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("2 scenarios need your review.");
    await expect(page.locator(".eyebrow")).toHaveText("Your project");

    const rows = page.locator("ul.scenarios > li");
    await expect(rows).toHaveCount(4);
    // Changed scenarios first, then unchanged, then never re-run.
    const expected = [
      [SCENARIOS.ineligible, "I want to return order 2001 for a refund, the watch is too big.", "Behaviour changed"],
      [SCENARIOS.refund, "I'd like a refund for order 1042 please", "Behaviour changed"],
      [SCENARIOS.status, "Where is my order 1043?", "Behaves as recorded"],
      [SCENARIOS.pending, "Can you tell me the status of order 1043?", "Not re-run yet"],
    ] as const;
    for (const [i, [name, said, verdict]] of expected.entries()) {
      const row = rows.nth(i);
      await expect(row.locator(".scenario-id")).toHaveText(name);
      await expect(row.locator(".scenario-said")).toContainText(said);
      await expect(row.locator(".status")).toHaveText(verdict);
    }
    await expect(rows.nth(0)).toContainText("Skips “Check refund policy” and uses “Search docs” instead.");
    await expect(rows.nth(3)).toContainText("Recorded. Not checked against a change yet.");
  });

  test("the primary button opens the first changed scenario", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("link", { name: "Start reviewing" }).click();
    await expect(page).toHaveURL(new RegExp(`#/scenario/${SCENARIOS.ineligible}$`));
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Refund ineligible openai");
  });

  test("the workspace switcher changes workspace and is remembered across reloads", async ({ page }) => {
    await page.goto("/");
    const switcher = page.getByRole("radiogroup", { name: "Workspace" });
    await expect(switcher.getByRole("radio", { name: "Project" })).toBeChecked();

    await switcher.getByRole("radio", { name: "Sample" }).click();
    await expect(switcher.getByRole("radio", { name: "Sample" })).toBeChecked();
    await expect(page.locator(".eyebrow")).toHaveText("Sample workspace · read-only");
    await expect(page.getByRole("link", { name: "See what changed" })).toBeVisible();
    await expect(page.locator(".lede")).toContainText("customer-support agent");

    await page.reload();
    await expect(page.getByRole("radio", { name: "Sample" })).toBeChecked();
    await expect(page.locator(".eyebrow")).toHaveText("Sample workspace · read-only");

    await page.getByRole("radio", { name: "Project" }).click();
    await page.reload();
    await expect(page.getByRole("radio", { name: "Project" })).toBeChecked();
    await expect(page.locator(".eyebrow")).toHaveText("Your project");
  });

  test("a link can open a workspace, and an unknown one falls back to the project", async ({ page }) => {
    await page.goto("/?workspace=sample-models");
    await expect(page.getByRole("radio", { name: "Model swap" })).toBeChecked();
    await expect(page.locator(".eyebrow")).toHaveText("Sample: model swap · read-only");
    await expect(page.locator(".lede")).toContainText("Claude Sonnet 5.5");

    // Switching moves the address with it, so a reload stays where the visitor is.
    await page.getByRole("radio", { name: "Project" }).click();
    await page.reload();
    await expect(page.getByRole("radio", { name: "Project" })).toBeChecked();

    await page.goto("/?workspace=nowhere");
    await expect(page.getByRole("radio", { name: "Project" })).toBeChecked();
  });

  test("a recording added while the page is open appears when the window regains focus", async ({ page, project }) => {
    await page.goto("/");
    const rows = page.locator("ul.scenarios > li");
    await expect(rows).toHaveCount(4);

    copyFileSync(project.baseline(SCENARIOS.status), project.baseline("order_status_again"));
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));

    await expect(rows).toHaveCount(5);
    await expect(page.locator(".scenario-id", { hasText: "order_status_again" })).toBeVisible();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("2 scenarios need your review.");
  });
});
