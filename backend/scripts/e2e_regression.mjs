/**
 * OA 代填 Playwright 回归测试（对着本地 Mock OA 跑所有报销场景）。
 *
 * 前提：
 *   1. 先起 mock：cd backend && uv run python scripts/mock_oa_server.py 8899
 *   2. 本机有 Playwright。用 createRequire 从 PW_DIR 解析（避免全局安装）：
 *        PW_DIR=/path/to/node_modules node backend/scripts/e2e_regression.mjs
 *      PW_DIR 指向含 playwright 的 node_modules 目录；默认从当前文件解析。
 *
 * 环境变量：
 *   MOCK_BASE      mock 地址，默认 http://127.0.0.1:8899
 *   INVOICE_HOTEL  6% 住宿发票 PDF 路径（用于金额/税率断言）
 *   INVOICE_TRAIN  9% 火车票 PDF 路径
 *   INVOICE_OTHER  票面类目任意（如软件服务费 6%）的 PDF 路径——回归「归不到场景也要出明细行」
 *   INVOICE_MAC    13% 电子专票 PDF 路径——回归「文字层先标签后数值，别把发票号码当金额」
 *   PW_DIR         含 playwright 的 node_modules 目录
 */

import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';

const requireBase = process.env.PW_DIR ? process.env.PW_DIR.replace(/\/?$/, '/') + 'x.js' : import.meta.url;
const { chromium } = createRequire(requireBase)('playwright');

const BASE = process.env.MOCK_BASE || 'http://127.0.0.1:8899';
const HOTEL = process.env.INVOICE_HOTEL;
const TRAIN = process.env.INVOICE_TRAIN;
const OTHER = process.env.INVOICE_OTHER;
const MAC = process.env.INVOICE_MAC;

const results = [];
function check(name, cond, detail) {
  results.push({ name, ok: !!cond, detail: detail === undefined ? '' : String(detail) });
}

async function injectPanel(page) {
  await page.goto(BASE + '/oa', { waitUntil: 'domcontentloaded' });
  await page.addScriptTag({ url: BASE + '/api/enterprise/oa-assistant/panel.js' });
  const host = page.locator('#sd-oa-panel-host');
  await host.waitFor({ state: 'attached' });
  return host;
}

async function readOa(page) {
  const frame = page.frames().find((f) => f.url().includes('/oa-form'));
  return frame.evaluate(() => {
    const hidden = (fm) => { const e = document.querySelector('[data-fieldmark="' + fm + '"] input[type=hidden]'); return e ? e.value : null; };
    const val = (sel) => { const e = document.querySelector(sel); return e ? (e.value !== undefined ? e.value : e.textContent) : null; };
    const files = (fm) => { const e = document.querySelector('[data-fieldmark="' + fm + '"] input[type=file]'); return e ? e.files.length : null; };
    return {
      expense: hidden('field6970'), taxExceeded: hidden('field7014'), travel: hidden('field6974'),
      reason: val('#field6946'), company: hidden('field6969'), costDept: hidden('field7012'),
      eticket: hidden('field29328'), relTickets: files('field6951'), otherTickets: files('field6973'),
      otherName: (() => { const e = document.querySelector('[data-fieldmark="field6973"] input[type=file]'); return e && e.files[0] ? e.files[0].name.slice(0, 12) : null; })(),
      scene0: hidden('field6926_0'), itype0: hidden('field6925_0'), desc0: val('#field6920_0'), amt0: val('#field6932_0'),
      scene1: hidden('field6926_1'), amt1: val('#field6932_1'),
      cpScene0: hidden('field6827_0'), cpScene1: hidden('field6827_1'),
      payee: (() => { const c = [...document.querySelectorAll('td')].find((t) => t.textContent.trim() === '收款单位名称'); if (!c || !c.nextElementSibling) return null; const h = c.nextElementSibling.querySelector('input[type=hidden]'); return h ? h.value : null; })(),
      cpItype0: hidden('field6826_0'), cpItype1: hidden('field6826_1'),
      cpDesc0: val('#field6821_0'), cpAmt0: val('#field6829_0'),
      cpDesc1: val('#field6821_1'), cpAmt1: val('#field6829_1'),
      relatedFlow: (() => { const c = [...document.querySelectorAll('td')].find((t) => t.textContent.trim() === '相关流程'); if (!c || !c.nextElementSibling) return null; const h = c.nextElementSibling.querySelector('input[type=hidden]'); return h ? h.value : null; })(),
      trace: window.__sdTrace || null,
      scene2: hidden('field6926_2'), amt2: val('#field6932_2'),
      btRows: document.querySelectorAll('[data-fieldmark^="field6926_"]').length,
      cpRows: document.querySelectorAll('[data-fieldmark^="field6827_"]').length,
    };
  });
}

