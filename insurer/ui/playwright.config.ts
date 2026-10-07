import { defineConfig } from "@playwright/test";

// Drives the system Chrome against the real hospital + insurer stack started by `make ins-ui-e2e` (scripts/ins_ui_e2e.sh).
export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  workers: 1,
  fullyParallel: false,
  reporter: [["list"]],
  outputDir: "./e2e/results",
  use: { baseURL: "http://localhost:3600", channel: "chrome", headless: true, screenshot: "only-on-failure", trace: "retain-on-failure" },
});
