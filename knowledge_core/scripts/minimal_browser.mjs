import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
const { chromium } = await import(process.env.KM_PLAYWRIGHT_MODULE || 'playwright');
const origin = process.env.KM_ACCEPTANCE_URL || 'http://127.0.0.1:18987';
const output = process.env.KM_EVIDENCE_DIR;
if (!output) throw Error('KM_EVIDENCE_DIR is required');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH });
const requests = [], errors = [];
let page;
try {
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on('request', request => { if (request.url().includes('/api/')) requests.push({ method: request.method(), url: request.url() }); });
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(origin);
  const capabilities = await (await page.request.get(origin + '/api/km/config')).json();
  if (capabilities.identity_required) {
    await page.getByLabel('访问令牌').fill('test');
    await page.getByRole('button', { name: '连接', exact: true }).click();
  }
  await page.getByLabel('Upload document').waitFor();
  if (capabilities.modules.spaces) {
    page.once('dialog', dialog => dialog.accept('Browser space'));
    await page.getByLabel('Create space').click();
    await page.getByLabel('Space', { exact: true }).locator('option').filter({ hasText: 'Browser space' }).waitFor({ state: 'attached' });
  }
  if (capabilities.modules.catalog) {
    page.once('dialog', dialog => dialog.accept('Browser catalog'));
    await page.getByLabel('Create knowledge base').click();
    await page.getByLabel('Knowledge base', { exact: true }).locator('option').filter({ hasText: 'Browser catalog' }).waitFor({ state: 'attached' });
  }
  if (process.env.KM_REQUIRE_REAL_MODEL === '1') {
    const selector = page.getByRole('combobox', { name: 'Model', exact: true });
    await selector.locator('option').first().waitFor({ state: 'attached' });
    await selector.selectOption({ index: 0 });
  }
  await page.getByLabel('Upload document').setInputFiles({ name: 'minimal-expense.md', mimeType: 'text/markdown',
    buffer: Buffer.from('# Expense policy\n\nExpense reimbursement requires original receipts within 30 days.') });
  await page.locator('.km-task').filter({ hasText: 'minimal-expense.md' }).getByText('succeeded', { exact: true }).waitFor({ timeout: 60000 });
  await page.getByRole('button', { name: /Expense policy.*chunks/ }).click();
  await page.getByLabel('Document content').fill('# Expense policy\n\nExpense reimbursement requires original receipts. Edited in standalone UI.');
  await Promise.all([page.waitForResponse(response => response.request().method() === 'PUT' && response.url().includes('/documents/') && response.status() === 200), page.getByLabel('Save document').click()]);
  await page.reload();
  if (capabilities.modules.catalog) await page.getByLabel('Knowledge base', { exact: true }).selectOption({ label: 'Browser catalog' });
  await page.getByRole('button', { name: /Expense policy.*chunks/ }).click();
  assert.match(await page.getByLabel('Document content').inputValue(), /Edited in standalone UI/);
  await page.getByLabel('Edit bucket', { exact: true }).first().click();
  await page.getByLabel('Layer content').fill('Expense receipts bucket edited in standalone UI.');
  await Promise.all([page.waitForResponse(response => response.request().method() === 'PUT' && response.url().includes('/buckets/') && response.ok()), page.getByLabel('Save content').click()]);
  await page.getByRole('dialog', { name: 'Edit bucket' }).waitFor({ state: 'hidden' });
  await page.getByLabel('Edit chunk', { exact: true }).first().click();
  await page.getByLabel('Layer content').fill('Expense receipts chunk edited in standalone UI.');
  await Promise.all([page.waitForResponse(response => response.request().method() === 'PUT' && response.url().includes('/chunks/') && response.ok()), page.getByLabel('Save content').click()]);
  await page.getByRole('dialog', { name: 'Edit chunk' }).waitFor({ state: 'hidden' });
  await page.getByLabel('Search knowledge').fill('Expense receipts');
  await page.getByLabel('Search', { exact: true }).click();
  await page.locator('.km-results article').first().waitFor();
  if (process.env.KM_REQUIRE_REAL_MODEL === '1') {
    const documents = await (await page.request.get(origin + '/api/km/v1/documents')).json();
    const ingestion = documents[0].metadata.route_trace;
    assert.ok(ingestion.some(row => row.phase === 'bucket_model' && row.valid_bucket_count > 0 && row.call_id));
    assert.ok(!ingestion.some(row => row.phase.includes('fallback')));
    const response = await page.request.post(origin + '/api/km/v1/query', { data: { query: 'Expense receipts', use_model: true } });
    const query = await response.json();
    assert.ok(query.chunks.length > 0 && query.route_trace.some(row => row.phase === 'document_route_model' && row.call_id));
    assert.ok(query.route_trace.some(row => row.phase === 'bucket_route_model' && row.selected_count > 0 && row.call_id));
    assert.ok(!query.route_trace.some(row => row.phase.includes('fallback')));
    await writeFile(output + '/browser-model.json', JSON.stringify({ status: 'passed', ingestion, query }, null, 2));
  }
  await page.locator('.km-results button').first().click();
  await page.getByRole('dialog', { name: 'Citation source' }).waitFor();
  assert.match(await page.getByRole('dialog').innerText(), /Expense/);
  await page.getByLabel('Close source').click();
  if (capabilities.modules.versions) {
    await page.getByLabel('Snapshot', { exact: true }).click();
    await page.getByLabel('Version', { exact: true }).locator('option').nth(1).waitFor({ state: 'attached' });
    await Promise.all([page.waitForResponse(response => response.request().method() === 'POST' && response.url().includes('/versions/publish') && response.ok()), page.getByLabel('Publish version', { exact: true }).click()]);
    await Promise.all([page.waitForResponse(response => response.request().method() === 'POST' && response.url().includes('/versions/rollback') && response.ok()), page.getByLabel('Rollback', { exact: true }).click()]);
    await page.getByRole('button', { name: /Expense policy.*chunks/ }).click();
    assert.match(await page.getByLabel('Document content').inputValue(), /Edited in standalone UI/);
  }
  if (capabilities.modules.concepts) {
    await page.getByRole('heading', { name: 'Concepts', exact: true }).waitFor();
    const downloaded = page.waitForEvent('download');
    await page.getByLabel('Export OKF').click();
    const bundle = await downloaded;
    assert.ok(await bundle.path());
  }
  await page.screenshot({ path: output + '/minimal-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(await page.locator('main.minimal-km').count(), 1);
  assert.equal(await page.getByRole('heading', { name: 'Knowledge', exact: true }).count(), 1);
  await page.screenshot({ path: output + '/minimal-mobile.png', fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.deepEqual(errors, []);
  const disabled = Object.entries({ spaces: /\/spaces|\/bindings/, catalog: /\/knowledge-bases/,
    versions: /\/versions|\/branch/, concepts: /\/concepts|\/okf/, discovery: /\/suggestions/, publication: /\/suggestions\/[^/]+\/publish(?:\?|$)/ });
  assert.ok(!requests.some(row => disabled.some(([name, pattern]) => !capabilities.modules[name] && pattern.test(row.url))));
  await writeFile(output + '/minimal-browser.json', JSON.stringify({ status: 'passed', origin, requests, errors,
    steps: ['upload', 'task_terminal', 'edit', 'bucket_edit', 'chunk_edit', 'reload', 'query', 'citation', 'desktop', 'mobile', 'no_disabled_requests'] }, null, 2));
} catch (error) {
  if (page) { await page.screenshot({ path: output + '/minimal-failure.png', fullPage: true }); await writeFile(output + '/minimal-failure.txt', await page.locator('body').innerText()); }
  await writeFile(output + '/minimal-browser.json', JSON.stringify({ status: 'failed', error: String(error), requests, errors }, null, 2));
  throw error;
} finally { await browser.close(); }