async function runScenario(page, host, id, opts = {}) {
  await host.locator('select').first().selectOption(id);
  for (const [placeholder, value] of Object.entries(opts.fill || {})) {
    await host.locator(`input[placeholder*="${placeholder}"]`).first().fill(value);
  }
  if (opts.numberFirst !== undefined) {
    await host.locator('input[type=number]').first().fill(String(opts.numberFirst));
  }
  if (opts.files && opts.files.length) {
    await host.locator('input[type=file]').first().setInputFiles(opts.files);
    await page.waitForTimeout(2200);
  }
  if (opts.otherFiles && opts.otherFiles.length) {
    // 面板的第二个文件框 = 其他参考资料
    await host.locator('input[type=file]').nth(1).setInputFiles(opts.otherFiles);
    await page.waitForTimeout(1200);
  }
  if (opts.rowScene) {
    // 明细行「报销场景」下拉（境内公司付款有多个可选项：IT服务外包 / IT硬件设备）
    await host.evaluate((el, v) => {
      const sr = el.shadowRoot;
      const sel = [...sr.querySelectorAll('select')].find((s) => [...s.options].some((o) => o.value === v));
      if (sel) { sel.value = v; sel.dispatchEvent(new Event('change')); }
    }, opts.rowScene);
    await page.waitForTimeout(500);
  }
  if (opts.mergeOff) {
    // 「税率相同的合并到同一明细中」开关：点标签即切换，切换后面板会立刻重算明细
    await host.getByText('税率相同的合并到同一明细中').first().click();
    await page.waitForTimeout(1800);
  }
  // 重置 mock 侧埋点，便于断言「先清空再填」
  const resetFrame = page.frames().find((f) => f.url().includes('/oa-form'));
  if (resetFrame) await resetFrame.evaluate(() => window.__sdResetTrace && window.__sdResetTrace()).catch(() => {});
  await host.locator('button:has-text("代填")').first().click();
  await page.waitForTimeout(opts.wait || 9000);
  const oa = await readOa(page);
  const log = await host.evaluate((el) => {
    const box = [...el.shadowRoot.querySelectorAll('div')].find((d) => (d.style.fontFamily || '').includes('monospace'));
    return box ? box.textContent.replace(/\s+/g, ' ') : '';
  });
  const m = log.match(/完成：成功 (\d+) · 跳过 (\d+) · 失败 (\d+)/);
  const notice = await host.evaluate((el) => {
    const parts = [...el.shadowRoot.querySelectorAll('div')]
      .map((d) => d.textContent.trim())
      .filter((t) => t.startsWith('⚠'));
    return Array.from(new Set(parts)).join(' ');
  });
  const link = await host.evaluate((el) => {
    const a = [...el.shadowRoot.querySelectorAll('a')].find((x) => x.getAttribute('href'));
    return a ? a.getAttribute('href') : '';
  });
  return { oa, summary: m ? { ok: +m[1], skip: +m[2], fail: +m[3] } : null, log, notice, link };
}

const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage();

