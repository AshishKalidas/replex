import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { expect, replex, SCENARIOS, test } from "./console";

test.describe("Single-file HTML report", () => {
  test("renders the overview from file:// with no network requests", async ({ page, project }) => {
    const out = join(project.root, "replex-report.html");
    await replex(
      ["report", project.baselineDir, project.candidateDir, "--policy", project.policy, "--html", out],
      project.root,
    );

    const requests: string[] = [];
    page.on("request", (r) => {
      if (!r.url().startsWith("file:")) requests.push(r.url());
    });
    await page.goto(pathToFileURL(out).href);

    await expect(page.getByRole("heading", { level: 1 })).toHaveText("2 scenarios need your review.");
    const rows = page.locator("ul.scenarios > li");
    await expect(rows).toHaveCount(3);
    await expect(rows.locator(".scenario-id")).toHaveText([SCENARIOS.ineligible, SCENARIOS.refund, SCENARIOS.status]);
    await expect(page.getByRole("radiogroup", { name: "Workspace" })).toHaveCount(0);

    await page.getByRole("link", { name: "Start reviewing" }).click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Refund ineligible openai");
    await expect(page.locator(".decision")).toContainText("replex approve");
    expect(requests).toEqual([]);
  });
});
