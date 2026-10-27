// UI smoke test: register, start a run, follow it live, open the deliverable, use the palette.
// Fails on any page error or console error (CSP violations included).
const { chromium } = require('playwright');
const BASE = process.env.E2E_BASE ?? 'http://127.0.0.1:8080';
const SHOTS = process.env.E2E_SHOTS;
const stamp = Date.now();
(async () => {
  const browser = await chromium.launch();
  const errors = [];
  for (const theme of ['light', 'dark']) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: theme });
    const page = await ctx.newPage();
    page.on('pageerror', (e) => errors.push(`${theme}: ${e.message}`));
    page.on('console', (m) => { if (m.type() === 'error') errors.push(`${theme} console: ${m.text()}`); });
    const email = `e2e-${theme}-${stamp}@example.com`;
    await page.goto(`${BASE}/register`);
    await page.getByLabel('Name').fill('E2E ' + theme);
    await page.getByLabel('Email').fill(email);
    await page.getByLabel('Password').fill('correct horse battery staple');
    await page.getByRole('button', { name: 'Create account' }).click();
    await page.waitForURL(`${BASE}/`);
    await page.getByLabel('Topic').waitFor();
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/home-${theme}.png`, fullPage: true });
    await page.getByLabel('Topic').fill('Solid-state batteries in 2026');
    await page.getByLabel('Description').fill('Focus on energy density, cost and commercial timelines.');
    await page.getByRole('button', { name: /PDF/ }).click();
    await page.getByRole('button', { name: 'Start run' }).click();
    await page.waitForURL(/\/runs\/[0-9a-f-]+$/);
    await page.getByText('Completed', { exact: true }).waitFor({ timeout: 20000 });
    await page.waitForTimeout(500);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/run-${theme}.png` });
    const feedCount = await page.locator('[role=feed] article').count();
    await page.getByRole('link', { name: 'Open deliverable' }).click();
    await page.getByRole('heading', { name: /Solid-state batteries/ }).first().waitFor();
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/output-${theme}.png` });
    await page.keyboard.press('Control+k');
    await page.getByPlaceholder('Search pages, runs, actions…').waitFor();
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/palette-${theme}.png` });
    console.log(theme, 'ok, timeline items:', feedCount);
    await ctx.close();
  }
  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const mp = await mobile.newPage();
  await mp.goto(`${BASE}/login`);
  await mp.getByLabel('Email').fill(`e2e-light-${stamp}@example.com`);
  await mp.getByLabel('Password').fill('correct horse battery staple');
  await mp.getByRole('button', { name: 'Sign in' }).click();
  await mp.waitForURL(`${BASE}/`);
  if (SHOTS) await mp.screenshot({ path: `${SHOTS}/home-mobile.png`, fullPage: true });
  const overflow = await mp.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  console.log('mobile horizontal overflow:', overflow);
  if (overflow) throw new Error('horizontal overflow on mobile');
  if (errors.length) throw new Error('browser errors: ' + JSON.stringify(errors));
  await browser.close();
})().catch((e) => { console.error('E2E FAILED', e); process.exit(1); });