// 1) 通讯费用（单行表单，无明细）
{
  const host = await injectPanel(page);
  const { oa, summary, notice, link } = await runScenario(page, host, 'communication', { files: HOTEL ? [HOTEL] : [] });
  check('通讯报销 「打开表单」链接→workflowid=57', /workflowid=57/.test(link), link);
  check('通讯报销 顶部提示含「发票类型…二次确认」', /发票类型.*二次确认/.test(notice), notice);
  check('通讯报销 失败=0', summary && summary.fail === 0, JSON.stringify(summary));
  check('通讯报销 报销类型=公务限额费用', oa.expense === '公务限额费用', oa.expense);
  check('通讯报销 报销场景=通讯费用', oa.scene0 === '通讯费用', oa.scene0);
  check('通讯报销 费用承担部门=核定费用承担部门', oa.costDept === '核定费用承担部门', oa.costDept);
  check('通讯报销 发票类型由附件识别（含6%）', !!oa.itype0 && oa.itype0.indexOf('6%') >= 0, oa.itype0);
  check('通讯报销 相关票据=1', oa.relTickets === 1, oa.relTickets);
}

// 2) 交通报销（报销场景=飞机，默认）
{
  const host = await injectPanel(page);
  const { oa, summary, notice } = await runScenario(page, host, 'traffic', { files: HOTEL ? [HOTEL] : [] });
  check('交通报销 顶部提示含「发票类型…二次确认」', /发票类型.*二次确认/.test(notice), notice);
  check('交通报销 失败=0', summary && summary.fail === 0, JSON.stringify(summary));
  check('交通报销 报销场景=交通费用', oa.scene0 === '交通费用', oa.scene0);
  check('交通报销 发票类型由附件识别（含6%）', !!oa.itype0 && oa.itype0.indexOf('6%') >= 0, oa.itype0);
}

// 3) 出差报销（明细按 报销场景+税率 合并）
{
  const host = await injectPanel(page);
  const files = [HOTEL, TRAIN].filter(Boolean);
  const { oa, summary, notice } = await runScenario(page, host, 'business_trip', { files, numberFirst: '2', wait: 14000 });
  check('出差报销 顶部提示含「发票类型…二次确认」', /发票类型.*二次确认/.test(notice), notice);
  check('出差报销 失败=0', summary && summary.fail === 0, JSON.stringify(summary));
  check('出差报销 明细2行', !!oa.scene1, 'scene1=' + oa.scene1);
  check('出差报销 明细行1=境内住宿', oa.scene0 === '境内住宿', oa.scene0);
  check('出差报销 明细行2=境内火车票', oa.scene1 === '境内火车票', oa.scene1);
  check('出差报销 金额 750.00 / 87.00', oa.amt0 === '750.00' && oa.amt1 === '87.00', oa.amt0 + ' / ' + oa.amt1);
  check('出差报销 行1 发票类型含 6%', !!oa.itype0 && oa.itype0.indexOf('6%') >= 0, oa.itype0);
}

