import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

async function main() {
const { chromium } = await import(process.env.KM_PLAYWRIGHT_MODULE || 'playwright');

const origin = process.env.KM_ACCEPTANCE_URL || 'http://127.0.0.1:18787';
const browser = await chromium.launch({
  headless: process.env.HEADED !== '1',
  executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH || undefined,
});
const errors = [];
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on('pageerror', error => errors.push(error.message));
  if (process.env.KM_HOST_SESSION) {
    const { readFile } = await import('node:fs/promises');
    const session = JSON.parse(await readFile(process.env.KM_HOST_SESSION, 'utf8'));
    await page.addInitScript(value => {
      localStorage.setItem('ultrarag_auth', JSON.stringify(value));
      localStorage.setItem('staffdeck_onboarding_guide_seen', '1');
      localStorage.setItem('staffdeck_quick_start_guide_seen', '1');
      localStorage.setItem('ultrarag_enterprise_agent_scope', value.agentScope || 'employee');
    }, session);
  }
  await page.goto(origin + (process.env.KM_ACCEPTANCE_PATH || '/'));
  const result = await runKnowledgeAcceptance(page, { screenshotPrefix: process.env.KM_SCREENSHOT_PREFIX || '/tmp/km-acceptance' });
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ origin, ...result, pageErrors: errors }));
} finally { await browser.close(); }
}

export async function knowledgeSurface(page) {
  const outerPage = page;
  if (/\/knowledge(?:\/|$)/.test(page.url())) await page.locator('iframe[title="Knowledge"]').waitFor({ state: 'attached', timeout: 60000 });
  if (await outerPage.locator('iframe[title="Knowledge"]').count()) {
    await outerPage.frameLocator('iframe[title="Knowledge"]').getByLabel('新建知识库', { exact: true }).waitFor();
    const frame = () => outerPage.frames().find(item => item.url().includes('/km-ui/'));
    page = new Proxy({}, { get(_target, property) {
      if (property === 'reload') return async options => {
        await outerPage.reload(options);
        await outerPage.frameLocator('iframe[title="Knowledge"]').getByLabel('新建知识库', { exact: true }).waitFor();
      };
      const owner = ['goto', 'reload', 'screenshot', 'setViewportSize', 'waitForEvent', 'waitForResponse', 'waitForTimeout'].includes(property) ? outerPage : frame();
      const value = owner[property];
      return typeof value === 'function' ? value.bind(owner) : value;
    } });
  }
  return page;
}

