import { expect, Page, BrowserContext, Browser } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { createHmac } from "crypto";
import { readFileSync } from "fs";
import path from "path";

export type Seeded = { id: string; claim_no: string; ref: string; hospital_id: string };
export const seed = (): Record<"single" | "dual" | "query" | "settled", Seeded> =>
  JSON.parse(readFileSync(path.join(__dirname, "..", "..", "..", ".e2e-logs", "ui-seed.json"), "utf8"));

const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64url");
/** Development token the insurer API accepts when it runs with INS_ALLOW_DEV_TOKENS (the e2e stack does). */
export function mintToken(roles: string[]): string {
  const secret = process.env.INS_DEV_JWT_SECRET ?? "dev-insurer-api-jwt-secret-0000000000000000";
  const now = Math.floor(Date.now() / 1000);
  const head = b64({ alg: "HS256", typ: "JWT" });
  const body = b64({ sub: `ui-${roles[0]}`, roles, name: `UI ${roles[0]}`, aud: "insurer-api", iat: now, exp: now + 3600 });
  const sig = createHmac("sha256", secret).update(`${head}.${body}`).digest("base64url");
  return `${head}.${body}.${sig}`;
}

export async function login(browser: Browser, roles: string[]): Promise<{ ctx: BrowserContext; page: Page; errors: string[] }> {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error" && !/favicon|Failed to load resource.*(401|403|404)/.test(m.text())) errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  await page.goto("/login");
  await page.getByLabel("Token").fill(mintToken(roles));
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("status")).toHaveText("Signed in.");
  return { ctx, page, errors };
}

/** Fail the test on serious or critical accessibility violations (the ones that block a keyboard or screen-reader user). */
export async function a11y(page: Page, label: string) {
  const res = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const bad = res.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(bad.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`), `axe on ${label}`).toEqual([]);
}

export async function shot(page: Page, name: string) { await page.screenshot({ path: `e2e/screens/${name}.png`, fullPage: true }); }

/** Visible alerts only: Next.js adds its own role=alert route announcer to every page. */
export const alerts = (page: Page) => page.locator('[role="alert"]:not(#__next-route-announcer__)');