// 4) 境内公司付款（fieldmark 明细 + 加行）
{
  const host = await injectPanel(page);
  const files = [HOTEL, TRAIN].filter(Boolean);
  const { oa, summary, notice, link } = await runScenario(page, host, 'company_payment', { files, wait: 16000, fill: { '收款单位名称': '上海测试科技', '流程关键字': '0918' } });
  check('境内公司付款 「打开表单」链接→workflowid=56', /workflowid=56/.test(link), link);
  check('境内公司付款 顶部提示「收款单位」需在 OA 页面上二次确认', /「收款单位」需在 OA 页面上二次确认/.test(notice), notice);
  check('境内公司付款 失败=0', summary && summary.fail === 0, JSON.stringify(summary));
  check('境内公司付款 明细行1 报销场景=IT服务外包', oa.cpScene0 === 'IT服务外包', oa.cpScene0);
  check('境内公司付款 明细行2 存在', !!oa.cpScene1 || !!oa.cpAmt1, 'scene1=' + oa.cpScene1 + ' amt1=' + oa.cpAmt1);
  check('境内公司付款 明细行1 金额=750.00', oa.cpAmt0 === '750.00', oa.cpAmt0);
  check('境内公司付款 明细行1 事项描述', !!oa.cpDesc0, oa.cpDesc0);
  check('境内公司付款 行1 发票类型含 6%（多个结果时按税率选）', !!oa.cpItype0 && oa.cpItype0.indexOf('6%') >= 0, oa.cpItype0);
  check('境内公司付款 收款单位名称搜索选中全称', oa.payee === '上海测试科技有限公司', oa.payee);
  check('境内公司付款 相关流程 按关键字检索选中', /0918/.test(oa.relatedFlow || ''), oa.relatedFlow);
  const cpPanel = await host.evaluate((el) => {
    const sr = el.shadowRoot;
    const rowOf = (t) => [...sr.querySelectorAll('div')].find((d) => d.firstChild && d.firstChild.textContent === t);
    const sceneSel = [...sr.querySelectorAll('select')].find((s) => [...s.options].some((o) => o.value === 'IT服务外包'));
    return {
      flowOrder: rowOf('相关流程') && rowOf('相关流程').style.order,
      payeeOrder: rowOf('收款单位名称') && rowOf('收款单位名称').style.order,
      sceneOpts: sceneSel ? [...sceneSel.options].map((o) => o.value) : null,
    };
  });
  check('境内公司付款 相关流程 排在「收款单位名称」下面', Number(cpPanel.flowOrder) > Number(cpPanel.payeeOrder), cpPanel.flowOrder + ' > ' + cpPanel.payeeOrder);
  check('境内公司付款 明细「报销场景」多选一含 IT硬件设备', (cpPanel.sceneOpts || []).indexOf('IT硬件设备') >= 0, JSON.stringify(cpPanel.sceneOpts));
  check('境内公司付款 明细「报销场景」多选一含 公司通讯费', (cpPanel.sceneOpts || []).indexOf('公司通讯费') >= 0, JSON.stringify(cpPanel.sceneOpts));
}

// 5) 境内公司付款：票面类目任意的发票（阿里云「*生产生活服务*软件服务费」6%）也要出明细行
//    —— 回归：早先「没命中 住宿/机票/火车票 关键词」会把整张票丢掉，一行明细都不生成。
if (OTHER) {
  const host = await injectPanel(page);
  const { oa, summary, log } = await runScenario(page, host, 'company_payment', {
    files: [OTHER],
    wait: 12000,
    fill: { '收款单位名称': '上海测试科技' },
  });
  check('任意类目 失败=0', summary && summary.fail === 0, JSON.stringify(summary));
  check('任意类目 明细行1 报销场景=IT服务外包', oa.cpScene0 === 'IT服务外包', oa.cpScene0);
  check('任意类目 明细行1 金额=31922.46', oa.cpAmt0 === '31922.46', oa.cpAmt0);
  check('任意类目 明细行1 事项描述=IT服务外包费', oa.cpDesc0 === 'IT服务外包费', oa.cpDesc0);
  check('任意类目 行1 发票类型含 6%', !!oa.cpItype0 && oa.cpItype0.indexOf('6%') >= 0, oa.cpItype0);
  check('任意类目 无「未识别」提示', !/未能解析|未识别/.test(log), '');
}

// 6) 出差报销：任意类目发票 → 不崩，明确提示「报销场景需手动选」，金额/发票类型照填。
//    出差是「场景必填」的明细表，面板对空场景不猜、直接跳过并提示。
if (OTHER) {
  const host = await injectPanel(page);
  const { oa, summary, log } = await runScenario(page, host, 'business_trip', {
    files: [OTHER],
    wait: 12000,
  });
  check('出差+任意类目 失败=0', summary && summary.fail === 0, JSON.stringify(summary));
  check('出差+任意类目 金额仍填上', oa.amt0 === '31922.46', oa.amt0);
  check('出差+任意类目 行1 发票类型含 6%', !!oa.itype0 && oa.itype0.indexOf('6%') >= 0, oa.itype0);
  check('出差+任意类目 提示报销场景需手动选', /需要在 OA 页面手动选择/.test(log), '');
}