export async function runKnowledgeAcceptance(page, { screenshotPrefix = '/tmp/km-acceptance', requireModelSelector = false, expectedModelId } = {}) {
  page = await knowledgeSurface(page);
  let selectedModel;
  if (requireModelSelector) {
    const selector = page.getByLabel('模型', { exact: true });
    await selector.waitFor({ state: 'visible', timeout: 10_000 });
    const options = await selector.locator('option').evaluateAll(rows => rows.map(row => ({ value: row.value, label: row.textContent })));
    assert.ok(options.length >= 2, `Hosted model selector has no configured model: ${JSON.stringify(options)}`);
    selectedModel = expectedModelId || options.find(option => option.value)?.value;
    assert.ok(selectedModel, 'Hosted model selector did not expose a selectable model');
    await selector.selectOption(selectedModel);
  }
  await page.getByLabel('新建知识库', { exact: true }).click();
  const name = 'Acceptance ' + Date.now();
  await page.getByRole('dialog').getByLabel('名称').fill(name);
  await page.getByRole('button', { name: '创建', exact: true }).click();
  await page.getByRole('heading', { name, exact: true }).waitFor();
  const uploaded = page.waitForResponse(response => response.request().method() === 'POST' && /\/documents(?:\?|$)/.test(response.url()));
  await page.locator('input[type=file]').first().setInputFiles({ name: 'expenses.md', mimeType: 'text/markdown',
    buffer: Buffer.from('# Expense policy\n\nExpense reimbursement requires receipts within 30 days.\n\n## Approval\n\nA manager approves every expense claim.') });
  const uploadResponse = await uploaded;
  assert.ok(uploadResponse.ok(), `Upload failed (${uploadResponse.status()}): ${await uploadResponse.text()}`);
  await page.locator('.job').filter({ hasText: 'expenses.md' }).getByText('完成', { exact: true }).waitFor({ timeout: 180000 });
  await page.locator('.document-list button').first().click();
  await page.getByLabel('文档正文').fill('# Expense policy\n\nExpense reimbursement requires original receipts within 30 days.');
  const saved = page.waitForResponse(response => response.request().method() === 'PUT' && /\/documents\//.test(response.url()), { timeout: 180000 });
  await page.getByLabel('保存正文', { exact: true }).click();
  const savedResponse = await saved;
  assert.ok(savedResponse.ok(), `Document edit failed (${savedResponse.status()}): ${await savedResponse.text()}`);
  await page.locator('.document-structure summary').click();
  await page.getByLabel('编辑主题', { exact: true }).first().click();
  await page.getByLabel('结构正文').fill('Expense reimbursement requires original receipts within 30 days. Topic edited.');
  await page.getByRole('button', { name: '保存结构', exact: true }).click();
  await page.getByLabel('结构正文').waitFor({ state: 'detached' });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('.base-row').filter({ hasText: name }).click();
  await page.getByRole('heading', { name, exact: true }).waitFor();
  await page.locator('.document-list button').first().click();
  await page.locator('.document-structure summary').click();
  await page.getByLabel('编辑主题', { exact: true }).first().click();
  assert.match(await page.getByLabel('结构正文').inputValue(), /Topic edited\./);
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await page.getByLabel('编辑片段', { exact: true }).first().click();
  await page.getByLabel('结构正文').fill('Expense reimbursement requires original receipts within 30 days. Chunk edited.');
  await page.getByRole('button', { name: '保存结构', exact: true }).click();
  await page.getByLabel('结构正文').waitFor({ state: 'detached' });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.locator('.base-row').filter({ hasText: name }).click();
  await page.getByRole('heading', { name, exact: true }).waitFor();
  await page.locator('.document-list button').first().click();
  await page.locator('.document-structure summary').click();
  await page.getByLabel('编辑片段', { exact: true }).first().click();
  assert.match(await page.getByLabel('结构正文').inputValue(), /Chunk edited\./);
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await page.getByRole('tab', { name: 'Wiki', exact: true }).click();
  await page.locator('.wiki-row').first().click();
  await page.getByRole('button', { name: '编辑', exact: true }).click();
  await page.getByLabel('Wiki 正文').fill('# Expense policy\n\nKeep original receipts.');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByRole('tab', { name: '检索', exact: true }).click();
  await page.getByLabel('检索问题').fill('Expense reimbursement original receipts');
  await page.getByRole('button', { name: '检索', exact: true }).click();
  await page.locator('.citation').first().click();
  await page.getByRole('dialog').getByRole('heading', { name: '引用来源' }).waitFor();
  assert.match(await page.getByRole('dialog').innerText(), /receipts/i);
  await page.getByLabel('关闭', { exact: true }).click();
  await page.getByRole('tab', { name: '版本', exact: true }).click();
  const populatedVersion = await page.locator('tbody tr').filter({ hasText: '当前' }).locator('td').first().innerText();
  await page.getByRole('button', { name: '复制', exact: true }).click();
  await page.locator('tbody tr').nth(1).waitFor();
  await page.getByRole('button', { name: '发布', exact: true }).click();
  await page.getByText('已发布', { exact: true }).waitFor();
  // Keep the populated draft when exercising rollback. The oldest version is
  // intentionally empty after creation, which would make the subsequent
  // graph/export checks validate an empty binding instead of the workflow.
  await page.locator('tbody tr').filter({ has: page.locator('td').getByText(populatedVersion, { exact: true }) }).getByRole('button', { name: '回滚', exact: true }).click();
  await page.getByRole('button', { name: '同步', exact: true }).click();
  await page.getByLabel('重命名知识库', { exact: true }).click();
  const renamed = name + ' renamed';
  await page.getByRole('dialog').getByLabel('名称').fill(renamed);
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click();
  await page.getByRole('heading', { name: renamed, exact: true }).waitFor();
  await page.getByLabel('删除知识库', { exact: true }).click();
  await page.getByRole('button', { name: '隐藏当前绑定', exact: true }).click();
  await page.locator('.base-row').filter({ hasText: renamed }).waitFor({ state: 'detached' });
  await page.getByLabel('恢复隐藏绑定', { exact: true }).click();
  await page.getByRole('dialog').locator('.section-title').filter({ hasText: renamed })
    .getByRole('button', { name: '恢复', exact: true }).click();
  await page.getByRole('heading', { name: renamed, exact: true }).waitFor();
  await page.getByRole('tab', { name: '关系图', exact: true }).click();
  await page.locator('.graph svg text').first().waitFor();
  assert.ok(await page.locator('svg text').count() > 0, 'Graph labels must render');
  const downloaded = page.waitForEvent('download');
  await page.getByLabel('导出 OKF', { exact: true }).click();
  const bundle = await downloaded;
  const archive = await bundle.path();
  assert.ok(archive, 'OKF export must produce a download');
  await page.getByRole('tab', { name: 'Wiki', exact: true }).click();
  await page.getByRole('button', { name: '检查', exact: true }).click();
  const { readFile } = await import('node:fs/promises');
  await page.locator('input[type=file]').nth(1).setInputFiles({
    name: bundle.suggestedFilename() || 'knowledge.okf.zip',
    mimeType: 'application/zip',
    buffer: await readFile(archive),
  });
  await page.getByRole('tab', { name: '文档', exact: true }).click();
  await page.locator('.document-list button').nth(1).waitFor();
  assert.ok(await page.locator('.document-list button').count() >= 2, 'OKF import must create a document');
  await page.screenshot({ path: screenshotPrefix + '-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('tab', { name: '文档', exact: true }).click();
  const overflow = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth,
    nodes: [...document.querySelectorAll('*')].filter(node => node.getBoundingClientRect().right > innerWidth + 1).slice(0, 15).map(node => ({ tag: node.tagName, class: node.className, text: node.textContent.slice(0, 100), right: node.getBoundingClientRect().right })) }));
  await page.screenshot({ path: screenshotPrefix + '-mobile.png', fullPage: true });
  assert.ok(overflow.scroll <= overflow.width, 'Mobile overflow: ' + JSON.stringify(overflow));
  await page.screenshot({ path: screenshotPrefix + '-mobile.png', fullPage: true });
  return { name, selectedModel, workflow: ['create', 'upload', 'edit', 'bucket-edit', 'chunk-edit', 'wiki-edit', 'search', 'citation', 'copy', 'publish', 'rollback', 'sync', 'rename', 'hide', 'restore', 'graph', 'okf-lint', 'okf-export', 'okf-import', 'mobile'] };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) await main();
