/**
 * StaffDeck OA 录制器（开发排查用）。
 *
 * 用法（推荐）：打开报销表单 → 点面板右下角的「录制排查」→ 在 OA 上做一遍你想让面板学会的操作
 *   （点附件的「×」→ 点明细行的复选框 → 点「-」）→ 按 Alt+Shift+D（或点左下角小条）导出，
 *   把复制出来的 JSON 发回即可。
 *
 * 用法（控制台）：在 OA 页面控制台执行（host 用面板加载的同源地址）
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
    var attrs = {};
    try {
      for (var a = 0; a < el.attributes.length; a++) {
        var at = el.attributes[a];
        if (at.name === 'class' || at.name === 'style') continue;
        attrs[at.name] = String(at.value).slice(0, 80);
      }
    } catch (e) {}
    var cursor = '';
    try { cursor = (el.ownerDocument.defaultView.getComputedStyle(el) || {}).cursor || ''; } catch (e) {}
    var bg = '';
    try { bg = String((el.ownerDocument.defaultView.getComputedStyle(el) || {}).backgroundImage || '').slice(0, 120); } catch (e) {}
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || '',
      cls: (el.getAttribute('class') || '').trim().slice(0, 300),
      name: el.getAttribute('name') || '',
      fieldmark: fieldmark,
      title: el.getAttribute('title') || el.getAttribute('aria-label') || '',
      attrs: attrs,
      cursor: cursor,
      bgImage: bg,
      text: (el.innerText || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 40),
      chain: chain.join(' < '),
    };
  }

  /** 在同一行/同一个文件项里，还有哪些「可点的小东西」——面板写定位器就靠这个。 */
  function siblingsOf(el) {
    if (!el || !el.closest) return [];
    var box = el;
    var hops = 0;
    while (box && hops < 3 && !(box.tagName === 'TR' || (box.getAttribute && /upload-list|file-item|attach-item|row/i.test(box.getAttribute('class') || '')))) {
      box = box.parentElement; hops++;
    }
    if (!box) return [];
    var out = [];
    var cands;
    try { cands = box.querySelectorAll('a, button, i, span, em, img, svg, td'); } catch (e) { return out; }
    for (var i = 0; i < cands.length && out.length < 40; i++) {
      var c = cands[i];
      if (c.querySelector && c.querySelector('a, button, i, img, svg')) continue;
      var tx = (c.innerText || c.textContent || '').replace(/\s+/g, ' ').trim();
      var cls = String(c.getAttribute('class') || '').trim().slice(0, 90);
      var tl = c.getAttribute('title') || c.getAttribute('aria-label') || '';
      var cur = '';
      try { cur = (c.ownerDocument.defaultView.getComputedStyle(c) || {}).cursor || ''; } catch (e) {}
      if (!tx && !cls && !tl && c.tagName !== 'IMG' && c.tagName !== 'TD') continue;
      out.push(tag(c) + (tl ? ' title=' + JSON.stringify(tl) : '') + (tx ? ' text=' + JSON.stringify(tx.slice(0, 24)) : '')
        + (cur ? ' cursor=' + cur : '') + (c.tagName === 'IMG' ? ' src=' + String(c.getAttribute('src') || '').slice(0, 80) : '')
        + (c.getAttribute('onclick') ? ' onclick' : ''));
    }
    return out;
  }

  function tag(el) {
    var cls = String(el.getAttribute && el.getAttribute('class') || '').trim().split(/\s+/).slice(0, 3).join('.');
    return '<' + el.tagName.toLowerCase() + (cls ? '.' + cls : '') + '>';
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
      // 下拉项真实结构（含已隐藏的；不必保持下拉打开）
      var dds = doc.querySelectorAll('.ant-select-dropdown, [class*="dropdown"]');
      for (var q = 0; q < dds.length; q++) {
        var items = dds[q].querySelectorAll('li.ant-select-dropdown-menu-item, .ant-select-item-option, li[role="option"], li');
        for (var it = 0; it < items.length && data.dropdowns.length < 60; it++) {
          var li = items[it];
          var visible = false;
          try { var rect = li.getBoundingClientRect(); visible = rect.width > 1 && rect.height > 1; } catch (e) {}
          data.dropdowns.push({
            ddCls: String(dds[q].className || '').slice(0, 40),
            visible: visible,
            text: (li.innerText || li.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 60),
            html: (li.outerHTML || '').replace(/\s+/g, ' ').slice(0, 500),
          });
        }
      }
    }
    return data;
  }

  /** 关键计数快照：附件个数 / 勾选数 / 各明细组的行数——用来判断这一下点击到底做了什么。 */
  function snapCounts() {
    var out = {};
    var docs = collectDocs();
    for (var d = 0; d < docs.length; d++) {
      var doc = docs[d];
      var p = d ? 'iframe' + d + ':' : '';
      try {
        var files = doc.querySelectorAll("input[type='file']");
        var counts = [];
        for (var i = 0; i < files.length; i++) counts.push((files[i].files || []).length);
        out[p + 'files'] = counts.join('/');
      } catch (e) {}
      try {
        out[p + 'checked'] = doc.querySelectorAll("input[type='checkbox']:checked").length
          + '/' + doc.querySelectorAll("input[type='checkbox']").length;
      } catch (e) {}
      try {
        var fms = doc.querySelectorAll('[data-fieldmark]');
        var groups = {};
        for (var m = 0; m < fms.length; m++) {
          var v = String(fms[m].getAttribute('data-fieldmark') || '');
          if (!/_\d+$/.test(v)) continue;
          var base = v.replace(/_\d+$/, '');
          groups[base] = (groups[base] || 0) + 1;
        }
        out[p + 'rows'] = groups;
      } catch (e) {}
    }
    return out;
  }

  /** 被点元素的局部 HTML：自身 + 所在 tr + 所在上传列表项（定位器就靠这些写出来）。 */
  function contextOf(el) {
    if (!el || !el.closest) return {};
    function html(node, max) { return node ? String(node.outerHTML || '').replace(/\s+/g, ' ').slice(0, max) : ''; }
    var item = null;
    try {
      item = el.closest('[class*="upload-list"] li, [class*="upload-list"] [class*="item"], [class*="file-item"], [class*="attachment"]');
    } catch (e) {}
    var tr = null;
    try { tr = el.closest('tr'); } catch (e) {}
    var uploadBox = null;
    try { uploadBox = el.closest('[class*="upload"], [class*="file"], [class*="attach"], [class*="annex"]'); } catch (e) {}
    var rowBox = null;
    try { rowBox = el.closest('tr, [class*="row"], [class*="item"]'); } catch (e) {}
    var all = null;
    try { all = el.closest('[class*="upload"], [class*="attach"]'); } catch (e) {}
    return {
      outer: html(el, 600),
      tr: html(tr, 2000),
      item: item ? html(item, 1200) : '',
      uploadBox: uploadBox ? html(uploadBox, 1500) : '',
      rowBox: rowBox ? html(rowBox, 1500) : '',
      uploadAll: all ? html(all, 3000) : '',
      siblings: siblingsOf(el),
      parentChain: parents(el),
    };
  }

  /** 被点元素的祖先链（带完整 class）——图标按钮的位置关系一眼可见。 */
  function parents(el) {
    var out = [];
    var n = el;
    for (var i = 0; i < 8 && n && n.tagName && n.tagName !== 'BODY'; i++) {
      out.push(tag(n) + (n.id ? '#' + n.id : '') + (n.getAttribute && n.getAttribute('data-fieldmark') ? '[fm=' + n.getAttribute('data-fieldmark') + ']' : ''));
      n = n.parentElement;
    }
    return out.join(' < ');
  }

  /** 点完可能弹出二次确认，把可见弹窗文字也记下来。 */
  function dialogText() {
    var out = [];
    var docs = collectDocs();
    for (var d = 0; d < docs.length; d++) {
      var modals;
      try { modals = docs[d].querySelectorAll('.ant-modal, .wea-dialog, [role="dialog"], .ant-popover, .ant-message'); } catch (e) { continue; }
      for (var i = 0; i < modals.length && out.length < 6; i++) {
        var rect = null;
        try { rect = modals[i].getBoundingClientRect(); } catch (e) {}
        if (!rect || rect.width < 2) continue;
        out.push({
          cls: String(modals[i].className || '').slice(0, 60),
          text: (modals[i].innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200),
        });
      }
    }
    return out;
  }

  function onMouseDown(e) {
    // 必须用 e.target：elementFromPoint 是「本 document」的坐标空间，
    // 在 iframe 的 document 里调它会拿到外层的 <iframe>，把真实点击目标丢掉。
    var el = e.target;
    if (!el || !el.tagName) {
      var doc = (e.target && e.target.ownerDocument) || document;
      try { el = doc.elementFromPoint(e.clientX, e.clientY) || e.target; } catch (err) {}
    }
    if (!el || String(el.tagName).toLowerCase() === 'iframe') return; // 点的是 iframe 本身，不是 OA 控件
    var rec = { act: 'click', at: Date.now(), target: describe(el), ctx: contextOf(el), before: snapCounts() };
    clicks.push(rec);
    // 稍后再取一次快照：点击造成的「附件少了一个 / 明细少了一行 / 勾选变了」都在这里体现
    setTimeout(function () {
      try {
        rec.after = snapCounts();
        rec.dialog = dialogText();
      } catch (err) {}
    }, 450);
  }

  function onChange(e) {
    clicks.push({ act: 'change', at: Date.now(), target: describe(e.target), value: e.target && e.target.value });
  }

  // 上下键选择时，偷看「当前高亮项」与屏幕上出现的百分数（税率可能只在悬停/选中时显示）
  var keySnaps = [];
  function onKeyDown(e) {
    if (!/^Arrow(Up|Down)$/.test(e.key) && e.key !== 'Enter') return;
    var active = [];
    var lis = document.querySelectorAll('.ant-select-dropdown-menu-item, .ant-select-item-option, li[role="option"]');
    for (var i = 0; i < lis.length && active.length < 20; i++) {
      var cls = lis[i].getAttribute('class') || '';
      if (cls.indexOf('active') < 0 && cls.indexOf('selected') < 0 && cls.indexOf('hover') < 0) continue;
      active.push({ cls: cls, text: (lis[i].innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60), html: (lis[i].outerHTML || '').replace(/\s+/g, ' ').slice(0, 500) });
    }
    var pct = [];
    var all = document.querySelectorAll('span, div, td, li, em, i, b');
    for (var j = 0; j < all.length && pct.length < 30; j++) {
      if (all[j].children.length) continue;
      var t = (all[j].textContent || '').trim();
      if (t && t.length <= 12 && /\d+(\.\d+)?\s*[%％]/.test(t)) pct.push(t);
    }
    keySnaps.push({ key: e.key, activeItems: active, percentages: pct });
  }

  /** 监听绑到「所有同源 document」——OA 表单在 iframe 里，只绑顶层会把点击全漏掉。 */
  function bindOn(doc) {
    if (!doc || doc.__sdRecorderBound) return;
    try {
      doc.__sdRecorderBound = true;
      doc.addEventListener('mousedown', onMouseDown, true);
      doc.addEventListener('change', onChange, true);
      doc.addEventListener('keydown', onKeyDown, true);
    } catch (e) {}
  }
  function bindAll() {
    var docs = collectDocs();
    for (var d = 0; d < docs.length; d++) bindOn(docs[d]);
  }
  bindAll();
  // iframe 可能是后加载出来的 → 定时补绑（轻量，2s 一次）
  try { setInterval(bindAll, 2000); } catch (e) {}

  /** 自查：把「附件区」「明细行」的真实结构和候选按钮全列出来（不用你去精确点击）。 */
  function probe() {
    var out = { url: location.href, files: [], rows: [], fileDeleteCandidates: [], rowDeleteCandidates: [] };
    var docs = collectDocs();
    for (var d = 0; d < docs.length; d++) {
      var doc = docs[d];
      var pre = d ? 'iframe' + d + ':' : '';
      var files;
      try { files = doc.querySelectorAll("input[type='file']"); } catch (e) { files = []; }
      for (var i = 0; i < files.length; i++) {
        var f = files[i];
        var names = [];
        try { for (var k = 0; k < (f.files || []).length; k++) names.push(f.files[k].name); } catch (e) {}
        var box = null;
        try { box = f.closest('[class*="upload"], [class*="file"], [class*="attach"]') || f.parentElement; } catch (e) { box = f.parentElement; }
        out.files.push({
          idx: pre + i,
          fileNames: names,
          inputOuter: String(f.outerHTML || '').slice(0, 200),
          boxCls: String(box && box.className || '').slice(0, 120),
          boxHtml: String(box && box.outerHTML || '').replace(/\s+/g, ' ').slice(0, 2500),
          trHtml: html2(f.closest && f.closest('tr'), 2500),
        });
      }
      // 明细行：按 data-fieldmark 的 _序号 分组
      var fms;
      try { fms = doc.querySelectorAll('[data-fieldmark]'); } catch (e) { fms = []; }
      var groups = {};
      for (var m = 0; m < fms.length; m++) {
        var v = String(fms[m].getAttribute('data-fieldmark') || '');
        var mm = v.match(/^(.*)_(\d+)$/);
        if (!mm) continue;
        (groups[mm[1]] = groups[mm[1]] || {})[mm[2]] = 1;
      }
      for (var base in groups) {
        var idxs = Object.keys(groups[base]).sort(function (x, y) { return x - y; });
        out.rows.push({ pre: pre, group: base, indexes: idxs });
      }
      // 候选按钮：所有「小图标/文字」元素
      var cands;
      try { cands = doc.querySelectorAll('a, button, i, span, em, img, svg'); } catch (e) { cands = []; }
      for (var c = 0; c < cands.length && out.fileDeleteCandidates.length + out.rowDeleteCandidates.length < 200; c++) {
        var el = cands[c];
        if (el.querySelector && el.querySelector('a, button, i, img, svg')) continue;
        var tx = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
        var cls = String(el.getAttribute('class') || '');
        var tl = el.getAttribute('title') || el.getAttribute('aria-label') || '';
        var src = String(el.getAttribute('src') || '');
        var cur = '';
        try { cur = (el.ownerDocument.defaultView.getComputedStyle(el) || {}).cursor || ''; } catch (e) {}
        var item = describe(el);
        item.pre = pre;
        item.text = tx.slice(0, 24);
        item.cursor = cur;
        item.src = src.slice(0, 80);
        var inUpload = /upload|attach|file|annex/i.test(parents(el)) || /upload-list|file-item|attach/i.test(cls);
        var smallish = tx.length <= 6;
        var looksDelete = /×|✕|✖|删除|移除|清空|^-$|^－$/.test(tx) || /del|remove|close|minus|jian/i.test(cls + ' ' + tl);
        if (inUpload && smallish) out.fileDeleteCandidates.push(item);
        else if (smallish && looksDelete) out.rowDeleteCandidates.push(item);
      }
    }
    return out;
  }

  function html2(node, max) { return node ? String(node.outerHTML || '').replace(/\s+/g, ' ').slice(0, max) : ''; }

  var dot = null;
  function setDot(text) { if (dot) dot.textContent = text; }

  function dump() {
    var data = structure();
    data.clicks = clicks;
    data.keySnaps = keySnaps;
    try { data.probe = probe(); } catch (e) { data.probe = { error: String(e) }; }
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
    var bar = document.createElement('div');
    bar.style.cssText = 'position:fixed;left:8px;bottom:8px;z-index:2147483647;background:#18181a;color:#fff;font:11px -apple-system,Segoe UI,sans-serif;padding:5px 8px;border-radius:8px;opacity:.94;box-shadow:0 2px 8px rgba(0,0,0,.25);display:flex;align-items:center;gap:8px';
    function barBtn(text, fn) {
      var b = document.createElement('button');
      b.textContent = text;
      b.style.cssText = 'border:1px solid #4a4a52;background:#2a2a30;color:#fff;font:inherit;padding:3px 8px;border-radius:6px;cursor:pointer';
      b.addEventListener('click', function (e) { e.stopPropagation(); fn(); });
      return b;
    }
    bar.appendChild(barBtn('自查', function () { probeAndDump(); }));
    bar.appendChild(barBtn('导出全部', function () { dump(); }));
    dot = document.createElement('span');
    dot.textContent = '● 录制中：先点「自查」，或操作一遍后点「导出全部」';
    bar.appendChild(dot);
    bar.addEventListener('click', function () { dump(); });
    (document.body || document.documentElement).appendChild(bar);
    window.addEventListener('keydown', function (e) {
      if (e.altKey && e.shiftKey && (e.key === 'D' || e.key === 'd')) { e.preventDefault(); dump(); }
    }, true);
  } catch (e) {}

  /** 自查单独导出：在页面上依次点「附件的 ×」「明细的 -」后再点这里，
   *  会自动带上这两个按钮的真实结构 + 所有候选按钮。 */
  function probeAndDump() {
    var data = { probe: probe(), clicks: clicks };
    var text = JSON.stringify(data, null, 2);
    console.log('[SD recorder] ==== 自查结果（复制发回）====');
    console.log(text);
    try {
      navigator.clipboard.writeText(text);
      setDot('● 自查已复制（' + text.length + ' 字符），粘贴发回即可');
    } catch (e) {
      setDot('● 自查已打印到控制台，请手动复制');
    }
    return data;
  }

  window.__SD_RECORDER__ = { dump: dump, probe: probe, probeAndDump: probeAndDump, structure: structure };
  console.log('[SD recorder] 已启动。请在页面上操作（点「报销场景」→选选项、点「+」…），完事点左下角小条或按 Alt+Shift+D 导出');
})();