// 7) 合并开关（默认勾选）：两张同为 6% 的票 → 合并成 1 行、金额累计
if (OTHER) {
  const host = await injectPanel(page);
  const { oa } = await runScenario(page, host, 'company_payment', {
    files: [HOTEL, OTHER].filter(Boolean),
    wait: 12000,
    fill: { '收款单位名称': '上海测试科技' },
  });
  check('合并开关 勾选 → 行1=32672.46（6% 合并）', oa.cpAmt0 === '32672.46', oa.cpAmt0);
  check('合并开关 勾选 → 只有 1 行明细', !oa.cpAmt1, 'row2=' + oa.cpAmt1);
}

// 8) 合并开关取消 → 一张发票一行（OA 上两行各自金额）
if (OTHER) {
  const host = await injectPanel(page);
  const { oa } = await runScenario(page, host, 'company_payment', {
    files: [HOTEL, OTHER].filter(Boolean),
    wait: 12000,
    mergeOff: true,
    fill: { '收款单位名称': '上海测试科技' },
  });
  check('合并开关 取消 → 行1=750.00', oa.cpAmt0 === '750.00', oa.cpAmt0);
  check('合并开关 取消 → 行2=31922.46', oa.cpAmt1 === '31922.46', oa.cpAmt1);
  check('合并开关 取消 → 行2 报销场景=IT服务外包', oa.cpScene1 === 'IT服务外包', oa.cpScene1);
}

// 9) 境内公司付款：明细行的「报销场景」可以切到 IT硬件设备（事项描述跟着走）
{
  const host = await injectPanel(page);
  const { oa } = await runScenario(page, host, 'company_payment', {
    files: HOTEL ? [HOTEL] : [],
    wait: 14000,
    rowScene: 'IT硬件设备',
    fill: { '收款单位名称': '上海测试科技', '流程关键字': '0918' },
  });
  check('境内公司付款 明细行1 报销场景=IT硬件设备', oa.cpScene0 === 'IT硬件设备', oa.cpScene0);
  check('境内公司付款 行1 事项描述随场景=IT硬件设备费', oa.cpDesc0 === 'IT硬件设备费', oa.cpDesc0);
  check('境内公司付款 未填发票类型时按税率预选（含6%）', !!oa.cpItype0 && oa.cpItype0.indexOf('6%') >= 0, oa.cpItype0);
}

// 10) 13% 电子专票（文字层「先整列标签、再整列数值」）：金额不能被发票号码/日期串掉
if (MAC) {
  const { total } = await (async () => {
    const res = await fetch(BASE + '/api/enterprise/oa-assistant/invoice-amounts', {
      method: 'POST',
      body: (() => {
        const fd = new FormData();
        fd.append('files', new Blob([readFileSync(MAC)]), 'mac.pdf');
        return fd;
      })(),
    });
    return res.json();
  })();
  check('13% 电子专票 金额=13925.00（不是发票号码串）', total === '13925.00', total);

  const host = await injectPanel(page);
  const { oa } = await runScenario(page, host, 'company_payment', {
    files: [MAC],
    wait: 14000,
    rowScene: 'IT硬件设备',
    fill: { '收款单位名称': '上海测试科技', '流程关键字': '0918' },
  });
  check('13% 电子专票 明细金额=13925.00', oa.cpAmt0 === '13925.00', oa.cpAmt0);
  check('13% 电子专票 发票类型含 13%', !!oa.cpItype0 && oa.cpItype0.indexOf('13%') >= 0, oa.cpItype0);
  check('13% 电子专票 报销场景=IT硬件设备', oa.cpScene0 === 'IT硬件设备', oa.cpScene0);
}

