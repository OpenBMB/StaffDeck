/**
 * StaffDeck OA 录制器（开发排查用）。
 *
 * 用法：在 OA 页面控制台执行（host 用面板加载的同源地址）
 *   var s=document.createElement('script');
 *   s.src=window.__SD_OA_BASE__+'/api/enterprise/oa-assistant/recorder.js?t='+Date.now();
 *   document.documentElement.appendChild(s);
 * 然后在页面上正常操作（点「报销场景」→ 选选项、点「+」加行…），最后执行
 *   window.__SD_RECORDER__.dump()
 * 把打印/复制出来的 JSON 发回即可（含页面结构 + 每一步点击/选择）。
 *
 * 只读页面结构 + 监听点击，不发任何请求、不改页面数据。
 */
(function () {
  'use strict';
  if (window.__SD_RECORDER__) { window.__SD_RECORDER__.dump(); return; }

  var clicks = [];

  /** 收集所有同源 document（含 iframe 里的表单）。 */
  function collectDocs() {
    var docs = [document];
    var seen = [document];
    for (var i = 0; i < docs.length; i++) {
      var frames;
      try { frames = docs[i].querySelectorAll('iframe'); } catch (e) { continue; }
      for (var j = 0; j < frames.length; j++) {
        var child = null;
        try { child = frames[j].contentDocument; } catch (e) { child = null; }
        if (child && seen.indexOf(child) < 0) { seen.push(child); docs.push(child); }
      }
    }
    return docs;
  }

  function describe(el) {
    if (!el || !el.tagName) return null;
    var chain = [];
    var n = el;
    var fieldmark = '';
    for (var i = 0; i < 10 && n && n.tagName && n.tagName !== 'BODY'; i++) {
      var id = n.id ? '#' + n.id : '';
      var cls = (n.getAttribute && n.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean).slice(0, 3).join('.');
      if (n.getAttribute && n.getAttribute('data-fieldmark')) fieldmark = n.getAttribute('data-fieldmark');
      chain.push(n.tagName.toLowerCase() + id + (cls ? '.' + cls : ''));
      n = n.parentElement;
    }
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      cls: (el.getAttribute('class') || '').trim().slice(0, 140),
      name: el.getAttribute('name') || '',
      fieldmark: fieldmark,
      text: (el.innerText || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 40),
      chain: chain.join(' < '),
    };
  }

  function cellInfo(cell) {
    var ctrl = null;
    try { ctrl = cell.querySelector('input, textarea, select, .ant-select, .wea-select, .wea-browser'); } catch (e) {}
    var cls = (cell.getAttribute('class') || '').trim().split(/\s+/).slice(0, 2).join('.');
    var fm = cell.getAttribute('data-fieldmark');
    return cell.tagName.toLowerCase()
      + (fm ? '[fm=' + fm + ']' : '')
      + (cls ? '.' + cls : '')
      + '{' + (cell.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 16) + '}'
      + (ctrl ? '*' + ctrl.tagName.toLowerCase() : '');
  }

  function rowInfo(row) {
    var cells = row.children;
    var out = [];
    for (var c = 0; c < cells.length && c < 24; c++) out.push(cellInfo(cells[c]));
    return 'row(' + cells.length + '): ' + out.join(' | ');
  }

  function structure() {
    var data = { url: location.href, title: document.title, tables: [], fieldmarks: [], clickables: [], dropdowns: [] };
    var docs = collectDocs();
    for (var d = 0; d < docs.length; d++) {
      var doc = docs[d];
      var tables;
      try { tables = doc.querySelectorAll('table'); } catch (e) { continue; }
      for (var t = 0; t < tables.length; t++) {
        var rows;
        try { rows = tables[t].querySelectorAll('tr'); } catch (e) { continue; }
        if (!rows.length) continue;
        var head = [];
        for (var r = 0; r < rows.length && r < 4; r++) head.push(rowInfo(rows[r]));
        data.tables.push({ doc: d, t: t, rows: rows.length, head: head });
      }
      var fms = doc.querySelectorAll('[data-fieldmark]');
      for (var m = 0; m < fms.length && data.fieldmarks.length < 80; m++) {
        data.fieldmarks.push((d ? 'iframe:' : '') + fms[m].getAttribute('data-fieldmark') + ' <' + fms[m].tagName.toLowerCase() + '>');
      }
      var btns = doc.querySelectorAll('a, button, i, span, img, [onclick], [title], [class*="add"]');
      for (var b = 0; b < btns.length && data.clickables.length < 60; b++) {
        var el = btns[b];
        var tx = (el.innerText || '').replace(/\s+/g, ' ').trim();
        var tl = el.getAttribute('title') || el.getAttribute('aria-label') || '';
        if (tx.length > 16) continue;
        if (!tx && !tl) continue;
        var cls = (el.getAttribute('class') || '').trim().split(/\s+/).slice(0, 2).join('.');
        data.clickables.push((tx || '') + (tl ? '(' + tl + ')' : '') + ' <' + el.tagName.toLowerCase() + (cls ? '.' + cls : '') + (el.getAttribute('onclick') ? ' onclick' : '') + '>');
      }
      // 当前打开的下拉项真实结构（判断税率在哪：文本/子节点/属性）
      var dds = doc.querySelectorAll('.ant-select-dropdown, [class*="dropdown"]');
      for (var q = 0; q < dds.length; q++) {
        if (dds[q].className && String(dds[q].className).indexOf('hidden') >= 0) continue;
        var items = dds[q].querySelectorAll('li.ant-select-dropdown-menu-item, .ant-select-item-option, li[role="option"], li');
        for (var it = 0; it < items.length && data.dropdowns.length < 40; it++) {
          var li = items[it];
          var visible = false;
          try { var rect = li.getBoundingClientRect(); visible = rect.width > 1 && rect.height > 1; } catch (e) {}
          data.dropdowns.push({
            visible: visible,
            text: (li.innerText || li.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 60),
            html: (li.outerHTML || '').replace(/\s+/g, ' ').slice(0, 400),
          });
        }
      }
    }
    return data;
  }

  document.addEventListener('mousedown', function (e) {
    var el = document.elementFromPoint(e.clientX, e.clientY) || e.target;
    clicks.push({ act: 'click', at: Date.now(), target: describe(el) });
  }, true);
  document.addEventListener('change', function (e) {
    clicks.push({ act: 'change', at: Date.now(), target: describe(e.target), value: e.target && e.target.value });
  }, true);

  var dot = null;
  function setDot(text) { if (dot) dot.textContent = text; }

  function dump() {
    var data = structure();
    data.clicks = clicks;
    var text = JSON.stringify(data, null, 2);
    console.log('[SD recorder] ==== 复制下面这段发回 ====');
    console.log(text);
    try {
      navigator.clipboard.writeText(text);
      console.log('[SD recorder] 已复制到剪贴板（' + text.length + ' 字符）');
      setDot('● 已导出并复制（' + text.length + ' 字符），粘贴发回即可');
    } catch (e) {
      console.log('[SD recorder] 自动复制失败，请手动复制控制台输出');
      setDot('● 导出失败，请手动复制控制台输出');
    }
    return data;
  }

  // 页面上放个小指示条：点它或按 Alt+Shift+D 即可导出（不用在控制台里敲命令）
  try {
    dot = document.createElement('div');
    dot.textContent = '● StaffDeck 录制中 · 点这里或按 Alt+Shift+D 导出';
    dot.style.cssText = 'position:fixed;left:8px;bottom:8px;z-index:2147483647;background:#18181a;color:#fff;font:11px -apple-system,Segoe UI,sans-serif;padding:5px 9px;border-radius:7px;opacity:.9;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.25)';
    dot.addEventListener('click', function () { dump(); });
    (document.body || document.documentElement).appendChild(dot);
    window.addEventListener('keydown', function (e) {
      if (e.altKey && e.shiftKey && (e.key === 'D' || e.key === 'd')) { e.preventDefault(); dump(); }
    }, true);
  } catch (e) {}

  window.__SD_RECORDER__ = { dump: dump, structure: structure };
  console.log('[SD recorder] 已启动。请在页面上操作（点「报销场景」→选选项、点「+」…），完事点左下角小条或按 Alt+Shift+D 导出');
})();