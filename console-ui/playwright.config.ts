import { defineConfig, devices } from "@playwright/test";

// End-to-end tests drive the built console (`npm run build`) served by a real `replex console`.
// Each worker starts its own server over a temporary project, so tests never touch the
// repository's files and can run in parallel.
export default defineConfig({
  testDir: "e2e",
  testMatch: "**/*.spec.ts",
  // exFAT / network volumes create AppleDouble "._*" files next to sources.
  testIgnore: "**/._*",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    ...devices["Desktop Chrome"],
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium" }],
});