// 11) 先清空再代填：文本先退格、下拉先清已选、附件先清空再挂
{
  const host = await injectPanel(page);
  const { oa, summary } = await runScenario(page, host, 'communication', { files: HOTEL ? [HOTEL] : [] });
  const keys = (oa.trace && oa.trace.backspace) || {};
  check('先清空再填 报销事由收到退格键', (keys['field6946'] || 0) >= 1, JSON.stringify(keys));
  check('先清空再填 下拉搜索框收到退格键', (keys['ant-select-search__field'] || 0) >= 1, JSON.stringify(keys));
  check('先清空再填 附件已挂上（change 触发）', oa.trace && oa.trace.fileChanges >= 1, oa.trace && oa.trace.fileChanges);
  check('先清空再填 附件最终仍=1', oa.relTickets === 1, oa.relTickets);
  check('先清空再填 报销场景=通讯费用', oa.scene0 === '通讯费用', oa.scene0);
  check('先清空再填 失败=0', summary && summary.fail === 0, JSON.stringify(summary));

  // 同一个面板、同一个 OA 表单上再点一次「代填」（用户填错了想重来）
  const frame2 = page.frames().find((f) => f.url().includes('/oa-form'));
  if (frame2) await frame2.evaluate(() => window.__sdResetTrace && window.__sdResetTrace()).catch(() => {});
  await host.locator('button:has-text("代填")').first().click();
  await page.waitForTimeout(16000);
  const again = await frame2.evaluate(() => {
    const hidden = (fm) => { const e = document.querySelector('[data-fieldmark="' + fm + '"] input[type=hidden]'); return e ? e.value : null; };
    return {
      scene0: hidden('field6926_0'),
      reason: (() => { const e = document.querySelector('#field6946'); return e ? e.value : null; })(),
      relTickets: (() => { const e = document.querySelector('[data-fieldmark="field6951"] input[type=file]'); return e ? e.files.length : null; })(),
      trace: window.__sdTrace || null,
    };
  });
  check('重复代填 报销场景仍是通讯费用（不是空/叠加）', again.scene0 === '通讯费用', again.scene0);
  check('重复代填 报销事由不叠加', again.reason === oa.reason, again.reason + ' vs ' + oa.reason);
  check('重复代填 相关票据仍=1（不叠加）', again.relTickets === 1, again.relTickets);
  check('重复代填 第二次也先清空附件', again.trace && again.trace.fileCleared >= 1, again.trace && again.trace.fileCleared);
}

// 12) 重新代填：先把上次生成的明细行删掉（2 行 ×2 次 ≠ 4 行）
if (HOTEL && TRAIN) {
  const host = await injectPanel(page);
  const files = [HOTEL, TRAIN];
  const first = await runScenario(page, host, 'business_trip', { files, numberFirst: '2', wait: 14000 });
  check('重复代填 第一次 2 行', first.oa.btRows === 2, 'btRows=' + first.oa.btRows);
  // 首次代填时 OA 上的初始明细行是空的 → 不该去删它（只删「有内容」的旧行）
  check('首次代填 不删空行（日志无「已删除」）', !/已删除/.test(first.log), first.log.slice(-120));
  // 同一个面板、同一个 OA 表单上再点一次「代填」
  const fr = page.frames().find((f) => f.url().includes('/oa-form'));
  await host.locator('button:has-text("代填")').first().click();
  await page.waitForTimeout(24000);
  const again = await readOa(page);
  const log2 = await host.evaluate((el) => {
    const box = [...el.shadowRoot.querySelectorAll('div')].find((d) => (d.style.fontFamily || '').includes('monospace'));
    return box ? box.textContent.replace(/\s+/g, ' ') : '';
  });
  check('重复代填 明细仍是 2 行（旧行已删）', again.btRows === 2, 'btRows=' + again.btRows);
  check('重复代填 行1/行2 金额仍是 750.00 / 87.00', again.amt0 === '750.00' && again.amt1 === '87.00', again.amt0 + ' / ' + again.amt1);
  check('重复代填 日志出现「已删除 N 行已有明细」', /已删除 \d+ 行已有明细/.test(log2), log2.slice(-160));
  check('删行前先勾选该行（日志有「已勾选第 N 行」）', /已勾选第 \d+ 行/.test(log2), log2.slice(-200));
}

