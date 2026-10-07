import { expect, Page, BrowserContext, Browser } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { readFileSync, readdirSync } from "fs";
import path from "path";

export const CASE_DIR = process.env.E2E_CASE_DIR ?? "/tmp/ui-e2e-case";
export const CONTROL = "http://127.0.0.1:8501";
export const caseJson = () => JSON.parse(readFileSync(path.join(CASE_DIR, "case.json"), "utf8"));
export const docFiles = () => readdirSync(path.join(CASE_DIR, "docs")).filter((f) => f.endsWith(".pdf") && !f.includes("degraded")).map((f) => path.join(CASE_DIR, "docs", f));

export async function login(browser: Browser, user: string): Promise<{ ctx: BrowserContext; page: Page; errors: string[] }> {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error" && !/favicon|Failed to load resource.*(401|404)/.test(m.text())) errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  await page.goto("/login");
  await page.getByLabel("User name").fill(user);
  await page.getByLabel("Password").fill(process.env.DEMO_PW ?? "");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("link", { name: /Cases|Config/ }).first()).toBeVisible();
  return { ctx, page, errors };
}

export async function push(claimRef: string, kind: string, payload: unknown) {
  const r = await fetch(`${CONTROL}/control/push`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ claim_ref: claimRef, kind, payload }) });
  const j = await r.json();
  expect(j.status, JSON.stringify(j)).toBe(204);
}

/** Fail the test on serious or critical accessibility violations (the ones that block a keyboard or screen-reader user). */
export async function a11y(page: Page, label: string) {
  const res = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const bad = res.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(bad.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`), `axe on ${label}`).toEqual([]);
}

export async function shot(page: Page, name: string) { await page.screenshot({ path: `e2e/screens/${name}.png`, fullPage: true }); }
