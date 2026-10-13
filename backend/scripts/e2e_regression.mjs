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
 *   PW_DIR         含 playwright 的 node_modules 目录
 */

import { createRequire } from 'node:module';

const requireBase = process.env.PW_DIR ? process.env.PW_DIR.replace(/\/?$/, '/') + 'x.js' : import.meta.url;
const { chromium } = createRequire(requireBase)('playwright');

const BASE = process.env.MOCK_BASE || 'http://127.0.0.1:8899';
const HOTEL = process.env.INVOICE_HOTEL;
const TRAIN = process.env.INVOICE_TRAIN;
const OTHER = process.env.INVOICE_OTHER;

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
  await host.locator('button:has-text("代填")').first().click();
  await page.waitForTimeout(opts.wait || 9000);
  const frame = page.frames().find((f) => f.url().includes('/oa-form'));
  const oa = await frame.evaluate(() => {
    const hidden = (fm) => { const e = document.querySelector('[data-fieldmark="' + fm + '"] input[type=hidden]'); return e ? e.value : null; };
    const val = (sel) => { const e = document.querySelector(sel); return e ? (e.value !== undefined ? e.value : e.textContent) : null; };
    const files = (fm) => { const e = document.querySelector('[data-fieldmark="' + fm + '"] input[type=file]'); return e ? e.files.length : null; };
    return {
      expense: hidden('field6970'), taxExceeded: hidden('field7014'), travel: hidden('field6974'),
      reason: val('#field6946'), company: hidden('field6969'), costDept: hidden('field7012'),
      eticket: hidden('field29328'), relTickets: files('field6951'),
      scene0: hidden('field6926_0'), itype0: hidden('field6925_0'), desc0: val('#field6920_0'), amt0: val('#field6932_0'),
      scene1: hidden('field6926_1'), amt1: val('#field6932_1'),
      cpScene0: hidden('field6827_0'), cpScene1: hidden('field6827_1'),
      payee: (() => { const c = [...document.querySelectorAll('td')].find((t) => t.textContent.trim() === '收款单位名称'); if (!c || !c.nextElementSibling) return null; const h = c.nextElementSibling.querySelector('input[type=hidden]'); return h ? h.value : null; })(),
      cpItype0: hidden('field6826_0'), cpItype1: hidden('field6826_1'),
      cpDesc0: val('#field6821_0'), cpAmt0: val('#field6829_0'),
      cpDesc1: val('#field6821_1'), cpAmt1: val('#field6829_1'),
    };
  });
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
  const { oa, summary, notice, link } = await runScenario(page, host, 'company_payment', { files, wait: 16000, fill: { '收款单位名称': '上海测试科技' } });
  check('境内公司付款 「打开表单」链接→workflowid=56', /workflowid=56/.test(link), link);
  check('境内公司付款 顶部提示「收款单位」需在 OA 页面上二次确认', /「收款单位」需在 OA 页面上二次确认/.test(notice), notice);
  check('境内公司付款 失败=0', summary && summary.fail === 0, JSON.stringify(summary));
  check('境内公司付款 明细行1 报销场景=IT服务外包', oa.cpScene0 === 'IT服务外包', oa.cpScene0);
  check('境内公司付款 明细行2 存在', !!oa.cpScene1 || !!oa.cpAmt1, 'scene1=' + oa.cpScene1 + ' amt1=' + oa.cpAmt1);
  check('境内公司付款 明细行1 金额=750.00', oa.cpAmt0 === '750.00', oa.cpAmt0);
  check('境内公司付款 明细行1 事项描述', !!oa.cpDesc0, oa.cpDesc0);
  check('境内公司付款 行1 发票类型含 6%（多个结果时按税率选）', !!oa.cpItype0 && oa.cpItype0.indexOf('6%') >= 0, oa.cpItype0);
  check('境内公司付款 收款单位名称搜索选中全称', oa.payee === '上海测试科技有限公司', oa.payee);
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

await browser.close();

let failed = 0;
for (const r of results) {
  if (!r.ok) failed += 1;
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? '  [' + r.detail + ']' : ''}`);
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);