// 13) 需要更少的行时（2 行 → 1 行）：多出来的那行要被「-」删掉
if (HOTEL && OTHER) {
  const host = await injectPanel(page);
  const files = [HOTEL, OTHER];
  const first = await runScenario(page, host, 'company_payment', {
    files, wait: 16000, mergeOff: true, fill: { '收款单位名称': '上海测试科技' },
  });
  check('明细 2 行 → 1 行：第一次 2 行', first.oa.cpRows === 2, 'cpRows=' + first.oa.cpRows);
  // 勾回「税率相同的合并到同一明细中」→ 只需要 1 行
  await host.getByText('税率相同的合并到同一明细中').first().click();
  await page.waitForTimeout(1800);
  await host.locator('button:has-text("代填")').first().click();
  await page.waitForTimeout(24000);
  const second = await readOa(page);
  check('明细 2 行 → 1 行：第二行已被删除', second.cpRows === 1, 'cpRows=' + second.cpRows);
  // 点是「明细的 -」而不是页面上别的删除键（附件的 × 就踩过这个坑）
  const logSecond = await host.evaluate((el) => {
    const box = [...el.shadowRoot.querySelectorAll('div')].find((d) => (d.style.fontFamily || '').includes('monospace'));
    return box ? box.textContent.replace(/\s+/g, ' ') : '';
  });
  check('明细删除 点的是明细按钮行的 delbutton', /点「-」：<i#delbutton0/.test(logSecond), (logSecond.match(/点「-」：[^－]{0,80}/) || [''])[0]);
  check('明细删除 弹窗已确认（日志有「已删除 N 行已有明细」）', /已删除 \d+ 行已有明细/.test(logSecond), '');
  check('明细 2 行 → 1 行：行1=32672.46', second.cpAmt0 === '32672.46', second.cpAmt0);
  check('明细 2 行 → 1 行：第二行金额已清空', !second.cpAmt1, second.cpAmt1);
}

// 14) 境内公司付款：明细行可切到「公司通讯费」（事项描述不重复拼「费」）
if (HOTEL) {
  const host = await injectPanel(page);
  const { oa } = await runScenario(page, host, 'company_payment', {
    files: [HOTEL],
    wait: 14000,
    rowScene: '公司通讯费',
    fill: { '收款单位名称': '上海测试科技' },
  });
  check('境内公司付款 明细行1 报销场景=公司通讯费', oa.cpScene0 === '公司通讯费', oa.cpScene0);
  check('境内公司付款 行1 事项描述=公司通讯费（不拼成「费费」）', oa.cpDesc0 === '公司通讯费', oa.cpDesc0);
}

// 15) 两个附件框都要挂上，且不能把同一轮刚挂上的票据删掉
if (HOTEL && TRAIN) {
  const host = await injectPanel(page);
  const { oa, log } = await runScenario(page, host, 'company_payment', {
    files: [HOTEL, TRAIN],
    otherFiles: [HOTEL],
    wait: 22000,
    fill: { '收款单位名称': '上海测试科技' },
  });
  check('相关票据 2 张都在（没被误删）', oa.relTickets === 2, oa.relTickets);
  check('其他参考资料 1 个也挂到 OA 了', oa.otherTickets === 1, oa.otherTickets);
  check('其他参考资料 文件名正确', oa.otherName === 'dzfp_2632200', String(oa.otherName));
  check('两次附件都报了「OA 列表 N 个」核对', /相关票据[^－]{0,60}OA 列表 2 个/.test(log) && /其他参考资料[^－]{0,60}OA 列表 1 个/.test(log), (log.match(/已提交 [^－]{0,90}/g) || []).join(' | '));
  check('明细 2 行（也没被误删）', oa.cpRows === 2, oa.cpRows);
}

await browser.close();

let failed = 0;
for (const r of results) {
  if (!r.ok) failed += 1;
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? '  [' + r.detail + ']' : ''}`);
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);