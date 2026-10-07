import { defineConfig } from "@playwright/test";

// Drives the system Chrome against the real stack started by `make ui-e2e` (scripts/ui_e2e.sh): no browser download.
export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  workers: 1,
  fullyParallel: false,
  reporter: [["list"]],
  outputDir: "./e2e/results",
  use: { baseURL: "http://localhost:3100", channel: "chrome", headless: true, screenshot: "only-on-failure", trace: "retain-on-failure" },
});
