import { test, expect, Page } from "@playwright/test";
import { a11y, alerts, caseJson, docFiles, login, push, shot } from "./helpers";
import { randomUUID } from "crypto";

const state: { caseUrl?: string; claimRef?: string } = {};

test.describe.serial("hospital UI against the real stack", () => {
  test("login: wrong password is refused, protected pages redirect, sign-out works", async ({ page, browser }) => {
    await page.goto("/cases");
    await expect(page).toHaveURL(/\/login/);
    await page.getByLabel("User name").fill("desk1");
    await page.getByLabel("Password").fill("definitely-wrong");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(alerts(page).first()).toContainText(/Wrong user name or password|failed/i);
    await a11y(page, "login");
    await shot(page, "01-login");
    const { page: p, ctx } = await login(browser, "desk1");
    await expect(p.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    await p.getByRole("button", { name: "Sign out" }).click();
    await expect(p).toHaveURL(/\/login/);
    await ctx.close();
  });

  test("roles: desk sees case pages only, admin sees configuration only", async ({ browser }) => {
    const desk = await login(browser, "desk1");
    await expect(desk.page.getByRole("link", { name: "Config" })).toHaveCount(0);
    await expect(desk.page.getByRole("link", { name: "Queries" })).toBeVisible();
    await a11y(desk.page, "dashboard");
    await shot(desk.page, "02-dashboard");
    await desk.ctx.close();
    const admin = await login(browser, "hadmin");
    await expect(admin.page.getByRole("link", { name: "Cases" })).toHaveCount(0);
    await expect(admin.page.getByRole("link", { name: "Users" })).toBeVisible();
    await admin.ctx.close();
  });

  test("new case: validation errors, then create", async ({ browser }) => {
    const { page, ctx, errors } = await login(browser, "desk1");
    await page.goto("/cases/new");
    await page.getByRole("button", { name: "Create case" }).click();
    await expect(alerts(page).first()).toBeVisible();
    await a11y(page, "new case (errors)");
    const c = caseJson();
    const m = c.member, a = c.admission;
    await page.getByLabel("Hospital ID (UHID)").fill(m.uhid);
    await page.getByLabel("Full name").fill(m.full_name);
    await page.getByLabel("Date of birth").fill(m.dob);
    await page.getByLabel("Phone").fill("+91" + m.phone);
    await page.getByLabel("Insurer").fill("Acme Health");
    await page.getByLabel("Policy number").fill(m.policy_number);
    await page.getByLabel("Member ID").fill(m.member_id);
    await page.getByLabel("Admitted on").fill(a.admitted_on);
    await page.getByLabel("Discharged on").fill(a.discharged_on);
    await page.getByLabel("Treating doctor").fill(a.treating_doctor);
    await page.getByLabel("Pre-auth reference").fill("PA-2026-33001");
    await shot(page, "03-new-case");
    await page.getByRole("button", { name: "Create case" }).click();
    await expect(page).toHaveURL(/\/cases\/[0-9a-f-]+\/documents/);
    state.caseUrl = page.url().replace(/\/documents.*/, "");
    state.claimRef = (await page.getByRole("heading", { level: 1 }).first().innerText()).trim();
    expect(errors, "console errors").toEqual([]);
    await ctx.close();
  });

  test("documents: upload shows progress, statuses become Ready, delete dialog traps focus", async ({ browser }) => {
    const { page, ctx, errors } = await login(browser, "desk1");
    await page.goto(state.caseUrl! + "/documents");
    await page.locator('input[type="file"]').setInputFiles(docFiles());
    await expect(page.getByText("Uploaded").first()).toBeVisible();
    await expect(page.getByText(/^✓?\s*Ready$/)).toHaveCount(4, { timeout: 90_000 });
    await expect(page.getByText(/live/i).first()).toBeVisible(); // SSE indicator
    await a11y(page, "documents");
    await shot(page, "04-documents");
    // keyboard: the confirm dialog keeps focus inside and Escape closes it
    await page.getByRole("button", { name: "Delete" }).first().click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    for (let i = 0; i < 6; i++) { await page.keyboard.press("Tab"); expect(await dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true); }
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    expect(errors, "console errors").toEqual([]);
    await ctx.close();
  });

  test("checklist is complete; officer builds, edits, signs off and submits the claim", async ({ browser }) => {
    const { page, ctx, errors } = await login(browser, "officer1");
    await page.goto(state.caseUrl! + "/checklist");
    await expect(page.getByText(/^✓?\s*Complete$/)).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText("prescription").first()).toBeVisible(); // rows are named after the document, not "required"
    await a11y(page, "checklist");
    await shot(page, "05-checklist");
    await page.getByRole("button", { name: "Build claim" }).click();
    await page.goto(state.caseUrl! + "/claim");
    await expect(page.getByText(/Claim draft v1/)).toBeVisible({ timeout: 60_000 });
    await a11y(page, "claim");
    await shot(page, "06-claim");
    // edit a line description and save: a new version appears
    const desc = page.getByLabel("Description line 1", { exact: true });
    await desc.fill("Room rent general ward (edited)");
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByText(/Claim draft v2/)).toBeVisible();
    for (const box of await page.getByRole("checkbox").all()) await box.check(); // acknowledge any warnings
    await page.getByRole("button", { name: "Sign off" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Sign off" }).click();
    await expect(page.getByText("Signed off").first()).toBeVisible();
    await page.getByRole("button", { name: "Submit to insurer" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Submit" }).click();
    await page.goto(state.caseUrl! + "/submission");
    await expect(page.getByText("Acknowledged").first()).toBeVisible({ timeout: 60_000 });
    await a11y(page, "submission");
    await shot(page, "07-submission");
    expect(errors, "console errors").toEqual([]);
    await ctx.close();
  });

  test("query: insurer asks, officer drafts, approves and sends", async ({ browser }) => {
    const { page, ctx } = await login(browser, "officer1");
    await push(state.claimRef!, "queries", { query: { query_id: randomUUID(), round: 1, category: "billing_discrepancy", text: "Please explain the room rent charge billed.", requested_doc_types: [], due_by: "2030-01-01T00:00:00Z", status: "open", raised_by: "adj-1", grounding: [] } });
    await page.goto("/queries");
    await expect(page.getByRole("link", { name: state.claimRef! })).toBeVisible({ timeout: 30_000 });
    await a11y(page, "query inbox");
    await page.goto(state.caseUrl! + "/queries");
    await page.getByRole("button", { name: "Draft a reply" }).click();
    await expect(page.getByLabel("Reply")).not.toHaveValue("", { timeout: 60_000 });
    await shot(page, "08-query");
    await page.getByRole("button", { name: /Approve/ }).click();
    await page.getByRole("button", { name: "Send to insurer" }).click();
    await expect(page.getByText(/answered/i).first()).toBeVisible({ timeout: 30_000 });
    await ctx.close();
  });

  test("decision and settlement arrive and the case shows settled", async ({ browser }) => {
    const { page, ctx } = await login(browser, "officer1");
    const dec = { outcome: "approve", approved_amount: { amount: "1000.00" }, deductions: [], reason_codes: [], reviewer_ids: ["r1"], calc_trace_id: randomUUID(), policy_version: 6, decided_at: "2026-10-07T11:00:00Z" };
    await push(state.claimRef!, "decisions", { decision: dec });
    await push(state.claimRef!, "settlements", { settlement: { settlement_id: randomUUID(), amount: { amount: "1000.00" }, utr: "UTR" + randomUUID().slice(0, 8).toUpperCase(), paid_on: "2026-10-07", mode: "NEFT", tds: { amount: "0.00" } } });
    await page.goto(state.caseUrl! + "/timeline");
    await expect(page.getByText("settled").first()).toBeVisible({ timeout: 30_000 });
    await a11y(page, "timeline");
    await ctx.close();
  });

  test("admin pages load without errors", async ({ browser }) => {
    const { page, ctx, errors } = await login(browser, "hadmin");
    for (const [url, text, name] of [["/admin/config/doc_requirements", /Versions/, "config"], ["/admin/users", /Users/, "users"], ["/admin/outbox", /Outbox/, "outbox"]] as const) {
      await page.goto(url);
      await expect(page.getByText(text).first()).toBeVisible();
      await a11y(page, name);
      await shot(page, `09-admin-${name}`);
    }
    expect(errors, "console errors").toEqual([]);
    await ctx.close();
  });
});
