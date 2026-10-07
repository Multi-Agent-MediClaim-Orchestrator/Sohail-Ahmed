import { test, expect } from "@playwright/test";
import { a11y, alerts, login, seed, shot } from "./helpers";

test.describe.configure({ mode: "serial" });

test("sign in shows the roles; without a token the API is closed", async ({ browser }) => {
  const anon = await browser.newContext();
  const p = await anon.newPage();
  await p.goto("/cases");
  await expect(alerts(p)).toContainText("Could not load cases");
  await anon.close();
  const { ctx, page, errors } = await login(browser, ["reviewer"]);
  await page.goto("/");
  await expect(page.getByText("Signed in roles: reviewer")).toBeVisible();
  await a11y(page, "home");
  expect(errors).toEqual([]);
  await ctx.close();
});

test("cases list shows the seeded claims and filters by status", async ({ browser }) => {
  const s = seed();
  const { ctx, page, errors } = await login(browser, ["reviewer"]);
  await page.goto("/cases");
  for (const k of ["single", "dual", "query", "settled"] as const) await expect(page.getByRole("link", { name: s[k].claim_no })).toBeVisible();
  await page.getByLabel("Status").selectOption("ready_for_decision");
  await expect(page.getByRole("link", { name: s.single.claim_no })).toBeVisible();
  await expect(page.getByRole("link", { name: s.settled.claim_no })).toHaveCount(0);
  await a11y(page, "cases");
  await shot(page, "cases");
  expect(errors).toEqual([]);
  await ctx.close();
});

test("workspace shows the claim, verification steps, calculation and a live stream", async ({ browser }) => {
  const s = seed();
  const { ctx, page, errors } = await login(browser, ["reviewer"]);
  await page.goto(`/cases/${s.single.id}`);
  await expect(page.getByRole("heading", { name: s.single.claim_no })).toBeVisible();
  for (const label of ["Fetch", "Completeness", "Identity", "Authenticity", "Coverage", "Calculation"]) await expect(page.getByRole("button", { name: new RegExp(label) })).toBeVisible();
  await expect(page.getByRole("table", { name: "Calculation breakdown" })).toBeVisible();
  await expect(page.getByText("Total payable")).toBeVisible();
  await expect(page.getByText("live", { exact: true })).toBeVisible();
  await a11y(page, "workspace");
  await shot(page, "workspace");
  expect(errors).toEqual([]);
  await ctx.close();
});
