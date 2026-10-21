import { existsSync, readFileSync } from "node:fs";
import type { Page } from "@playwright/test";
import { expect, SCENARIOS, test } from "./console";

const name = SCENARIOS.refund;

function decisionRequests(page: Page): string[] {
  const seen: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST") seen.push(new URL(r.url()).pathname);
  });
  return seen;
}

test.describe("Scenario review", () => {
  test("tells the conversation before and after, marking skipped and new actions", async ({ page }) => {
    await page.goto(`/#/scenario/${name}`);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Refund request");
    await expect(page.locator(".eyebrow .status")).toHaveText("Behaviour changed");

    const conversation = page.getByRole("region", { name: "The conversation, before and after" });
    await expect(conversation.locator(".customer blockquote")).toContainText("I'd like a refund for order 1042");

    const before = conversation.locator(".side").filter({ has: page.getByRole("heading", { name: "Before your change" }) });
    const after = conversation.locator(".side").filter({ has: page.getByRole("heading", { name: "After your change" }) });
    await expect(before.locator(".action-name")).toHaveText(["Get order", "Check refund policy", "Create refund request"]);
    await expect(after.locator(".action-name")).toHaveText(["Get order", "Search docs", "Create refund request"]);
    await expect(before.locator(".action-removed")).toHaveText(/^Check refund policy\s*Skipped/);
    await expect(after.locator(".action-added")).toHaveText(/^Search docs\s*New/);
    await expect(page.getByRole("heading", { name: "Is this what you wanted?" })).toBeVisible();
  });

  test("accepting replaces the recording with the latest run", async ({ page, project }) => {
    const latest = readFileSync(project.candidate(name), "utf8");
    await page.goto(`/#/scenario/${name}`);

    await page.getByRole("button", { name: "Yes, accept the new behaviour" }).click();
    const confirm = page.getByRole("group", { name: "Confirm" });
    await expect(confirm).toContainText("Save the latest run as the new expected behaviour?");
    await confirm.getByRole("button", { name: "Accept", exact: true }).click();

    await expect(page.getByRole("status")).toContainText("Accepted. This is now the expected behaviour.");
    await expect(page.locator(".eyebrow .status")).toHaveText("Not re-run yet");
    await expect(page.getByRole("button", { name: "Yes, accept the new behaviour" })).toHaveCount(0);
    expect(existsSync(project.candidate(name))).toBe(false);
    expect(JSON.parse(readFileSync(project.baseline(name), "utf8"))).toEqual(JSON.parse(latest));
  });

  test("rejecting throws the latest run away and keeps the recording", async ({ page, project }) => {
    const recorded = readFileSync(project.baseline(name), "utf8");
    await page.goto(`/#/scenario/${name}`);

    await page.getByRole("button", { name: "No, reject it" }).click();
    const confirm = page.getByRole("group", { name: "Confirm" });
    await expect(confirm).toContainText("Throw away the latest run and keep the expected behaviour?");
    await confirm.getByRole("button", { name: "Reject", exact: true }).click();

    await expect(page.getByRole("status")).toContainText("Rejected. The latest run is gone");
    await expect(page.locator(".eyebrow .status")).toHaveText("Not re-run yet");
    expect(existsSync(project.candidate(name))).toBe(false);
    expect(readFileSync(project.baseline(name), "utf8")).toBe(recorded);
  });

  test("cancelling leaves both runs untouched", async ({ page, project }) => {
    const recorded = readFileSync(project.baseline(name), "utf8");
    const latest = readFileSync(project.candidate(name), "utf8");
    const posts = decisionRequests(page);
    await page.goto(`/#/scenario/${name}`);

    for (const answer of ["Yes, accept the new behaviour", "No, reject it"]) {
      await page.getByRole("button", { name: answer }).click();
      await page.getByRole("group", { name: "Confirm" }).getByRole("button", { name: "Cancel" }).click();
      await expect(page.getByRole("group", { name: "Confirm" })).toHaveCount(0);
      await expect(page.getByRole("button", { name: answer })).toBeVisible();
    }

    expect(posts).toEqual([]);
    expect(readFileSync(project.baseline(name), "utf8")).toBe(recorded);
    expect(readFileSync(project.candidate(name), "utf8")).toBe(latest);
  });

  test("a scenario that was never re-run shows the recorded conversation", async ({ page }) => {
    await page.goto(`/#/scenario/${SCENARIOS.pending}`);
    await expect(page.locator(".eyebrow .status")).toHaveText("Not re-run yet");
    await expect(page.getByRole("region", { name: "The recorded conversation" })).toContainText(
      "Can you tell me the status of order 1043?",
    );
    await expect(page.getByRole("heading", { name: "Is this what you wanted?" })).toHaveCount(0);
  });

  test("the sample workspace is read-only", async ({ page }) => {
    const posts = decisionRequests(page);
    await page.goto("/");
    await page.getByRole("radio", { name: "Sample" }).click();
    await expect(page.locator(".eyebrow")).toHaveText("Sample workspace · read-only");
    await page.goto(`/#/scenario/${name}`);

    await expect(page.getByRole("heading", { name: "Is this what you wanted?" })).toBeVisible();
    await expect(page.locator(".decision")).toContainText("This sample is read-only.");
    await expect(page.locator(".decision").getByRole("button")).toHaveCount(0);
    expect(posts).toEqual([]);
  });
});
