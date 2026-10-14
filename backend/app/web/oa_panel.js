/**
 * StaffDeck OA 代填面板（书签注入版）。
 *
 * 运行位置：用户浏览器里的 OA 页面（由书签把本文件以 <script> 注入）。
 * 因此：
 * - 配置 `window.__SD_OA_CONFIG__` 由后端拼在文件头部一起下发，面板不做任何跨域请求；
 * - 所有 DOM 操作都在 OA 页面自己的 origin 里，等价于用户手点；
 * - 填表动作沿用原 Playwright 版（已下线）验证过的实录操作序列（同样的
 *   field_library 语义：input / select / autocomplete / radio / file）。
 *
 * 不依赖任何构建工具，ES2017+ 语法即可（Chrome/Edge/Safari 现代版本都支持）。
 */
(function () {
  'use strict';

  var CFG = window.__SD_OA_CONFIG__ || {};
  var PANEL_CFG = CFG.panel || {};
  var TODO = '__TODO__';

  // 用当前脚本的真实 src 反推 StaffDeck 源站：书签在 OA 页里加载本文件，这个 origin 一定可达。
  var scriptEl = document.currentScript;
  var BASE = '';
  try {
    BASE = scriptEl && scriptEl.src ? new URL(scriptEl.src).origin : (window.__SD_OA_BASE__ || '');
  } catch (e) {
    BASE = window.__SD_OA_BASE__ || '';
  }
  try { window.__SD_OA_BASE__ = BASE; } catch (e) {}

  if (window.__SD_OA_PANEL__) {
    window.__SD_OA_PANEL__.toggle();
    return;
  }

  // ───────────────────────── 通用工具 ─────────────────────────

  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  function el(tag, style, text) {
    var node = document.createElement(tag);
    if (style) node.style.cssText = style;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function isVisible(node) {
    if (!node) return false;
    var r = node.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  }

  function visibleIn(root, selector) {
    var list = root.querySelectorAll(selector);
    for (var i = 0; i < list.length; i++) {
      if (isVisible(list[i])) return list[i];
    }
    return null;
  }

  /** 元素所在 window（OA 表单在 iframe 里，事件/异常要用它自己的 realm 构造）。 */
  function winOf(node) {
    return (node && node.ownerDocument && node.ownerDocument.defaultView) || window;
  }

  function clickEl(node) {
    if (!node) return;
    var W = winOf(node);
    var opts = { bubbles: true, cancelable: true, view: W, button: 0 };
    try { node.dispatchEvent(new W.MouseEvent('mousedown', opts)); } catch (e) {}
    try { node.dispatchEvent(new W.MouseEvent('mouseup', opts)); } catch (e) {}
    try { node.dispatchEvent(new W.MouseEvent('click', opts)); } catch (e) {}
  }

  /** 用元素自身原型上的 value setter 赋值，React 受控输入才会读到新值。
   *  不写 `node instanceof HTMLInputElement`：iframe 里的元素与顶层 window 不同 realm，
   *  instanceof 会恒为 false，进而静默退化成 `node.value=`（受控组件会回滚）。 */
  function setNative(node, value) {
    var proto = Object.getPrototypeOf(node);
    var desc = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
    if (desc && desc.set) desc.set.call(node, value);
    else node.value = value;
  }

  function fireEvent(node, type) {
    var W = winOf(node);
    node.dispatchEvent(new W.Event(type, { bubbles: true }));
  }

  function fireInput(node) {
    fireEvent(node, 'input');
    fireEvent(node, 'change');
  }

  function fireKey(node, type, key) {
    var W = winOf(node);
    try {
      node.dispatchEvent(new W.KeyboardEvent(type, { bubbles: true, cancelable: true, key: key }));
    } catch (e) {}
  }

  /** 清空一个输入框：先真的按退格键（合成 keydown/keyup，走框架自己的事件链），再兜底置空。
   *  用户可能填错了想「重新执行代填」，OA 上会残留上次填的值或预填默认值，必须清干净再写，
   *  否则会「追加」进去或受控组件回滚，表现就是「填了没生效」。 */
  function clearByBackspace(node) {
    try { node.focus(); } catch (e) {}
    var len = String(node.value === undefined || node.value === null ? '' : node.value).length;
    // 光标兜到末尾：有的组件按 selectionStart 决定退格删什么
    try { if (node.setSelectionRange) node.setSelectionRange(len, len); } catch (e) {}
    var rounds = Math.max(len + 1, 1);
    for (var i = 0; i < rounds; i++) {
      var before = String(node.value || '');
      fireKey(node, 'keydown', 'Backspace');
      fireKey(node, 'keyup', 'Backspace');
      // 已经是空的：这一下只是「所有字段先退格」的形式（空字段也可能挂着组件内部状态）
      if (!before.length) break;
      if (String(node.value || '') === before) {
        // 纯 native input：合成按键不会产生默认动作，自己截掉一个字符
        setNative(node, before.slice(0, -1));
        fireEvent(node, 'input');
      }
    }
    setNative(node, '');
    fireInput(node);
  }

  function fireHover(node) {
    var W = winOf(node);
    try { node.dispatchEvent(new W.MouseEvent('mouseover', { bubbles: true })); } catch (e) {}
    try { node.dispatchEvent(new W.MouseEvent('mouseenter', { bubbles: false })); } catch (e) {}
  }

  /** OA 表单在 static4form 的 iframe 里：把所有同源 document（含嵌套）收集起来，
   *  等价于 Playwright 侧的 _frame_with（主 frame 优先）。跨域 iframe 直接跳过。 */
  function collectDocs() {
    var docs = [document];
    var seen = [];
    seen.push(document);
    for (var i = 0; i < docs.length; i++) {
      var frames;
      try { frames = docs[i].querySelectorAll('iframe'); } catch (e) { continue; }
      for (var j = 0; j < frames.length; j++) {
        var child = null;
        try { child = frames[j].contentDocument; } catch (e) { child = null; }
        if (child && seen.indexOf(child) < 0) {
          seen.push(child);
          docs.push(child);
        }
      }
    }
    return docs;
  }

  /** 文本归一化：去掉所有空白，让 "保 存" 能匹配 innerText 里的 "保存"。 */
  function normText(value) {
    return String(value || '').replace(/\s+/g, '');
  }

  /**
   * 兼容 Playwright 特有的伪类（`button.ant-btn:has-text("保存")`）：
   * 浏览器原生 querySelectorAll 不认识 `:has-text()`，会直接抛 SyntaxError。
   * 做法：把 `:has-text("x")` 剥离成 CSS 前缀 + 文本过滤，逗号列表逐段处理。
   */
  function queryInDoc(doc, selector) {
    var out = [];
    var parts = String(selector).split(',');
    for (var i = 0; i < parts.length; i++) {
      var part = parts[i].trim();
      if (!part) continue;
      var needle = null;
      var cssPart = part;
      var match = part.match(/:has-text\(\s*(['"])([\s\S]*?)\1\s*\)/);
      if (!match) match = part.match(/:text\(\s*(['"])([\s\S]*?)\1\s*\)/);
      if (match) {
        needle = normText(match[2]);
        cssPart = part.replace(match[0], '').trim() || '*';
      }
      var nodes;
      try {
        nodes = doc.querySelectorAll(cssPart);
      } catch (e) {
        continue; // 还有其他不认识的伪类，跳过这一段
      }
      for (var j = 0; j < nodes.length; j++) {
        if (needle === null) { out.push(nodes[j]); continue; }
        var text = normText(nodes[j].innerText || nodes[j].textContent || nodes[j].value);
        if (text.indexOf(needle) >= 0) out.push(nodes[j]);
      }
    }
    return out;
  }

  /** 跨 frame 定位（主 frame 优先）：优先返回可见元素，没有可见的再返回首个匹配。 */
  function findEl(selector) {
    var docs = collectDocs();
    var fallback = null;
    for (var i = 0; i < docs.length; i++) {
      var list = queryInDoc(docs[i], selector);
      for (var j = 0; j < list.length; j++) {
        if (isVisible(list[j])) return list[j];
        if (!fallback) fallback = list[j];
      }
    }
    return fallback;
  }

  /** 从任一子节点上回到「select 容器」（ant-select / wea-select / wea-browser）。 */
  function selectContainerOf(node) {
    if (!node) return node;
    if (node.closest) {
      var box = node.closest('.ant-select, .wea-select, .wea-browser');
      if (box) return box;
    }
    return node;
  }

  /**
   * selector 未配置时的兜底：按页面可见的标签文本找控件。
   *  ① placeholder / aria-label / <label for> 命中标签；
   *  ② 整行（tr）文本包含标签，且行内只有一个可见输入（e-cology 表格表单的常见结构）。
   * mode=select 时优先返回 select 容器（供 fillSelect 使用）。找不到返回 null。
   */
  /** 页面上的短标签文本样本（by_label 找不到时附带，便于定位）；表头（th）优先。 */
  function sampleLabels(limit) {
    var out = [];
    var max = limit || 64;
    var docs = collectDocs();
    function push(raw) {
      var t = (raw || '').replace(/\s+/g, ' ').trim();
      if (t && t.length <= 16 && out.indexOf(t) < 0 && out.length < max) out.push(t);
    }
    for (var d = 0; d < docs.length; d++) {
      var ths = docs[d].querySelectorAll('th');
      for (var i = 0; i < ths.length; i++) push(ths[i].innerText || ths[i].textContent);
    }
    for (var d2 = 0; d2 < docs.length; d2++) {
      var cells = docs[d2].querySelectorAll('td, label');
      for (var j = 0; j < cells.length; j++) push(cells[j].innerText || cells[j].textContent);
    }
    return out;
  }

  /** 页面上的按钮样本（「新增」找不到时附带）；无文本的图标按钮用 <tag.class onclick> 标识。 */
  function sampleButtons(limit) {
    var out = [];
    var max = limit || 30;
    var docs = collectDocs();
    function push(s) { if (s && s.length <= 30 && out.indexOf(s) < 0 && out.length < max) out.push(s); }
    for (var d = 0; d < docs.length; d++) {
      var els;
      try {
        els = docs[d].querySelectorAll('a, button, [onclick], [role="button"], [class*="add"], [class*="Add"], [class*="plus"], [class*="new"], [title]');
      } catch (e) { continue; }
      for (var i = 0; i < els.length && out.length < max; i++) {
        var el = els[i];
        var text = (el.innerText || el.value || '').replace(/\s+/g, ' ').trim();
        if (text && text.length <= 12) { push(text); continue; }
        var title = el.getAttribute('title') || el.getAttribute('aria-label');
        if (title && title.length <= 16) { push(title); continue; }
        var cls = (el.getAttribute('class') || '').trim();
        var onclick = el.getAttribute('onclick') || '';
        if (onclick || /add|plus|new|insert|append/i.test(cls)) {
          var tag = el.tagName.toLowerCase();
          var short = cls.split(/\s+/).slice(0, 2).join('.');
          push('<' + tag + (short ? '.' + short : '') + (onclick ? ' onclick' : '') + '>');
        }
      }
    }
    return out;
  }

  /**
   * 表格列定位：明细是「列头 + 数据行」的表格时，按列号取第 index 个数据行的单元格控件。
   * 列头不限 th（e-cology 常用 td/div 做列头）：取包含标签的那个列头单元，算列号。
   */
  function findByGridColumn(docs, want, index, isSelect, isFile) {
    var labelEl = null;
    for (var d = 0; d < docs.length && !labelEl; d++) {
      var cands;
      try { cands = docs[d].querySelectorAll('th, td, div, span, label'); } catch (e) { continue; }
      var exact = null;
      var loose = null;
      for (var i = 0; i < cands.length; i++) {
        var t = normText(cands[i].textContent);
        if (!t || t.indexOf(want) < 0 || t.length > want.length + 20) continue;
        if (t === want) { exact = cands[i]; break; }
        if (!loose) loose = cands[i];
      }
      labelEl = exact || loose;
    }
    if (!labelEl) return null;

    var labelCell = labelEl.closest ? (labelEl.closest('td, th') || labelEl) : labelEl;
    var headerRow = labelCell.parentElement;
    if (!headerRow) return null;
    var colIndex = -1;
    var sibs = headerRow.children;
    for (var s = 0; s < sibs.length; s++) { if (sibs[s] === labelCell) { colIndex = s; break; } }
    if (colIndex < 0) return null;

    var dataRows = [];
    var doc = labelEl.ownerDocument || document;
    var hasControl = function (el) {
      return !!(el && el.querySelector && el.querySelector('input, textarea, select, .ant-select, .wea-select, .wea-browser'));
    };
    // ① 文档里排在表头之后的、含控件的行（兼容「表头表 / 数据表」分表的 e-cology 明细）
    var allTr = doc.querySelectorAll('tr');
    var after = false;
    for (var r = 0; r < allTr.length; r++) {
      if (allTr[r] === headerRow) { after = true; continue; }
      if (!after) continue;
      if (hasControl(allTr[r])) dataRows.push(allTr[r]);
    }
    // ② 表头不是 tr（div 网格）：同容器的后续兄弟行
    if (!dataRows.length) {
      var rows = headerRow.parentElement ? headerRow.parentElement.children : [];
      var started = false;
      for (var rr = 0; rr < rows.length; rr++) {
        if (rows[rr] === headerRow) { started = true; continue; }
        if (started && hasControl(rows[rr])) dataRows.push(rows[rr]);
      }
    }
    var dataRow = dataRows[index];
    if (!dataRow) return null;
    var cells = dataRow.children;
    var cell = null;
    // 优先用「表头的横向位置」匹配数据行的单元格（不依赖列号，兼容表头/数据列数
    // 不一致、多出复选框列、colspan 等）。数据行各列横向位置不同（真实布局）才用。
    var headerRect = labelCell.getBoundingClientRect ? labelCell.getBoundingClientRect() : null;
    var lefts = {};
    for (var ci = 0; ci < cells.length; ci++) {
      var rr = cells[ci].getBoundingClientRect();
      if (rr && rr.width > 1) lefts[Math.round(rr.left)] = 1;
    }
    if (headerRect && headerRect.width > 1 && Object.keys(lefts).length >= 2) {
      var hx = headerRect.left + headerRect.width / 2;
      var bestDist = Infinity;
      for (var cj = 0; cj < cells.length; cj++) {
        var rc = cells[cj].getBoundingClientRect();
        if (!rc || rc.width < 1) continue;
        var dist = Math.abs((rc.left + rc.width / 2) - hx);
        if (dist < bestDist) { bestDist = dist; cell = cells[cj]; }
      }
    }
    if (!cell) cell = cells[colIndex];
    if (cell) { var found = resolveControlIn(cell, isSelect, isFile); if (found) return found; }
    return null;
  }

  /** 在给定范围内解析控件：file → select 容器 → 可见输入框。 */
  function resolveControlIn(scope, isSelect, isFile) {
    if (!scope || !scope.querySelector) return null;
    if (isFile) {
      var fileInput = scope.querySelector('input[type=file]');
      if (fileInput) return fileInput;
    }
    if (isSelect) {
      var box = scope.querySelector('.ant-select, .wea-select, .wea-browser');
      if (box) return box;
    }
    var inputs = scope.querySelectorAll('input:not([type=hidden]):not([readonly]), textarea:not([readonly])');
    for (var c = 0; c < inputs.length; c++) {
      if (isVisible(inputs[c])) return isSelect ? selectContainerOf(inputs[c]) : inputs[c];
    }
    if (isSelect) {
      var anyBox = scope.querySelector('.ant-select, .wea-select, .wea-browser');
      if (anyBox) return anyBox;
    }
    return null;
  }

  /**
   * selector 未配置时的兜底：按页面标签文本找控件（可指定第 index 个，用于多行明细）。
   *  ① placeholder / aria-label / <label for>；
   *  ② 标签单元格 → 下一个单元格 → 整行，依次找控件（标签文本优先精确匹配）。
   */
  function findByLabelText(labelText, mode, index) {
    var want = normText(labelText);
    if (!want) return null;
    var wantIndex = index || 0;
    var isSelect = mode === 'select';
    var isFile = mode === 'file';
    var docs = collectDocs();

    // ① placeholder / aria-label / <label for>
    for (var d = 0; d < docs.length; d++) {
      var doc = docs[d];
      var inputs = doc.querySelectorAll('input, textarea');
      for (var i = 0; i < inputs.length; i++) {
        var el = inputs[i];
        var type = (el.getAttribute('type') || '').toLowerCase();
        if (type === 'hidden' || !isVisible(el)) continue;
        var hint = normText(el.getAttribute('placeholder')) + normText(el.getAttribute('aria-label'));
        if (hint && hint.indexOf(want) >= 0) return isSelect ? selectContainerOf(el) : el;
        if (el.id) {
          var lab = null;
          try {
            var esc = (window.CSS && window.CSS.escape) ? window.CSS.escape(el.id) : el.id;
            lab = doc.querySelector('label[for="' + esc + '"]');
          } catch (e) { lab = null; }
          if (lab && normText(lab.textContent).indexOf(want) >= 0) return isSelect ? selectContainerOf(el) : el;
        }
      }
    }

    // ② 标签单元格：文本包含标签、且自身不太长的最内层元素；精确匹配优先
    var labels = [];
    for (var d2 = 0; d2 < docs.length; d2++) {
      var cands = docs[d2].querySelectorAll('td, th, label, div, span');
      for (var j = 0; j < cands.length; j++) {
        var cand = cands[j];
        var text = normText(cand.textContent);
        if (!text || text.indexOf(want) < 0) continue;
        if (text.length > want.length + 20) continue;
        labels.push(cand);
      }
    }
    var leaves = labels.filter(function (el) {
      for (var k = 0; k < labels.length; k++) {
        if (labels[k] !== el && el.contains(labels[k])) return false;
      }
      return true;
    });
    var exact = leaves.filter(function (el) { return normText(el.textContent) === want; });
    var pool = exact.length ? exact : leaves;
    var label = pool[wantIndex] || pool[0] || null;
    if (!label) {
      return findByGridColumn(docs, want, wantIndex, isSelect, isFile);
    }

    var labelCell = label.closest ? label.closest('td, th') : null;
    var valueCell = labelCell && labelCell.nextElementSibling ? labelCell.nextElementSibling : null;
    var scopes = [];
    if (labelCell) scopes.push(labelCell);
    if (valueCell) scopes.push(valueCell);
    if (labelCell && labelCell.closest) scopes.push(labelCell.closest('tr'));
    if (label.parentElement) scopes.push(label.parentElement);
    for (var s = 0; s < scopes.length; s++) {
      var found = resolveControlIn(scopes[s], isSelect, isFile);
      if (found) return found;
    }
    // ③ 表格列：明细拿表格列头时，按列号取第 index 个数据行
    return findByGridColumn(docs, want, wantIndex, isSelect, isFile);
  }

  // ───────────────────────── 字段填写动作 ─────────────────────────

  function fillText(node, value, mode) {
    try { node.focus(); } catch (e) {}
    // 重新代填时会残留上次填的值 / 预填默认值 → 先退格清空
    clearByBackspace(node);
    if (mode === 'autocomplete') {
      // 逐字写入，触发受控组件的 input 同步，再等联想下拉
      var buf = '';
      for (var i = 0; i < value.length; i++) {
        buf += value[i];
        setNative(node, buf);
        fireEvent(node, 'input');
        fireKey(node, 'keyup', value[i]);
      }
    } else {
      setNative(node, value);
    }
    fireInput(node);
    try { node.blur(); } catch (e) {}
  }

  var _OPTION_SELECTORS = [
    '.ant-select-dropdown:not(.ant-select-dropdown-hidden) li.ant-select-dropdown-menu-item',
    '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option',
    '.ant-select-dropdown:not(.ant-select-dropdown-hidden) li',
    'ul[role="listbox"] li[role="option"]',
    '.wea-select-option',
    '.wea-browser-item',
    'a.child-item',
  ];

  /** 选项的完整文案：列表文字 + title/data-* 属性（e-cology 把完整名+税率放在 title/悬浮提示里，列表只显示截断名）。 */
  function optionText(el) {
    var text = (el.innerText || el.textContent || '');
    var attrRe = /^(title|data-title|data-value|data-rate|data-name|data-text)$/;
    function collect(node) {
      if (!node.attributes) return;
      for (var k = 0; k < node.attributes.length; k++) {
        var a = node.attributes[k];
        if (attrRe.test(a.name) && a.value) text += ' ' + a.value;
      }
    }
    collect(el);
    try {
      var inner = el.querySelectorAll('[title],[data-title],[data-value],[data-rate],[data-name],[data-text]');
      for (var i = 0; i < inner.length; i++) collect(inner[i]);
    } catch (e) {}
    return text;
  }

  function hoverEl(node) {
    if (!node) return;
    var W = winOf(node);
    var opts = { bubbles: true, cancelable: true, view: W };
    try { node.dispatchEvent(new W.MouseEvent('mouseover', opts)); } catch (e) {}
    try { node.dispatchEvent(new W.MouseEvent('mousemove', opts)); } catch (e) {}
    try { node.dispatchEvent(new W.MouseEvent('mouseenter', { bubbles: false, cancelable: true, view: W })); } catch (e) {}
  }

  /** 当前可见的 tooltip 文本（悬浮提示）；取最后一个（antd 会保留旧 tooltip，最新的在最后）。 */
  function visibleTooltipText(doc) {
    var sels = ['.ant-tooltip:not(.ant-tooltip-hidden)', '.ant-tooltip-inner', '[class*="tooltip"]:not([class*="hidden"])'];
    var last = '';
    for (var s = 0; s < sels.length; s++) {
      var list;
      try { list = doc.querySelectorAll(sels[s]); } catch (e) { continue; }
      for (var i = 0; i < list.length; i++) {
        if (!isVisible(list[i])) continue;
        var t = (list[i].innerText || list[i].textContent || '').trim();
        if (t) last = t;
      }
      if (last) return last;
    }
    return last;
  }

  function visibleOptions(doc, keyword) {
    var found = [];
    var seen = [];
    for (var s = 0; s < _OPTION_SELECTORS.length; s++) {
      var list;
      try { list = doc.querySelectorAll(_OPTION_SELECTORS[s]); } catch (e) { continue; }
      for (var i = 0; i < list.length; i++) {
        var li = list[i];
        if (seen.indexOf(li) >= 0) continue;
        seen.push(li);
        if (!isVisible(li)) continue;
        var text = optionText(li).trim();
        if (!keyword || text.indexOf(keyword) >= 0
            || text.replace(/\s+/g, '').indexOf(String(keyword).replace(/\s+/g, '')) >= 0) found.push(li);
      }
    }
    return found;
  }

  /**
   * 从候选项里挑最匹配的：
   *  ① 文本完全一致（归一化空白后）；
   *  ② 以关键词开头（如「电子普通发票」优先于「增值税电子普通发票」）；
   *  ③ 退而求其次：包含关键词的最短项。
   * 之前的「取第一个包含项」会在「电子普通发票」这类关键词上误选「增值税电子普通发票」。
   */
  /** 选项的各候选文案（列表文字 + title/data-*），用于精确/前缀/税率匹配。 */
  function optionTexts(el) {
    var arr = [];
    var t = (el.innerText || el.textContent || '');
    if (t) arr.push(t.replace(/\s+/g, ''));
    var attrRe = /^(title|data-title|data-value|data-rate|data-name|data-text)$/;
    function from(node) {
      if (!node.attributes) return;
      for (var k = 0; k < node.attributes.length; k++) {
        var a = node.attributes[k];
        if (attrRe.test(a.name) && a.value) arr.push(String(a.value).replace(/\s+/g, ''));
      }
    }
    from(el);
    try {
      var inner = el.querySelectorAll('[title],[data-title],[data-value],[data-rate],[data-name],[data-text]');
      for (var i = 0; i < inner.length; i++) from(inner[i]);
    } catch (e) {}
    return arr;
  }

  /** 去掉选项末尾的税率段，得到「发票类型名」：如「增值税普通发票|6%」→「增值税普通发票」。 */
  function baseName(t) {
    var s = String(t).replace(/\s+/g, '');
    s = s.replace(/[|｜/，,、\-—:：]*[（(【\[]?(税率|税额)?\d+(?:\.\d+)?[%％][)）】\]]?$/, '');
    return s;
  }

  /** 选项的「类型名」（去掉末尾税率段）。 */
  function optionNames(el) {
    return optionTexts(el).map(baseName).filter(Boolean);
  }

  /** 选项的税率段（如 '6%'）；无税率返回空串。 */
  function optionRate(el) {
    var t = optionTexts(el).filter(function (x) { return /[%％]/.test(x); });
    return t.length ? t[0] : '';
  }

  /**
   * 选下拉项（所有场景共用）：
   *  ① 先取「类型名完全相同」的选项；没有同名就退回全部检索结果；
   *  ② 只有一个结果 → 直接选；
   *  ③ 多个 → 按期望税率匹配；匹配不到（含税率 0%）→ 选「名字完全相同且税率为空」的那条。
   */
  function pickOption(options, keyword, prefer) {
    var key = String(keyword || '').replace(/\s+/g, '');
    if (!key || !options.length) return options[0];
    var pref = String(prefer || '').replace(/\s+/g, '');
    var sameName = options.filter(function (el) { return optionNames(el).indexOf(key) >= 0; });
    var pool = sameName.length ? sameName : options;
    if (pool.length === 1) return pool[0];
    if (pref) {
      var withPref = pool.filter(function (el) {
        var texts = optionTexts(el);
        for (var i = 0; i < texts.length; i++) {
          if (texts[i].indexOf(pref) >= 0 || texts[i].indexOf(pref.replace(/%/g, '％')) >= 0) return true;
        }
        return false;
      });
      if (withPref.length) return withPref[0];
    }
    var noRate = pool.filter(function (el) { return optionRate(el) === ''; });
    if (noRate.length) return noRate[0];
    return pool[0];
  }

  /** 提交判据：容器内 hidden input 有值（渲染区文本会镜像搜索词，不能作依据）。 */
  function selectCommitted(container, value) {
    var hidden = container.querySelector("input[type='hidden']");
    if (hidden && (hidden.value || '').trim()) return true;
    if (!visibleIn(container, '.ant-select-search__field')) {
      var rendered = container.querySelector('.ant-select-selection__rendered');
      if (rendered && value && (rendered.innerText || '').indexOf(value) >= 0) return true;
    }
    return false;
  }

  /** 渲染区是否已经清空（判断预填值还在不在）。 */
  function selectionEmpty(container) {
    if (container.querySelector('.ant-select-selection__choice, .ant-select-selection__choice__content')) return false;
    var rendered = container.querySelector('.ant-select-selection__rendered');
    if (!rendered) return true;
    return !(rendered.innerText || rendered.textContent || '').trim();
  }

  function clearSelection(container, search) {
    fireHover(container);
    // ① 清除图标 / 每个 chip 的 ×
    var clearIcon = visibleIn(container, '.ant-select-selection__clear');
    if (clearIcon) clickEl(clearIcon);
    var chipRemoves = container.querySelectorAll('.ant-select-selection__choice__remove');
    for (var c = 0; c < chipRemoves.length; c++) clickEl(chipRemoves[c]);
    // ② 复刻原 Playwright 的「退格清空」：搜索框清空后连按退格，
    //    antd/wea 在输入为空时会用 Backspace 取消已选项（合成 keydown 会走 React onKeyDown）
    if (search) { try { search.focus(); } catch (e) {} }
    var target = search || container;
    // 至少按一次退格（空组件也一样）：守「所有字段先退格」这条规则，
    // 也让组件把自己的内部选中状态放掉。
    fireKey(target, 'keydown', 'Backspace');
    fireKey(target, 'keyup', 'Backspace');
    for (var i = 0; i < 8; i++) {
      if (selectionEmpty(container)) break;
      if (search) { setNative(search, ''); fireEvent(search, 'input'); }
      fireKey(target, 'keydown', 'Backspace');
      fireKey(target, 'keyup', 'Backspace');
    }
    // ③ 兼底：直接把 hidden 值清掉
    var hidden = container.querySelector("input[type='hidden']");
    if (hidden && (hidden.value || '').trim()) {
      setNative(hidden, '');
      fireInput(hidden);
    }
    if (search) { setNative(search, ''); fireEvent(search, 'input'); }
  }

  /** 联想下拉（ant-select / wea-select / wea-browser）：沿用原 Playwright 版验证过的实录操作序列。 */
  async function fillSelect(container, spec, value, prefer) {
    var doc = container.ownerDocument || document;
    var keyword = spec.search_value || value;
    var attempts = [];

    for (var attempt = 0; attempt < 3; attempt++) {
      // ① 打开下拉，等搜索框或选项出现
      var search = visibleIn(container, '.ant-select-search__field');
      if (!search) {
        var clickTarget = visibleIn(container, '.ant-select-selection, .wea-select-selection, .wea-browser-input') || container;
        clickEl(clickTarget);
        for (var w = 0; w < 8; w++) {
          search = visibleIn(container, '.ant-select-search__field');
          if (search) break;
          if (visibleOptions(doc, '').length) break;
          await sleep(250);
        }
      }

      // ② 先清掉已选项：OA 会给「费用承担部门」之类预填默认值，重新代填时也会残留上次的选值。
      //    clearSelection 对空组件是 no-op（先找清除图标/chip，再按退格），无条件调用是安全的。
      clearSelection(container, search);
      await sleep(150);
      search = visibleIn(container, '.ant-select-search__field');

      // ③ 写入搜索词：读该组件实测，必须走「填值 + input 事件」，逐字键入不触发联想过滤
      if (search) {
        try { search.focus(); } catch (e) {}
        await sleep(150);
        setNative(search, keyword);
        fireEvent(search, 'input');
        fireKey(search, 'keyup', keyword.slice(-1));
        if ((search.value || '').trim() !== keyword) {
          setNative(search, keyword);
          fireEvent(search, 'input');
        }
      }

      // ④ 轮询等匹配选项（下拉是 portal，挂在 frame 的 body 上）
      var option = null;
      var lastAvail = [];
      for (var o = 0; o < 12; o++) {
        var all = visibleOptions(doc, '');
        if (all.length) lastAvail = all.map(function (li) { return optionText(li).trim().slice(0, 20); });
        var opts = visibleOptions(doc, keyword);
        if (opts.length) {
          // 期望税率不在选项文字里（e-cology 只显示截断名，完整名+税率在悬浮提示）时，逐个悬浮读 tooltip
          var prefNorm = String(prefer || '').replace(/\s+/g, '');
          if (prefNorm && opts.length > 1) {
            var foundPref = false;
            for (var z = 0; z < opts.length; z++) {
              if (optionText(opts[z]).replace(/\s+/g, '').indexOf(prefNorm) >= 0) { foundPref = true; break; }
            }
            if (!foundPref) {
              for (var hz = 0; hz < opts.length; hz++) {
                // antd Tooltip 一般包在列表项的内层元素上：内层 + 外层都悬一下
                var innerEl = opts[hz].querySelector('span, div, a') || opts[hz];
                hoverEl(innerEl);
                hoverEl(opts[hz]);
                await sleep(260);
                if (visibleTooltipText(doc).replace(/\s+/g, '').indexOf(prefNorm) >= 0) { opts = [opts[hz]]; break; }
              }
            }
          }
          option = pickOption(opts, keyword, prefer);
          break;
        }
        await sleep(300);
      }
      attempts.push('opts=' + (option ? 'hit' : 'miss'));
      if (!option) continue;

      // ⑤ 真点击 + 校验提交
      clickEl(option);
      await sleep(400);
      if (selectCommitted(container, value)) {
        try { if (search) fireKey(search, 'keydown', 'Escape'); } catch (e) {}
        try { document.body.click(); } catch (e) {}
        return optionText(option).trim() || String(value);
      }
      attempts.push('not_committed');
    }
    throw new Error('下拉选项中未找到「' + value + '」（' + attempts.join(' | ') + '）'
      + ' 可选项：' + (lastAvail.length ? lastAvail.slice(0, 16).join('/') : '（空）'));
  }

  async function fillFile(container, files) {
    // 容器可能是上传区 div（真实 input 在内部），也可能直接就是 input；组件异步渲染，稍等重试
    function resolveInput() {
      if (container.matches && container.matches("input[type='file']")) return container;
      var inner = container.querySelector ? container.querySelector("input[type='file']") : null;
      if (!inner && container.parentElement) inner = container.parentElement.querySelector("input[type='file']");
      return inner;
    }
    var input = resolveInput();
    for (var i = 0; i < 10 && !input; i++) {
      await sleep(200);
      input = resolveInput();
    }
    if (!input) throw new Error('未找到文件输入框（selector 是否指向上传区？）');
    // 重新代填时 OA 上会留着上次挂的附件 → 先清空，避免新旧附件叠在一起
    await clearFileInput(container, input);
    await sleep(200);
    var W = winOf(input);
    var dt = new W.DataTransfer();
    for (var k = 0; k < files.length; k++) dt.items.add(files[k]);
    input.files = dt.files;
    fireEvent(input, 'change');
    await sleep(300);
  }

  /** 清空上传组件上已挂的附件：先点掉列表里的删除图标（自定义上传组件只认这个），
   *  再把 file input 的值置空并派发 change（原生 input 会因此清空文件列表）。 */
  async function clearFileInput(container, input) {
    var stale = (input.files && input.files.length) || 0;
    var listItems;
    try { listItems = container.querySelectorAll('[class*="upload-list"] [class*="item"]'); } catch (e) { listItems = []; }
    if (!stale && !listItems.length) return; // 本来就没挂附件，不用清
    var killSel = [
      '[class*="upload-list"] [class*="delete"]',
      '[class*="upload-list"] [class*="remove"]',
      '[class*="upload-list"] [class*="close"]',
      '[title*="删除"]', '[title*="移除"]',
      '[aria-label*="删除"]', '[aria-label*="移除"]',
    ].join(',');
    var kills;
    try { kills = container.querySelectorAll(killSel); } catch (e) { kills = []; }
    var clicked = false;
    for (var i = 0; i < kills.length && i < 12; i++) {
      if (!isVisible(kills[i])) continue;
      clickEl(kills[i]);
      clicked = true;
    }
    if (clicked) await confirmDeleteDialog();
    try { setNative(input, ''); } catch (e) { try { input.value = ''; } catch (e2) {} }
    try { input.files = new (winOf(input).DataTransfer)().files; } catch (e) {}
    fireEvent(input, 'change');
  }

  /** 删除附件时 OA 有时弹二次确认：出现就点「确定」，别让弹窗挡住后面的字段。
   *  只在确认框里点「确定/确认/是」，避免误触表单的「保存」。 */
  async function confirmDeleteDialog() {
    await sleep(300);
    var docs = collectDocs();
    for (var d = 0; d < docs.length; d++) {
      var btns;
      try { btns = docs[d].querySelectorAll('.ant-modal-confirm-btns button, .wea-dialog-footer button'); } catch (e) { continue; }
      for (var i = 0; i < btns.length; i++) {
        if (!isVisible(btns[i])) continue;
        var txt = (btns[i].innerText || btns[i].textContent || '').trim();
        if (/^(确定|确认|是|OK|Yes)$/i.test(txt)) { clickEl(btns[i]); await sleep(300); }
      }
    }
  }

  /** 单个字段：返回 {field, status, detail}，不抛异常（异常由调用方兜住）。 */
  async function fillField(spec, values, filesByField) {
    var label = spec.label || spec.key || '';
    var mode = spec.mode || 'input';

    var selector = spec.selector;
    if (mode === 'radio') {
      var wantYes = !!values[spec.from || ''];
      selector = wantYes ? spec.selector_yes : spec.selector_no;
      if (!selector || selector === TODO) return { field: label, status: 'todo', detail: '定位器待配置' };
      var radio = findEl(selector);
      if (!radio) throw new Error('未找到单选元素：' + selector);
      clickEl(radio);
      return { field: label, status: 'filled', detail: '勾选 ' + (wantYes ? '是' : '否') };
    }

    // 取字段值：from 指向表单输入，否则用配置常量
    var value;
    if (spec.from) {
      value = values[spec.from];
      if (value === undefined || value === null || value === '') {
        return { field: label, status: 'skipped', detail: '表单未提供值' };
      }
    } else {
      value = spec.value;
    }
    if (value === undefined || value === null) {
      return { field: label, status: 'skipped', detail: '未配置值' };
    }

    if (mode === 'select' && typeof value === 'boolean') {
      value = value ? (spec.value_yes || '是') : (spec.value_no || '否');
    }

    // 定位元素：selector 缺失时，若配了 by_label 就按页面标签文本兜底查找
    var node;
    if (!selector || selector === TODO) {
      var matchLabel = spec.by_label ? (typeof spec.by_label === 'string' ? spec.by_label : label) : '';
      node = matchLabel ? findByLabelText(matchLabel, mode, 0) : null;
      if (!node) {
        return { field: label, status: 'todo', detail: matchLabel ? ('未按标签「' + matchLabel + '」定位到输入框（页面标签：' + sampleLabels().join('、') + '）') : '定位器待配置' };
      }
    } else {
      node = findEl(selector);
      if (!node) throw new Error('未找到元素：' + selector + '（当前页可能不是该场景的表单？）');
    }

    if (mode === 'file') {
      // 面板里选的本地文件直接搬进 OA 的 input（File 对象同文档传递，不经服务器）；
      // 按字段的 from 取对应附件（相关票据/其他参考资料各自一份）
      var picked = ((filesByField && filesByField[spec.from]) || []).slice();
      if (!picked.length) return { field: label, status: 'skipped', detail: '未选择附件' };
      await fillFile(node, picked);
      return { field: label, status: 'filled', detail: '附件 ' + picked.length + ' 个：' + picked.map(function (f) { return f.name; }).join('、') };
    }

    if (mode === 'select') {
      var prefer = spec.from === 'invoice_type' && values.invoice_rate ? String(values.invoice_rate) + '%' : '';
      var pickedText = await fillSelect(node, spec, String(value), prefer);
      return { field: label, status: 'filled', detail: '选择 ' + (pickedText || value) };
    }

    fillText(node, String(value), mode);
    return { field: label, status: 'filled', detail: String(value) };
  }

  function resolveFields(scenario) {
    var raw = scenario.fields || {};
    if (!Array.isArray(raw)) {
      return Object.keys(raw).map(function (k) { return [k, raw[k]]; });
    }
    var lib = CFG.field_library || {};
    var overrides = scenario.field_overrides || {};
    return raw.map(function (name) {
      var spec = {};
      var base = lib[name];
      if (base) {
        Object.keys(base).forEach(function (k) { spec[k] = base[k]; });
        var ov = overrides[name];
        if (ov) Object.keys(ov).forEach(function (k) { spec[k] = ov[k]; });
      } else {
        spec = { label: name, selector: TODO, mode: 'input' };
      }
      spec.key = name;
      return [name, spec];
    });
  }

  // ───────────────────────── 登录检测 ─────────────────────────

  function isLoginPage() {
    var check = (CFG.browser && CFG.browser.login_check) || {};
    var url = location.href;
    var words = check.url_contains || [];
    for (var i = 0; i < words.length; i++) {
      if (words[i] && url.toLowerCase().indexOf(String(words[i]).toLowerCase()) >= 0) return true;
    }
    var sels = check.selectors || [];
    for (var j = 0; j < sels.length; j++) {
      if (!sels[j] || sels[j] === TODO) continue;
      var n = findEl(sels[j]);
      if (n && isVisible(n)) return true;
    }
    return false;
  }

  // ───────────────────────── 面板 UI ─────────────────────────

  var host = document.createElement('div');
  host.id = 'sd-oa-panel-host';
  host.style.cssText = 'position:fixed;top:0;right:0;width:0;height:0;z-index:2147483647;';
  (document.body || document.documentElement).appendChild(host);
  var root = host.attachShadow ? host.attachShadow({ mode: 'open' }) : host;

  var CARD = 'position:fixed;top:14px;right:14px;width:340px;max-height:calc(100vh - 28px);' +
    'display:flex;flex-direction:column;background:#ffffff;color:#1f2333;' +
    'border:1px solid #e3e7f1;border-radius:14px;box-shadow:0 16px 40px rgba(20,28,60,.22);' +
    'font:12px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;' +
    'overflow:hidden;';
  var HEAD = 'display:flex;align-items:center;justify-content:space-between;gap:8px;' +
    'padding:10px 12px;border-bottom:1px solid #eef0f5;background:#fafbfd;cursor:move;user-select:none;-webkit-user-select:none;';
  var BODY = 'display:flex;flex-direction:column;gap:10px;padding:12px;overflow:auto;flex:1 1 auto;min-height:0;';
  var ROW = 'display:flex;flex-direction:column;gap:4px;';
  var LABEL = 'font-size:11.5px;color:#5b6274;';
  var INPUT = 'height:30px;border:1px solid #e3e7f1;border-radius:8px;padding:0 8px;font-size:12px;' +
    'color:#1f2333;background:#fff;box-sizing:border-box;width:100%;outline:none;';
  var BTN = 'height:30px;border:1px solid #e3e7f1;border-radius:8px;background:#fff;color:#464c5e;' +
    'font-size:12px;cursor:pointer;padding:0 10px;';
  var BTN_PRIMARY = 'height:32px;border:0;border-radius:9px;background:#18181a;color:#fff;' +
    'font-size:12px;cursor:pointer;flex:1;';
  var LOG = 'max-height:170px;overflow:auto;background:#fafbfd;border:1px solid #eef0f5;border-radius:8px;' +
    'padding:8px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;' +
    'line-height:1.6;white-space:pre-wrap;color:#5b6274;';

  var card = el('div', CARD);
  var head = el('div', HEAD);
  var titleWrap = el('div', 'display:flex;flex-direction:column;gap:1px;min-width:0;');
  titleWrap.appendChild(el('div', 'font-size:13px;font-weight:600;color:#18181a;', PANEL_CFG.title || 'AI报销代填'));
  titleWrap.appendChild(el('div', 'font-size:10.5px;color:#a3aaba;', '在当前 OA 页面直接填报 · 按住标题可拖动'));
  var closeBtn = el('button', 'border:0;background:transparent;font-size:16px;line-height:1;cursor:pointer;color:#858b9c;padding:4px;', '×');
  closeBtn.type = 'button';
  head.appendChild(titleWrap);
  head.appendChild(closeBtn);
  card.appendChild(head);

  var body = el('div', BODY);
  card.appendChild(body);

  function field(labelText, control) {
    var row = el('div', ROW);
    row.appendChild(el('div', LABEL, labelText));
    row.appendChild(control);
    return row;
  }

  // 报销场景
  var scenarioSelect = el('select', INPUT);
  var scenarios = (CFG.scenarios || []).filter(function (s) { return s.enabled !== false; });
  scenarios.forEach(function (s) {
    var opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.name;
    scenarioSelect.appendChild(opt);
  });
  var scenarioCtrl = el('div', 'display:flex;align-items:center;gap:6px;min-width:0;');
  scenarioSelect.style.flex = '1 1 auto';
  scenarioSelect.style.minWidth = '0';
  scenarioCtrl.appendChild(scenarioSelect);
  var scenarioLink = el('a', 'flex:0 0 auto;font-size:11px;color:#1a56db;text-decoration:none;white-space:nowrap;cursor:pointer;', '去报销');
  scenarioLink.title = '在当前页面跳转到所选场景的 OA 报销表单';
  scenarioCtrl.appendChild(scenarioLink);
  var scenarioRow = field('报销场景', scenarioCtrl);
  scenarioRow.style.order = '1';
  body.appendChild(scenarioRow);

  // 场景提示（如「相关流程」需手动选择、报销明细暂不支持代填）
  var noticeBox = el('div', 'display:none;flex-direction:column;gap:3px;border:1px solid #f0dca8;background:#fffaef;color:#8a4b00;border-radius:9px;padding:7px 9px;font-size:11px;line-height:1.65;');
  noticeBox.style.order = '3';
  body.appendChild(noticeBox);

  // 报销场景二选一（如交通报销：飞机 / 火车·汽车·船）
  var sceneSeg = el('div', 'display:flex;border:1px solid #e3e7f1;border-radius:8px;overflow:hidden;');
  var sceneSegRow = field('报销场景', sceneSeg);
  sceneSegRow.style.display = 'none';
  sceneSegRow.style.order = '2';
  body.appendChild(sceneSegRow);
  var sceneChoice = '';

  // 报销事由
  var reasonInput = el('input', INPUT);
  var reasonRow = field('报销事由', reasonInput);
  reasonRow.style.order = '4';
  body.appendChild(reasonRow);

  // 出差天数（仅出差报销场景；对应 OA「实际出差天数」）
  var tripDaysInput = el('input', INPUT);
  tripDaysInput.type = 'number';
  tripDaysInput.min = '0';
  tripDaysInput.step = '0.5';
  var tripDaysRow = field('出差天数', tripDaysInput);
  tripDaysRow.style.display = 'none';
  tripDaysRow.style.order = '5';
  body.appendChild(tripDaysRow);

  // 相关流程（出差报销：填流程关键字 → 面板在 OA 里检索并选中）
  var relatedFlowInput = el('input', INPUT);
  relatedFlowInput.placeholder = '输入流程关键字检索，如 0918';
  var relatedFlowRow = field('相关流程', relatedFlowInput);
  relatedFlowRow.style.display = 'none';
  relatedFlowRow.style.order = '5';
  body.appendChild(relatedFlowRow);

  // 公司名称
  var companySelect = el('select', INPUT);
  (PANEL_CFG.company_names || []).forEach(function (name) {
    var opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    companySelect.appendChild(opt);
  });
  var companyRow = field('公司名称', companySelect);
  companyRow.style.order = '6';
  body.appendChild(companyRow);

  // 收款单位名称（境内公司付款：文本输入 → 引擎在 OA 联想组件里搜索选中全称）
  var payeeNameInput = el('input', INPUT);
  payeeNameInput.placeholder = '输入收款单位名称，自动搜索全称';
  var payeeNameRow = field('收款单位名称', payeeNameInput);
  payeeNameRow.style.display = 'none';
  payeeNameRow.style.order = '6';
  body.appendChild(payeeNameRow);

  // 发票类型
  var invoiceSelect = el('select', INPUT);
  (PANEL_CFG.invoice_types || []).forEach(function (t) {
    var opt = document.createElement('option');
    opt.value = t.value;
    opt.textContent = t.label || t.value;
    invoiceSelect.appendChild(opt);
  });
  var invoiceRow = field('发票类型', invoiceSelect);
  invoiceRow.style.order = '10';
  body.appendChild(invoiceRow);

  // 是否全为电子票
  var radioWrap = el('div', 'display:flex;gap:16px;align-items:center;height:26px;');
  function radio(value, text, checked) {
    var wrap = el('label', 'display:flex;align-items:center;gap:5px;cursor:pointer;color:#464c5e;');
    var input = document.createElement('input');
    input.type = 'radio';
    input.name = 'sd-oa-eticket';
    input.value = value;
    input.checked = checked;
    wrap.appendChild(input);
    wrap.appendChild(el('span', '', text));
    return wrap;
  }
  var defaultYes = PANEL_CFG.default_all_e_ticket === true;
  radioWrap.appendChild(radio('no', '否', !defaultYes));
  radioWrap.appendChild(radio('yes', '是', defaultYes));
  var eticketRow = field('是否全为电子票', radioWrap);
  eticketRow.style.order = '8';
  body.appendChild(eticketRow);
  radioWrap.addEventListener('change', function () { syncAttachVisibility(); });

  // 报销金额（下方 hint 用于显示自动计算/取整的原始金额）
  var amountInput = el('input', INPUT);
  amountInput.value = String(PANEL_CFG.default_amount || '100.00');
  var amountHint = el('div', 'font-size:11px;color:#a4650a;line-height:1.6;display:none;');
  amountInput.addEventListener('input', function () { amountHint.textContent = ''; amountHint.style.display = 'none'; });
  var amountBox = el('div', 'display:flex;flex-direction:column;gap:4px;');
  amountBox.appendChild(amountInput);
  amountBox.appendChild(amountHint);
  var amountRow = field('报销金额（默认值，可改）', amountBox);
  amountRow.style.order = '12';
  body.appendChild(amountRow);

  // 附件（全为电子票才需要）：本地多选 PDF → 直接搬进 OA，同时可自动算金额
  var attachmentFiles = [];
  var attachBox = el('div', 'display:flex;flex-direction:column;gap:8px;border:1px dashed #dfe3ec;border-radius:10px;padding:10px;background:#fafbfd;');
  var attachTop = el('div', 'display:flex;align-items:center;gap:8px;');
  var pickBtn = el('button', BTN, '选择附件');
  pickBtn.type = 'button';
  var attachHint = el('span', 'font-size:10.5px;color:#a3aaba;flex:1;', '仅限 PDF（电子发票），可多选');
  var clearFilesBtn = el('button', 'border:0;background:transparent;color:#a3aaba;font-size:11.5px;cursor:pointer;padding:0;', '清空');
  clearFilesBtn.type = 'button';
  attachTop.appendChild(pickBtn);
  attachTop.appendChild(attachHint);
  attachTop.appendChild(clearFilesBtn);
  attachBox.appendChild(attachTop);

  var fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = '.pdf';
  fileInput.multiple = true;
  fileInput.style.display = 'none';
  attachBox.appendChild(fileInput);

  var fileList = el('div', 'display:flex;flex-direction:column;gap:3px;');
  attachBox.appendChild(fileList);

  var lastCalcTotal = null;
  var lastInvoiceRate = '';

  var calcWrap = el('label', 'display:flex;align-items:center;gap:6px;cursor:pointer;color:#464c5e;font-size:11.5px;');
  var calcCheck = document.createElement('input');
  calcCheck.type = 'checkbox';
  calcCheck.checked = PANEL_CFG.auto_calc_amount !== false;
  calcWrap.appendChild(calcCheck);
  calcWrap.appendChild(el('span', '', '自动计算发票总金额'));
  attachBox.appendChild(calcWrap);

  var floorWrap = el('label', 'display:flex;align-items:center;gap:6px;cursor:pointer;color:#464c5e;font-size:11.5px;');
  var floorCheck = document.createElement('input');
  floorCheck.type = 'checkbox';
  floorCheck.checked = PANEL_CFG.floor_amount_to_hundred === true;
  floorWrap.appendChild(floorCheck);
  floorWrap.appendChild(el('span', '', '金额向下取整到百位（如 1033.22 → 1000）'));
  attachBox.appendChild(floorWrap);

  var attachRow = field('电子发票附件（挂到 OA「相关票据」区）', attachBox);
  attachRow.style.order = '9';
  body.appendChild(attachRow);

  // 发票明细（出差报销）：与附件同区，一张发票一行（发票类型按该票税率，可改）
  var rowsBox = el('div', 'display:flex;flex-direction:column;gap:7px;');
  var rowsHint = el('div', 'font-size:11px;color:#a3aaba;', '选择发票后自动生成：一张发票一行');
  var rowsWrap = el('div', 'display:none;flex-direction:column;gap:7px;');

  // 明细合并开关：勾选 = 同一「报销场景 + 税率」合并为一行、金额累计；
  // 不勾选 = 一张发票一行，各自金额。默认值随场景配置（line_items.group_by）走。
  var mergeWrap = el('label', 'display:flex;align-items:center;gap:6px;cursor:pointer;color:#464c5e;font-size:11.5px;');
  mergeWrap.title = '不勾选时：一张发票一行明细（各自金额、发票类型可单独改）';
  var mergeCheck = document.createElement('input');
  mergeCheck.type = 'checkbox';
  mergeCheck.checked = true;
  mergeWrap.appendChild(mergeCheck);
  mergeWrap.appendChild(el('span', '', '税率相同的合并到同一明细中'));
  // 只在「切换场景」时按配置重置开关，避免同场景内的其它交互把用户的选择抹掉
  var mergeDefaultScenarioId = '';

  rowsWrap.appendChild(rowsHint);
  rowsWrap.appendChild(mergeWrap);
  rowsWrap.appendChild(rowsBox);
  attachBox.appendChild(rowsWrap);
  var panelRows = [];

  // 其他参考资料（额外附件，多选）
  var otherRefFiles = [];
  var otherRefInput = document.createElement('input');
  otherRefInput.type = 'file';
  otherRefInput.multiple = true;
  otherRefInput.style.display = 'none';
  var otherRefList = el('div', 'display:flex;flex-direction:column;gap:3px;');
  var otherRefsBox = el('div', 'display:flex;flex-direction:column;gap:8px;border:1px dashed #dfe3ec;border-radius:10px;padding:10px;background:#fafbfd;');
  var otherRefTop = el('div', 'display:flex;align-items:center;gap:8px;');
  var otherRefPick = el('button', BTN, '选择附件');
  otherRefPick.type = 'button';
  var otherRefHint = el('span', 'font-size:10.5px;color:#a3aaba;flex:1;', '可多选');
  var otherRefClear = el('button', 'border:0;background:transparent;color:#a3aaba;font-size:11.5px;cursor:pointer;padding:0;', '清空');
  otherRefClear.type = 'button';
  otherRefTop.appendChild(otherRefPick);
  otherRefTop.appendChild(otherRefHint);
  otherRefTop.appendChild(otherRefClear);
  otherRefsBox.appendChild(otherRefTop);
  otherRefsBox.appendChild(otherRefInput);
  otherRefsBox.appendChild(otherRefList);
  var otherRefsRow = field('其他参考资料', otherRefsBox);
  otherRefsRow.style.display = 'none';
  otherRefsRow.style.order = '10';
  body.appendChild(otherRefsRow);

  function renderOtherRefs() {
    otherRefList.textContent = '';
    if (!otherRefFiles.length) { otherRefList.appendChild(el('div', 'font-size:11px;color:#a3aaba;', '尚未选择附件')); return; }
    otherRefFiles.forEach(function (file, index) {
      var row = el('div', 'display:flex;align-items:center;gap:6px;font-size:11.5px;color:#464c5e;');
      row.appendChild(el('span', 'color:#0f6b4f;', '✓'));
      var nameEl = el('span', 'flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;', file.name);
      nameEl.title = file.name;
      row.appendChild(nameEl);
      var del = el('button', 'border:0;background:transparent;color:#a3aaba;cursor:pointer;padding:0 2px;', '×');
      del.type = 'button';
      del.addEventListener('click', function () { otherRefFiles.splice(index, 1); renderOtherRefs(); });
      row.appendChild(del);
      otherRefList.appendChild(row);
    });
  }
  renderOtherRefs();
  otherRefPick.addEventListener('click', function () { otherRefInput.click(); });
  otherRefClear.addEventListener('click', function () { otherRefFiles = []; renderOtherRefs(); });
  otherRefInput.addEventListener('change', function (e) {
    var picked = Array.prototype.slice.call(e.target.files || []);
    e.target.value = '';
    picked.forEach(function (file) {
      var dup = otherRefFiles.some(function (x) { return x.name === file.name && x.size === file.size; });
      if (!dup) otherRefFiles.push(file);
    });
    renderOtherRefs();
  });

  function isTripScenario() {
    var s = currentScenario();
    return !!(s && s.line_items);
  }

  function refreshRowTotal() {
    var sum = panelRows.reduce(function (n, r) { return n + (Number(r.amount) || 0); }, 0);
    if (isTripScenario()) amountInput.value = sum.toFixed(2);
  }

  /** 按票面税率预选发票类型（同税率可能有电子/普通/增值多个，面板只做预选，用户可改）。 */
  /** 把「发票类型名/搜索词（+税率）」反查成面板选项的唯一 value。同名多个时按税率取唯一。 */
  function invoiceTypeValueFromSearch(name, rate) {
    if (!name) return '';
    var opts = PANEL_CFG.invoice_types || [];
    var exact = '';
    var first = '';
    var hasRate = rate !== undefined && rate !== null && rate !== '';
    for (var i = 0; i < opts.length; i++) {
      var isName = String(opts[i].search || opts[i].value) === String(name) || String(opts[i].value) === String(name);
      if (!isName) continue;
      if (!first) first = String(opts[i].value);
      if (hasRate && String(opts[i].tax_rate || '') === String(rate)) { exact = String(opts[i].value); break; }
    }
    return exact || first;
  }

  /** 面板里某个发票类型选项对应的 OA 搜索词（发给 OA 用）。 */
  function invoiceTypeSearch(value) {
    var opts = PANEL_CFG.invoice_types || [];
    for (var i = 0; i < opts.length; i++) {
      if (String(opts[i].value) === String(value)) return String(opts[i].search || opts[i].value);
    }
    return String(value || '');
  }

  /** 面板里某个发票类型选项对应的税率（用于给引擎传期望税率）。 */
  function invoiceTypeRate(value) {
    var opts = PANEL_CFG.invoice_types || [];
    for (var i = 0; i < opts.length; i++) {
      if (String(opts[i].value) === String(value)) return String(opts[i].tax_rate || '');
    }
    return '';
  }

  function matchInvoiceType(rate) {
    var opts = PANEL_CFG.invoice_types || [];
    if (rate !== undefined && rate !== null && rate !== '') {
      for (var i = 0; i < opts.length; i++) {
        if (String(opts[i].tax_rate || '') === String(rate)) return opts[i].value;
      }
    }
    // 识别不出税率 → 默认「普通发票 税率0%」
    for (var j = 0; j < opts.length; j++) {
      if (String(opts[j].tax_rate || '') === '0') return opts[j].value;
    }
    return opts.length ? opts[0].value : '';
  }

  /** 明细行的事项描述：默认「{scene}费」，场景可用 line_items.item_desc_template 覆盖。 */
  function rowItemDesc(scene) {
    var li = (currentScenario() || {}).line_items || {};
    var tpl = li.item_desc_template || '{scene}费';
    return tpl.replace('{scene}', scene || '');
  }

  function renderRows() {
    rowsBox.textContent = '';
    rowsHint.style.display = panelRows.length ? 'none' : 'block';
    var li = (currentScenario() || {}).line_items || {};
    var sceneOpts = li.scene_options || (li.scene_value ? [li.scene_value] : []);
    panelRows.forEach(function (row, i) {
      var item = el('div', 'border:1px solid #e3e7f1;border-radius:9px;background:#fff;padding:7px;display:flex;flex-direction:column;gap:6px;');
      var head = el('div', 'display:flex;align-items:center;gap:6px;');
      if (!sceneOpts.length) {
        head.appendChild(el('span', 'font-size:11.5px;font-weight:600;color:#464c5e;white-space:nowrap;', row.scene || row.category || '明细'));
      }
      head.appendChild(el('span', 'font-size:10px;color:#a3aaba;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;', row.filename || ('第 ' + (i + 1) + ' 行')));
      item.appendChild(head);

      // 报销场景：下拉，默认选中（如境内公司付款默认「IT服务外包」，还能选「IT硬件设备」）
      if (sceneOpts.length) {
        var sceneSel = el('select', INPUT);
        sceneOpts.forEach(function (s) { var o = document.createElement('option'); o.value = s; o.textContent = s; sceneSel.appendChild(o); });
        sceneSel.value = row.scene || sceneOpts[0];
        if (!sceneSel.value && row.scene) {
          // 解析出来的场景不在预设选项里（票面类目任意）→ 临时补一项，别把值清空
          var extra = document.createElement('option');
          extra.value = row.scene;
          extra.textContent = row.scene;
          sceneSel.insertBefore(extra, sceneSel.firstChild);
          sceneSel.value = row.scene;
        }
        row.scene = sceneSel.value;
        sceneSel.addEventListener('change', function () {
          row.scene = sceneSel.value;
          // 事项描述默认跟场景走（"{scene}费"）；用户手改过就不再覆盖
          if (!row.item_desc_manual) {
            row.item_desc = rowItemDesc(row.scene);
            var descNode = item.querySelector('input[data-role="item_desc"]');
            if (descNode) descNode.value = row.item_desc;
          }
        });
        item.appendChild(field('报销场景', sceneSel));
      }

      // 发票类型：下拉（选项来自 panel.invoice_types；按该票税率预选，用户可改）
      var itype = el('select', INPUT);
      var blank = document.createElement('option');
      blank.value = '';
      blank.textContent = '请选择发票类型';
      itype.appendChild(blank);
      (PANEL_CFG.invoice_types || []).forEach(function (o) {
        var opt = document.createElement('option');
        opt.value = o.value;
        opt.textContent = o.label || o.value;
        itype.appendChild(opt);
      });
      var known = (PANEL_CFG.invoice_types || []).some(function (o) { return o.value === row.invoice_type; });
      itype.value = known ? row.invoice_type : matchInvoiceType(row.tax_rate);
      row.invoice_type = itype.value;
      itype.addEventListener('change', function () { row.invoice_type = itype.value; row.invoice_type_manual = true; });
      item.appendChild(field('发票类型', itype));

      var amt = el('input', INPUT);
      amt.type = 'number';
      amt.step = '0.01';
      amt.value = row.amount || '';
      amt.addEventListener('input', function () { row.amount = amt.value; refreshRowTotal(); });
      var grid = el('div', 'display:grid;grid-template-columns:1fr 108px;gap:6px;');
      var desc = el('input', INPUT);
      desc.setAttribute('data-role', 'item_desc');
      desc.value = row.item_desc || '';
      desc.addEventListener('input', function () { row.item_desc = desc.value; row.item_desc_manual = true; });
      grid.appendChild(field('事项描述', desc));
      grid.appendChild(field('金额', amt));
      item.appendChild(grid);

      rowsBox.appendChild(item);
    });
    refreshRowTotal();
  }

  async function parseForRows() {
    if (!attachmentFiles.length) { panelRows = []; renderRows(); return; }
    if (!BASE) { logLine('无法定位 StaffDeck 服务地址，跳过发票解析。', 'warn'); return; }
    logLine('正在解析 ' + attachmentFiles.length + ' 张发票…', 'dim');
    try {
      var fd = new FormData();
      attachmentFiles.forEach(function (file) { fd.append('files', file, file.name); });
      var res = await fetch(BASE + '/api/enterprise/oa-assistant/invoice-amounts', { method: 'POST', body: fd });
      var data = await res.json();
      if (!res.ok) throw new Error((data && data.detail) || ('HTTP ' + res.status));
      var scenario = currentScenario() || {};
      var parsed = (data.rows || []).map(function (r) {
        var itValue = invoiceTypeValueFromSearch(r.suggested_invoice_type, r.tax_rate) || invoiceTypeValueFromSearch(r.invoice_type, r.tax_rate) || '';
        return {
          category: r.category, scene: r.scene || '', tax_rate: r.tax_rate,
          invoice_type: itValue, amount: Number(r.amount) || 0, filename: r.filename,
          // 票面类目是任意的：没归到场景时事项描述留空（不要拼出孤零零的「费」字）
          item_desc: r.scene ? rowItemDesc(r.scene) : ''
        };
      });
      var li = scenario.line_items || {};
      // 报销场景兜底（票面类目任意，后端归类不到就留空）：
      // 1) 场景有默认明细场景（境内公司付款 scene_value=IT服务外包，下拉里还能再改
      //    成 scene_options 的其它值，如 IT硬件设备）→ 全部行改写；
      // 2) 场景本身只对应一个报销场景（通讯费用 / 交通费用）→ 只补空的那几行。
      var sceneDefault = li.scene_value || (li.scene_options && li.scene_options[0]) || '';
      if (sceneDefault) {
        parsed.forEach(function (r) { r.scene = sceneDefault; r.item_desc = rowItemDesc(sceneDefault); });
      } else if (scenario.scene) {
        parsed.forEach(function (r) {
          if (!r.scene) { r.scene = scenario.scene; r.item_desc = rowItemDesc(scenario.scene); }
        });
      }
      // 合并规则由面板上的「税率相同的合并到同一明细中」开关决定（默认随场景配置）：
      // 勾选 = 同一「报销场景 + 税率」合并为一行、金额累计；不勾选 = 一张发票一行。
      if (mergeCheck.checked) {
        var byKey = {};
        parsed.forEach(function (r) {
          var key = (r.scene || '') + '|' + String(r.tax_rate || '');
          if (!byKey[key]) {
            byKey[key] = { category: r.category, scene: r.scene, tax_rate: r.tax_rate, invoice_type: r.invoice_type, amount: 0, filename: r.filename, item_desc: r.item_desc };
          }
          byKey[key].amount += r.amount;
        });
        parsed = Object.keys(byKey).map(function (k) { return byKey[k]; });
      }
      // 金额统一成两位小数字符串：不合并时是票面金额、合并时是累计值，都要落成
      // "750.00" 这种写法，否则 OA 明细上会写成 "750"（Number 直接转字符串）。
      parsed.forEach(function (r) { r.amount = (Number(r.amount) || 0).toFixed(2); });
      panelRows = parsed;
      renderRows();
      // 逐张报出识别结果：明细行会按「报销场景 + 税率」合并（2 张票可能只出 1 行），
      // 只报行数会让人误以为「后加的那张没被识别」——这里把每张票的金额/税率都列出来。
      var okItems = (data.items || []).filter(function (it) { return !it.error; });
      (data.items || []).forEach(function (it) {
        if (it.error) return;
        logLine('· ' + (it.amount || '—') + (it.tax_rate ? '（税率 ' + it.tax_rate + '%）' : '') + ' ← ' + it.filename, 'dim');
      });
      var failed = (data.items || []).filter(function (it) { return it.error; });
      failed.forEach(function (it) {
        logLine('· 未能解析 ← ' + it.filename + '：' + (it.error || ''), 'warn');
      });
      logLine('解析完成：' + okItems.length + ' 张发票 → ' + panelRows.length + ' 行明细'
        + (panelRows.length && panelRows.length < okItems.length ? '（同「报销场景 + 税率」已合并，合计 ' + (data.total || '') + '）' : '')
        + (failed.length ? '；' + failed.length + ' 张未识别' : ''), failed.length ? 'warn' : 'ok');
      var noScene = panelRows.filter(function (r) { return !r.scene; }).length;
      if (noScene) {
        logLine('有 ' + noScene + ' 行发票未能归到报销场景（票面类目不在场景关键词内）：报销场景需要在 OA 页面手动选择。', 'warn');
      }
    } catch (err) {
      logLine('发票解析失败：' + String((err && err.message) || err) + '（可在下方手动填明细）', 'err');
    }
  }

  function syncAttachVisibility() {
    attachRow.style.display = allETicket() ? 'flex' : 'none';
  }

  /** 发票附件（相关票据）只接受 PDF —— 所有报销场景统一。 */
  function isPdfFile(file) {
    return /\.pdf$/i.test(file.name) || file.type === 'application/pdf';
  }

  function fmtSize(bytes) {
    if (bytes > 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + 'MB';
    return Math.max(1, Math.round(bytes / 1024)) + 'KB';
  }

  function renderFiles() {
    fileList.textContent = '';
    if (!attachmentFiles.length) {
      fileList.appendChild(el('div', 'font-size:11px;color:#a3aaba;', '尚未选择附件'));
      return;
    }
    attachmentFiles.forEach(function (file, index) {
      var row = el('div', 'display:flex;align-items:center;gap:6px;font-size:11.5px;color:#464c5e;');
      row.appendChild(el('span', 'color:#0f6b4f;', '✓'));
      var nameEl = el('span', 'flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;', file.name);
      nameEl.title = file.name;
      row.appendChild(nameEl);
      row.appendChild(el('span', 'color:#a3aaba;font-size:10.5px;', fmtSize(file.size)));
      var del = el('button', 'border:0;background:transparent;color:#a3aaba;cursor:pointer;padding:0 2px;', '×');
      del.type = 'button';
      del.addEventListener('click', function () {
        attachmentFiles.splice(index, 1);
        renderFiles();
        if (isTripScenario()) { parseForRows(); return; }
        if (!attachmentFiles.length) { lastCalcTotal = null; amountHint.style.display = 'none'; }
        else if (calcCheck.checked) calcAmounts();
      });
      row.appendChild(del);
      fileList.appendChild(row);
    });
  }
  renderFiles();

  /** 把最近一次的发票合计写进金额框；开启「向下取整到百位」时按百位截断，并在下方
   *  hint 里括注原始合计（用户手动改金额时 hint 会被清掉）。 */
  function applyCalculatedAmount() {
    if (lastCalcTotal === null || isNaN(lastCalcTotal)) return false;
    var value = lastCalcTotal;
    var floored = false;
    if (floorCheck.checked) {
      var next = Math.floor(value / 100) * 100;
      floored = next !== value;
      value = next;
    }
    amountInput.value = value.toFixed(2);
    if (floored) {
      amountHint.textContent = '已向下取整到百位：' + value.toFixed(2) + '（原 ' + lastCalcTotal.toFixed(2) + '）';
      amountHint.style.display = 'block';
    } else {
      amountHint.textContent = '';
      amountHint.style.display = 'none';
    }
    return floored;
  }

  async function calcAmounts() {
    if (!attachmentFiles.length) return;
    if (!BASE) {
      logLine('无法定位 StaffDeck 服务地址，跳过自动计算（请手动填金额）。', 'warn');
      return;
    }
    calcCheck.disabled = true;
    logLine('正在解析 ' + attachmentFiles.length + ' 张发票的价税合计…', 'dim');
    try {
      var fd = new FormData();
      attachmentFiles.forEach(function (file) { fd.append('files', file, file.name); });
      var res = await fetch(BASE + '/api/enterprise/oa-assistant/invoice-amounts', { method: 'POST', body: fd });
      var data = await res.json();
      if (!res.ok) throw new Error((data && data.detail) || ('HTTP ' + res.status));
      var rowsParsed = data.rows || [];
      if (rowsParsed.length) {
        // 由发票附件识别税率 + 票面种类 → 自动选发票类型（与金额是否勾选无关）
        lastInvoiceRate = String(rowsParsed[0].tax_rate || '');
        var autoType = invoiceTypeValueFromSearch(rowsParsed[0].suggested_invoice_type, lastInvoiceRate)
          || matchInvoiceType(lastInvoiceRate);
        if (autoType) invoiceSelect.value = autoType;
      }
      var failed = (data.items || []).filter(function (item) { return item.error; });
      if (calcCheck.checked) {
        lastCalcTotal = Number(data.total);
        var floored = applyCalculatedAmount();
        var suffix = floored ? '（已向下取整到百位，原 ' + lastCalcTotal.toFixed(2) + '）' : '';
        if (failed.length) {
          logLine('已按可识别发票填写金额 ' + amountInput.value + suffix + '；' + failed.length + ' 张未能解析：' +
            failed.map(function (item) { return item.filename; }).join('、'), 'warn');
        } else {
          logLine('已按发票合计填写金额：' + amountInput.value + suffix, 'ok');
        }
      } else if (rowsParsed.length) {
        logLine('已按发票识别并选择发票类型：' + (invoiceSelect.value || '—'), 'ok');
      }
    } catch (err) {
      logLine('自动计算失败：' + String((err && err.message) || err) + '（可手动填金额）', 'err');
    } finally {
      calcCheck.disabled = false;
    }
  }

  mergeCheck.addEventListener('change', function () {
    refreshAttachLabel();
    if (isTripScenario()) parseForRows();
  });

  pickBtn.addEventListener('click', function () { fileInput.click(); });
  clearFilesBtn.addEventListener('click', function () {
    attachmentFiles = [];
    lastCalcTotal = null;
    amountHint.style.display = 'none';
    renderFiles();
    if (isTripScenario()) { panelRows = []; renderRows(); }
  });
  floorCheck.addEventListener('change', function () {
    if (lastCalcTotal !== null) {
      applyCalculatedAmount();
      logLine(floorCheck.checked
        ? '金额已向下取整到百位：' + amountInput.value + '（原 ' + lastCalcTotal.toFixed(2) + '）'
        : '金额已按发票合计原值填写：' + amountInput.value, 'dim');
    } else if (calcCheck.checked && attachmentFiles.length) {
      calcAmounts();
    }
  });
  fileInput.addEventListener('change', function (e) {
    var picked = Array.prototype.slice.call(e.target.files || []);
    e.target.value = '';
    var invalid = picked.filter(function (file) {
      return !isPdfFile(file);
    });
    if (invalid.length) logLine('已跳过非 PDF 文件：' + invalid.map(function (f) { return f.name; }).join('、'), 'warn');
    var duplicates = [];
    picked.forEach(function (file) {
      if (invalid.indexOf(file) >= 0) return;
      var dup = attachmentFiles.some(function (x) { return x.name === file.name && x.size === file.size; });
      if (dup) { duplicates.push(file.name); return; }
      attachmentFiles.push(file);
    });
    if (duplicates.length) {
      // 同名同大小视为同一张票；说清楚，别让人以为「上传了却没反应」
      logLine('已跳过重复附件（同名同大小已在列表）：' + duplicates.join('、'), 'warn');
    }
    renderFiles();
    if (isTripScenario()) parseForRows();
    else if (attachmentFiles.length) calcAmounts();
  });

  syncAttachVisibility();

  // 填完是否点保存
  var saveWrap = el('label', 'display:flex;align-items:center;gap:7px;cursor:pointer;color:#464c5e;');
  var saveCheck = document.createElement('input');
  saveCheck.type = 'checkbox';
  saveCheck.checked = PANEL_CFG.auto_save_default === true;
  saveWrap.appendChild(saveCheck);
  saveWrap.appendChild(el('span', '', '填完后自动点「保存」'));
  saveWrap.style.order = '13';
  body.appendChild(saveWrap);

  // 执行过程日志：放在「代填」按钮正上方的固定一层（不随表单滚动，单独可滚）
  var logBox = el('div', LOG + 'margin:0 12px 10px;flex:0 0 auto;display:none;', '');

  var footer = el('div', 'display:flex;gap:8px;padding:0 12px 12px;');
  var runBtn = el('button', BTN_PRIMARY, '代填');
  runBtn.type = 'button';
  footer.appendChild(runBtn);
  card.appendChild(logBox);
  card.appendChild(footer);
  root.appendChild(card);

  // ── 拖动面板（按住标题）：变 fixed 的 left/top，位置存 sessionStorage ──
  var POS_KEY = 'sd_oa_panel_pos';
  function applyPos(left, top) {
    var w = card.offsetWidth || 340;
    var h = card.offsetHeight || 400;
    left = Math.min(Math.max(4, left), Math.max(4, window.innerWidth - w - 4));
    top = Math.min(Math.max(4, top), Math.max(4, window.innerHeight - h - 4));
    card.style.left = left + 'px';
    card.style.top = top + 'px';
    card.style.right = 'auto';
  }
  try {
    var savedPos = JSON.parse(sessionStorage.getItem(POS_KEY) || 'null');
    if (savedPos && typeof savedPos.left === 'number') applyPos(savedPos.left, savedPos.top);
  } catch (e) {}
  (function enableDrag() {
    var dragging = false;
    var startX = 0, startY = 0, startLeft = 0, startTop = 0;
    var hostDoc = host.ownerDocument || document;
    head.addEventListener('mousedown', function (e) {
      if (e.target === closeBtn) return;
      var rect = card.getBoundingClientRect();
      dragging = true;
      startX = e.clientX; startY = e.clientY;
      startLeft = rect.left; startTop = rect.top;
      card.style.right = 'auto';
      try { hostDoc.defaultView.getSelection().removeAllRanges(); } catch (err) {}
      e.preventDefault();
    });
    hostDoc.addEventListener('mousemove', function (e) {
      if (!dragging) return;
      applyPos(startLeft + (e.clientX - startX), startTop + (e.clientY - startY));
    });
    hostDoc.addEventListener('mouseup', function () {
      if (!dragging) return;
      dragging = false;
      try {
        sessionStorage.setItem(POS_KEY, JSON.stringify({
          left: parseInt(card.style.left, 10) || 0,
          top: parseInt(card.style.top, 10) || 0
        }));
      } catch (err) {}
    });
    window.addEventListener('resize', function () {
      if (card.style.left) applyPos(parseInt(card.style.left, 10) || 0, parseInt(card.style.top, 10) || 0);
    });
  })();

  function logLine(text, tone) {
    logBox.style.display = 'block';
    var color = { ok: '#0f6b4f', warn: '#a4650a', err: '#a4342c', dim: '#858b9c' }[tone] || '#5b6274';
    var line = el('div', 'color:' + color + ';', text);
    logBox.appendChild(line);
    logBox.scrollTop = logBox.scrollHeight;
  }

  function resetLog(text) { logBox.textContent = ''; logBox.style.display = text ? 'block' : 'none'; if (text) logLine(text, 'dim'); }

  function currentScenario() {
    var id = scenarioSelect.value;
    for (var i = 0; i < scenarios.length; i++) if (scenarios[i].id === id) return scenarios[i];
    return scenarios[0];
  }

  function allETicket() {
    var picked = radioWrap.querySelector("input[name='sd-oa-eticket']:checked");
    return picked ? picked.value === 'yes' : false;
  }

  function lastMonth() {
    var month = new Date().getMonth() + 1;
    return month === 1 ? 12 : month - 1;
  }

  function refreshPrefill() {
    var scenario = currentScenario();
    if (!scenario) return;
    var template = scenario.reason_prefill || '';
    reasonInput.value = template.replace(/\{last_month\}/g, String(lastMonth()));
  }

  /** 附件区标题：跟着「合并」开关与当前场景走，让用户一眼看出会生成几行。 */
  function refreshAttachLabel() {
    var scenario = currentScenario() || {};
    if (!scenario.line_items) {
      attachRow.firstChild.textContent = '电子发票附件（挂到 OA「相关票据」区）';
      return;
    }
    attachRow.firstChild.textContent = mergeCheck.checked
      ? '相关票据（税率相同合并为一行）'
      : '发票附件与明细（一张发票一行，可改）';
  }

  /** 按当前场景切换面板上的条件项：提示、报销场景二选一、出差天数。 */
  function syncScenarioFields() {
    var scenario = currentScenario();
    if (!scenario) return;

    var notices = (PANEL_CFG.notices || []).concat(scenario.notices || []);
    noticeBox.textContent = '';
    notices.forEach(function (n) { noticeBox.appendChild(el('div', '', '⚠ ' + n)); });
    noticeBox.style.display = notices.length ? 'flex' : 'none';

    // 「↗ 打开表单」：按所选场景的 url 在当前页跳转
    var scenarioUrl = scenario.url || '';
    if (scenarioUrl) {
      scenarioLink.href = scenarioUrl;
      scenarioLink.style.display = '';
    } else {
      scenarioLink.removeAttribute('href');
      scenarioLink.style.display = 'none';
    }

    var options = scenario.scene_options || [];
    sceneSeg.textContent = '';
    if (options.length) {
      if (!options.some(function (o) { return o.value === sceneChoice; })) sceneChoice = options[0].value;
      options.forEach(function (o, idx) {
        var on = o.value === sceneChoice;
        var btn = el('button', 'flex:1;border:0;padding:6px 4px;font-size:12px;cursor:pointer;font-family:inherit;background:' + (on ? '#18181a' : '#fff') + ';color:' + (on ? '#fff' : '#5b6274') + ';' + (idx > 0 ? 'border-left:1px solid #e3e7f1;' : ''), o.label);
        btn.type = 'button';
        btn.addEventListener('click', function () { sceneChoice = o.value; syncScenarioFields(); });
        sceneSeg.appendChild(btn);
      });
      sceneSegRow.style.display = 'flex';
    } else {
      sceneSegRow.style.display = 'none';
    }

    var needsTripDays = (scenario.fields || []).indexOf('trip_days') >= 0;
    tripDaysRow.style.display = needsTripDays ? 'flex' : 'none';

    var needsRelatedFlow = (scenario.fields || []).indexOf('related_flow') >= 0;
    relatedFlowRow.style.display = needsRelatedFlow ? 'flex' : 'none';
    // 显示位置：境内公司付款放在「收款单位名称」下面；出差报销仍跟「实际出差天数」同段
    relatedFlowRow.style.order = (scenario.fields || []).indexOf('payee_name') >= 0 ? '7' : '5';

    // 出差报销：发票类型/明细与附件同区（一张发票一行，各自税率）；报销金额在最后
    var isTrip = !!scenario.line_items;
    invoiceRow.style.display = isTrip ? 'none' : 'flex';
    eticketRow.style.display = isTrip ? 'none' : 'flex';
    rowsWrap.style.display = isTrip ? 'flex' : 'none';
    if (scenario.id !== mergeDefaultScenarioId) {
      mergeDefaultScenarioId = scenario.id;
      mergeCheck.checked = (scenario.line_items || {}).group_by !== 'invoice';
    }
    refreshAttachLabel();
    calcWrap.style.display = isTrip ? 'none' : 'flex';
    floorWrap.style.display = isTrip ? 'none' : 'flex';
    otherRefsRow.style.display = (scenario.fields || []).indexOf('other_refs') >= 0 ? 'flex' : 'none';
    payeeNameRow.style.display = (scenario.fields || []).indexOf('payee_name') >= 0 ? 'flex' : 'none';
    amountRow.firstChild.textContent = isTrip ? '报销金额（明细合计）' : '报销金额（默认值，可改）';
    amountInput.readOnly = isTrip;
    if (isTrip) { amountHint.textContent = ''; amountHint.style.display = 'none'; }
    if (isTrip) refreshRowTotal();
  }

  scenarioSelect.addEventListener('change', function () { refreshPrefill(); syncScenarioFields(); });
  refreshPrefill();
  syncScenarioFields();

  closeBtn.addEventListener('click', function () { collapse(); });

  function collapse() { card.style.display = 'none'; }

  function report(payload) {
    if (!BASE || !navigator.sendBeacon) return;
    try {
      var body = new Blob([JSON.stringify(payload)], { type: 'text/plain;charset=UTF-8' });
      navigator.sendBeacon(BASE + '/api/enterprise/oa-assistant/report', body);
    } catch (e) {}
  }

  /** 填多行报销明细：先按需点「新增」补行，再逐行填 row_fields（selector 里的 {i} 换成行号）。 */
  /** 读第 i 行明细的 OA「税率」列（一般是小数，如 0.06），换成百分数 6；读不到返回 null。 */
  function readRowRate(rowIndex) {
    var node = findByLabelText('税率', 'input', rowIndex);
    if (!node) return null;
    var v = String(node.value || '').trim();
    if (!v) return null;
    var n = Number(v);
    if (isNaN(n)) return null;
    return n > 1.5 ? Math.round(n) : Math.round(n * 100);
  }

  /**
   * 发票类型按税率选：OA 下拉只显示类型名（税率只在悬浮提示里，读不到），所以
   * 逐个选项点选后读该行的「税率」列验证，命中期望税率才保留；否则重开下拉试下一个。
   */
  async function fillSelectByRate(container, spec, keyword, prefer, rowIndex) {
    var wantNum = Number(String(prefer).replace(/[^0-9.]/g, ''));
    var picked = await fillSelect(container, spec, keyword, prefer);
    if (!wantNum) return picked;
    var initial = readRowRate(rowIndex);
    if (initial === null) return picked; // OA 没有可读的税率列 → 只能用常规结果
    if (initial === wantNum) return picked;
    var tried = [];
    for (var attempt = 0; attempt < 5; attempt++) {
      var doc = container.ownerDocument || document;
      var opts = visibleOptions(doc, keyword);
      if (!opts.length) break;
      var progressed = false;
      for (var i = 0; i < opts.length; i++) {
        if (tried.indexOf(opts[i]) >= 0) continue;
        tried.push(opts[i]);
        progressed = true;
        clickEl(opts[i]);
        await sleep(450);
        if (readRowRate(rowIndex) === wantNum) return optionText(opts[i]).trim();
        // 未命中：重新打开下拉、重新搜索，继续试下一个
        clickEl(visibleIn(container, '.ant-select-selection') || container);
        await sleep(300);
        var search = visibleIn(container, '.ant-select-search__field');
        if (search) { setNative(search, keyword); fireEvent(search, 'input'); await sleep(300); }
      }
      if (!progressed) break;
    }
    return picked;
  }

  /** 明细行「-」删除按钮候选（选择器兜底之外，还会按文本/属性找，见 findDeleteButtonIn）。 */
  var DEFAULT_DEL_ROW_SELECTOR = [
    "[title*='删除']", "[title*='移除']", "[title='-']", "[title*='减']",
    "a[class*='delete']", "i[class*='delete']", "span[class*='delete']", "td[class*='delete']",
    "a[class*='remove']", "i[class*='remove']", "span[class*='remove']",
    "a[class*='del']", "i[class*='del']", "span[class*='del']",
    "a[class*='minus']", "i[class*='minus']", "span[class*='minus']",
    "a:has-text('－')", "i:has-text('－')", "span:has-text('－')", "a:has-text('-')",
    "a:has-text('删除')", "i:has-text('删除')", "span:has-text('删除')",
  ].join(',');

  /** 明细行「{i}」字段定位符（优先用「报销场景」，没有就用「金额」「事项描述」）。 */
  function rowFieldSelector(li) {
    var fields = li.row_fields || {};
    var spec = fields.scene || fields.amount || fields.item_desc;
    var sel = spec && spec.selector;
    if (!sel || sel === TODO || String(sel).indexOf('{i}') < 0) return null;
    return String(sel);
  }

  /** 当前 OA 上实际存在的明细行下标（探测 {i}；删除/新增后可能不从 0 开始或有缺口）。 */
  function lineItemRowIndexes(li) {
    var sel = rowFieldSelector(li);
    if (!sel) return null;
    var out = [];
    for (var i = 0; i < 20; i++) {
      if (findEl(sel.replace(/\{i\}/g, String(i)))) out.push(i);
    }
    return out;
  }

  /** 第 i 行的「行容器」：表格行就是 tr；div 版明细则取「包含本行字段、不再包含下一行字段」的那层。 */
  function rowContainerOf(li, i, indexes) {
    var sel = rowFieldSelector(li);
    var own = sel ? findEl(sel.replace(/\{i\}/g, String(i))) : null;
    if (!own) return null;
    var later = (indexes || []).filter(function (x) { return x > i; })[0];
    var next = later === undefined ? null : findEl(sel.replace(/\{i\}/g, String(later)));
    var node = own;
    var last = own;
    for (var d = 0; d < 8 && node && node.parentElement; d++) {
      node = node.parentElement;
      if (node.tagName === 'TR') return node;
      if (next && !node.contains(next)) return last; // 再往上就跨到下一行了 → 上一层就是行容器
      last = node;
    }
    return last;
  }

  /** 行的搜索范围：行容器 + 紧邻的兄弟 tr（有的 OA 把一行拆在多条 tr 里，
   *  复选框/「-」在下一行 tr 上）。兄弟 tr 里一旦出现「别的行」的明细字段就停。 */
  function rowScopeOf(li, i, indexes) {
    var box = rowContainerOf(li, i, indexes);
    if (!box) return [];
    var scopes = [box];
    var sel = rowFieldSelector(li);
    function hasOtherRow(node) {
      if (!sel) return false;
      for (var t = 0; t < (indexes || []).length; t++) {
        if (indexes[t] === i) continue;
        if (queryInDoc(node, sel.replace(/\{i\}/g, String(indexes[t]))).length) return true;
      }
      return false;
    }
    function hasControls(node) {
      try {
        return !!node.querySelector("input[type='checkbox'], .ant-checkbox, .ant-checkbox-wrapper,"
          + " [title*='删除'], [class*='del'], [class*='remove']");
      } catch (e) { return false; }
    }
    if (box.tagName === 'TR') {
      var next = box.nextElementSibling;
      for (var hops = 0; next && next.tagName === 'TR' && hops < 3; hops++) {
        if (hasOtherRow(next)) break;
        scopes.push(next);
        if (hasControls(next)) break;
        next = next.nextElementSibling;
      }
    }
    return scopes;
  }

  /** 第 i 行前面的复选框：OA 的「-」按钮要先勾中该行才生效。
   *  优先取行范围内的复选框；取不到就退到整片明细区域里按「第几行」取第几个（跳过表头）。 */
  function rowCheckboxOf(li, i, indexes) {
    var scopes = rowScopeOf(li, i, indexes);
    var box = rowContainerOf(li, i, indexes);
    var sel = "input[type='checkbox'], .ant-checkbox, .ant-checkbox-wrapper, [class*='checkbox']";
    for (var s = 0; s < scopes.length; s++) {
      var list = queryInDoc(scopes[s], sel);
      for (var j = 0; j < list.length; j++) {
        if (isVisible(list[j]) && !list[j].disabled) return list[j];
      }
    }
    var scope = box && box.parentElement;
    if (!scope) return null;
    var all;
    try { all = scope.querySelectorAll("input[type='checkbox'], .ant-checkbox, .ant-checkbox-wrapper"); } catch (e) { return null; }
    var visible = [];
    for (var k = 0; k < all.length; k++) {
      if (!isVisible(all[k]) || all[k].disabled) continue;
      if (all[k].closest && all[k].closest('th, thead')) continue; // 表头的「全选」不算
      visible.push(all[k]);
    }
    var ordinal = (indexes || []).filter(function (x) { return x <= i; }).length - 1;
    return visible[ordinal] || null;
  }

  /** 勾选第 i 行前面的复选框（点不中就再点一次真正的 input）。 */
  async function selectRowForDelete(li, i, indexes) {
    var cb = rowCheckboxOf(li, i, indexes);
    if (!cb) return false;
    clickEl(cb);
    await sleep(250);
    var real = cb.tagName === 'INPUT' ? cb : cb.querySelector("input[type='checkbox']");
    if (real && !real.checked && real.click) {
      try { real.click(); } catch (e) {}
      await sleep(250);
    }
    return true;
  }

  /** 在一个范围里找「删除本行」的按钮：先按选择器，再按文本/属性兜底（e-cology 的图标没 title 也能认）。 */
  function findDeleteButtonIn(scope, delSel, clicked, accept) {
    if (!scope) return null;
    function usable(el) {
      return el && !clicked[el] && isVisible(el) && !el.disabled && (!accept || accept(el));
    }
    var cands = queryInDoc(scope, delSel);
    for (var c = 0; c < cands.length; c++) if (usable(cands[c])) return cands[c];
    // 兜底②：文本是「- / － / × / 删除 / 移除」的可见小元素
    var all;
    try { all = scope.querySelectorAll('a, i, span, button, em, td, div'); } catch (e) { all = []; }
    for (var i = 0; i < all.length; i++) {
      if (!usable(all[i])) continue;
      var t = normText(all[i].innerText || all[i].textContent || '');
      if (t === '-' || t === '－' || t === '−' || t === '×' || t === '✕' || t === '删除' || t === '移除') return all[i];
    }
    // 兜底③：class/title 里带 del/delete/remove/minus 的图标元素
    for (var j = 0; j < all.length; j++) {
      if (!usable(all[j])) continue;
      var attr = (all[j].getAttribute('title') || '') + ' ' + (all[j].getAttribute('aria-label') || '')
        + ' ' + String(all[j].className || '');
      if (/(^|[\s_-])(del|delete|remove|minus|reduce)([\s_-]|$)/i.test(attr)) return all[j];
    }
    return null;
  }

  /** 第 i 行是否已经有内容（上一次代填留下的）。空行不用删——留着直接复用，
   *  既少点一次「新增」，也避免「删掉最后一行后表单不允许」的坑。 */
  function rowHasContent(li, i) {
    var fields = li.row_fields || {};
    var keys = ['scene', 'invoice_type', 'item_desc', 'amount'];
    for (var k = 0; k < keys.length; k++) {
      var spec = fields[keys[k]];
      if (!spec || !spec.selector || spec.selector === TODO) continue;
      var node = findEl(String(spec.selector).replace(/\{i\}/g, String(i)));
      if (!node) continue;
      if (keys[k] === 'scene' || keys[k] === 'invoice_type') {
        var box = selectContainerOf(node);
        var hidden = box.querySelector ? box.querySelector("input[type='hidden']") : null;
        if (hidden && String(hidden.value || '').trim()) return true;
        var rendered = box.querySelector ? box.querySelector('.ant-select-selection__rendered, .wea-select-selection') : null;
        if (rendered && String(rendered.innerText || rendered.textContent || '').trim()) return true;
      } else {
        var v = node.value !== undefined ? node.value : (node.innerText || node.textContent || '');
        if (String(v || '').trim()) return true;
      }
    }
    return false;
  }

  /** 候选元素是否属于第 i 行：所在 tr 含本行的明细字段，且不含别的行的明细字段。 */
  function belongsToRow(li, i, indexes, node) {
    var tr = node.closest ? node.closest('tr') : null;
    if (!tr) return true; // 不是表格行（div 版明细）→ 交给行范围本身判断
    var sel = rowFieldSelector(li);
    if (!sel) return true;
    for (var t = 0; t < (indexes || []).length; t++) {
      if (indexes[t] === i) continue;
      if (queryInDoc(tr, sel.replace(/\{i\}/g, String(indexes[t]))).length) return false;
    }
    return true;
  }

  /** 删掉 OA 上已经存在的明细行（用户可能填错了想重来；第一次没有明细时自然删不到）。
   *  从最后一行往前删：先在该行容器里找「-」，再退到整片明细区域里找（仍要求命中行内）。
   *  点不动（不可见 / 点完行数不变）就停手，并把该行 HTML 片段打到日志里方便排查。 */
  async function clearLineItemRows(li, steps) {
    var delSel = li.del_row_selector && li.del_row_selector !== TODO ? li.del_row_selector : DEFAULT_DEL_ROW_SELECTOR;
    var clicked = {};
    var removed = 0;
    var warned = false;
    for (var guard = 0; guard < 20; guard++) {
      var idxs = lineItemRowIndexes(li);
      if (!idxs || !idxs.length) break;
      // 只删「有内容」的行（上次代填留下的）；空行留着复用
      var last = null;
      for (var r = idxs.length - 1; r >= 0; r--) {
        if (rowHasContent(li, idxs[r])) { last = idxs[r]; break; }
      }
      if (last === null) break;
      var scopes = rowScopeOf(li, last, idxs);
      if (!scopes.length) break;
      var box = scopes[0];
      // OA 的「-」要先勾中该行：先点复选框，再找删除按钮（勾选后按钮才可点）
      var checked = await selectRowForDelete(li, last, idxs);
      if (checked && !removed) logLine('－ 「-」需先勾选行：已勾选第 ' + (last + 1) + ' 行', 'dim');
      // 「属于第 last 行」的过滤：其所在 tr 若含「别的行」的明细字段就否掉，
      // 这样即使退到整张明细表里找，也不会误点成别人那一行的「-」。
      var accept = function (el) { return belongsToRow(li, last, idxs, el); };
      var btn = null;
      for (var sc = 0; sc < scopes.length && !btn; sc++) btn = findDeleteButtonIn(scopes[sc], delSel, clicked, accept);
      if (!btn && box.parentElement) btn = findDeleteButtonIn(box.parentElement, delSel, clicked, accept);
      if (!btn && box.parentElement && box.parentElement.parentElement) {
        btn = findDeleteButtonIn(box.parentElement.parentElement, delSel, clicked, accept);
      }
      if (!btn) {
        if (!warned) {
          warned = true;
          var html = String(box.outerHTML || box.innerHTML || '').replace(/\s+/g, ' ').slice(0, 240);
          logLine('! 报销明细：没找到行内「-」删除按钮，' + (removed ? '已删 ' + removed + ' 行，' : '')
            + (checked ? '（已勾选该行仍未出现）' : '（该行也没找到可勾的复选框）') + '剩余明细行请手动删除', 'warn');
          logLine('  第 ' + (last + 1) + ' 行 HTML 片段：' + html, 'dim');
        }
        break;
      }
      clicked[btn] = true;
      clickEl(btn);
      removed++;
      await sleep(600);
      var after = lineItemRowIndexes(li);
      if (!after || after.length >= idxs.length) break; // 点了没减少，别死循环
    }
    if (removed) {
      steps.push({ field: '报销明细', status: 'filled', detail: '删除已有明细 ' + removed + ' 行' });
      logLine('－ 已删除 ' + removed + ' 行已有明细', 'dim');
    }
    return removed;
  }

  async function fillLineItems(scenario, rows, steps) {
    var li = scenario.line_items || {};
    var rowFields = li.row_fields || {};
    var order = ['scene', 'invoice_type', 'item_desc', 'amount'];

    // 先清掉 OA 上已有的明细行（重新代填时别把旧的留着）
    await clearLineItemRows(li, steps);

    var idxs = lineItemRowIndexes(li);
    var have = idxs ? idxs.length : Number(li.initial_rows || 1);
    if (rows.length > have) {
      var addSel = li.add_row_selector;
      if (!addSel || addSel === TODO) {
        steps.push({ field: '报销明细', status: 'todo', detail: '「新增」按钮定位器待配置' });
        logLine('! 报销明细：「新增」按钮定位器待配置，只填第 1 行', 'warn');
      } else {
        for (var k = have; k < rows.length; k++) {
          var addNode = findEl(addSel);
          if (!addNode) {
            steps.push({ field: '报销明细', status: 'error', detail: '未找到「新增」按钮（页面按钮：' + sampleButtons().join('、') + '）' });
            logLine('✗ 报销明细：未找到新增按钮，只能填已有行', 'err');
            break;
          }
          clickEl(addNode);
          await sleep(700);
          logLine('＋ 已点「新增」，准备填第 ' + (k + 1) + ' 行', 'dim');
        }
      }
    }
    // 补行后重新探测一次：行下标可能不从 0 开始（有的 OA 删行后不重排）
    if (idxs) {
      idxs = lineItemRowIndexes(li) || idxs;
      if (have < rows.length && idxs.length > have) have = idxs.length;
      if (have > rows.length) {
        logLine('! 报销明细：OA 上还剩 ' + have + ' 行，只填前 ' + rows.length + ' 行，多余的行请手动删除', 'warn');
      }
    }

    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      for (var o = 0; o < order.length; o++) {
        var key = order[o];
        var spec = rowFields[key];
        if (!spec) continue;
        var label = '明细第' + (i + 1) + '行·' + (spec.label || key);
        var value = row[key];
        if (value === undefined || value === null || value === '') {
          steps.push({ field: label, status: 'skipped', detail: '无值' });
          logLine('○ ' + label + '：无值，跳过', 'dim');
          continue;
        }
        var node = null;
        // 用实际存在的行下标：删行后 OA 可能不重排（删掉第 1 行后剩下的仍是 _2），
        // 补行后新行下标也可能不从 0 开始——按探测到的下标集合对齐。
        var rowIdx = (idxs && idxs[i] !== undefined) ? idxs[i] : i;
        var sel = String(spec.selector || '').replace(/\{i\}/g, String(rowIdx));
        if (spec.selector && spec.selector !== TODO) node = findEl(sel);
        // fieldmark 找不到时，回退到按标签（第 i 个）定位
        if (!node && spec.by_label) {
          node = findByLabelText(typeof spec.by_label === 'string' ? spec.by_label : (spec.label || key), spec.mode || 'input', i);
        }
        if (!node) {
          var missDetail = spec.by_label
            ? ('未找到 ' + sel + ' / 标签「' + spec.by_label + '」' + (i === 0 ? '（页面标签：' + sampleLabels().join('、') + '）' : ''))
            : ('未找到 ' + sel);
          steps.push({ field: label, status: 'error', detail: missDetail });
          logLine('✗ ' + label + '：' + missDetail, 'err');
          continue;
        }
        try {
          var prefer = '';
          if (key === 'invoice_type' && row.invoice_rate) prefer = String(row.invoice_rate) + '%';
          var pickedText = '';
          if ((spec.mode || 'input') === 'select') {
            if (key === 'invoice_type' && prefer) pickedText = (await fillSelectByRate(node, spec, String(value), prefer, i)) || '';
            else pickedText = (await fillSelect(node, spec, String(value), prefer)) || '';
          } else {
            fillText(node, String(value), spec.mode || 'input');
          }
          steps.push({ field: label, status: 'filled', detail: pickedText || String(value) });
          logLine('✓ ' + label + '：' + (pickedText || value), 'ok');
        } catch (err) {
          steps.push({ field: label, status: 'error', detail: String((err && err.message) || err) });
          logLine('✗ ' + label + '：' + String((err && err.message) || err), 'err');
        }
        await sleep(200);
      }
    }
  }

  async function run() {
    var scenario = currentScenario();
    if (!scenario) { logLine('没有可用场景', 'err'); return; }

    runBtn.disabled = true;
    runBtn.textContent = '执行中…';
    var steps = [];
    resetLog('');
    logLine('开始执行…', 'dim');
    logLine('场景：' + scenario.name + '（' + (scenario.id) + '）');

    if (isLoginPage()) {
      logLine('当前看起来是 OA 登录页：请先登录，回到报销表单页后再点「代填」。', 'warn');
      runBtn.disabled = false;
    runBtn.textContent = '代填';
      return;
    }

    var isTrip = !!scenario.line_items;
    if (isTrip && !panelRows.length) {
      logLine('请先选择发票，自动生成报销明细后再点「代填」。', 'err');
      runBtn.disabled = false;
    runBtn.textContent = '代填';
      return;
    }
    // 未归类提醒要在「执行日志」里再说一次：解析完成后用户往往会再点「代填」，
    // 而 run() 会清空日志，只在解析时提示一次等于提示丢失。
    if (isTrip) {
      var noSceneRows = panelRows.filter(function (r) { return !r.scene; });
      if (noSceneRows.length) {
        logLine('注意：有 ' + noSceneRows.length + ' 行明细没有报销场景（票面类目未匹配到场景关键词），'
          + '这几行的「报销场景」需要在 OA 页面手动选择。', 'warn');
      }
    }

    var amountValue = String(amountInput.value || '').trim();
    if (!isTrip && (!amountValue || isNaN(Number(amountValue)))) {
      logLine('报销金额未填写或格式不对（应为数字），已中止。', 'err');
      runBtn.disabled = false;
    runBtn.textContent = '代填';
      return;
    }

    var eTicket = allETicket();
    var files = attachmentFiles.slice();
    var filesByField = { attachment_paths: files, other_ref_files: otherRefFiles.slice() };
    if (eTicket && !files.length) {
      logLine('全为电子票但未选择附件：附件项会跳过，可在 OA 页面手动挂票据。', 'warn');
    }

    var sceneValue = (scenario.scene_options && scenario.scene_options.length)
      ? sceneChoice
      : (scenario.scene || '');

    var li = scenario.line_items || {};

    var values = {
      amount: amountValue,
      reason: String(reasonInput.value || '').trim(),
      scene: sceneValue,
      trip_days: String(tripDaysInput.value || '').trim(),
      related_flow: String(relatedFlowInput.value || '').trim(),
      company_name: companySelect.value,
      payee_name: String(payeeNameInput.value || '').trim(),
      invoice_type: invoiceTypeSearch(invoiceSelect.value),
      invoice_rate: invoiceTypeRate(invoiceSelect.value),
      all_e_ticket: eTicket,
      attachment_paths: files.map(function (f) { return f.name; }),
      other_ref_files: otherRefFiles.map(function (f) { return f.name; }),
      rows: isTrip ? panelRows.map(function (r) {
        return {
          scene: r.scene,
          invoice_type: invoiceTypeSearch(r.invoice_type),
          invoice_rate: invoiceTypeRate(r.invoice_type),
          amount: r.amount,
          item_desc: r.item_desc
        };
      }) : []
    };
    values['all_e_ticket=yes'] = eTicket;

    if ((scenario.fields || []).indexOf('trip_days') >= 0 && !values.trip_days) {
      logLine('未填写出差天数：该项会跳过，请在 OA 页面手动补。', 'warn');
    }
    if ((scenario.fields || []).indexOf('related_flow') >= 0 && !values.related_flow) {
      logLine('未填写相关流程关键字：该项会跳过，请在 OA 页面手动选择流程。', 'warn');
    }

    var fields = resolveFields(scenario);
    for (var i = 0; i < fields.length; i++) {
      var spec = fields[i][1];
      if (spec.enabled === false) {
        steps.push({ field: spec.label || spec.key, status: 'skipped', detail: '按场景配置跳过' });
        logLine('○ ' + (spec.label || spec.key) + '：按场景配置跳过', 'dim');
        continue;
      }
      var when = spec.when;
      if (when && when in values && !values[when]) {
        steps.push({ field: spec.label || spec.key, status: 'skipped', detail: '条件 ' + when + ' 不满足' });
        logLine('○ ' + (spec.label || spec.key) + '：条件不满足，跳过', 'dim');
        continue;
      }
      try {
        var step = await fillField(spec, values, filesByField);
        steps.push(step);
        if (step.status === 'filled') logLine('✓ ' + step.field + '：' + step.detail, 'ok');
        else if (step.status === 'todo') logLine('! ' + step.field + '：' + step.detail, 'warn');
        else logLine('○ ' + step.field + '：' + step.detail, 'dim');
      } catch (err) {
        steps.push({ field: spec.label || spec.key, status: 'error', detail: String((err && err.message) || err) });
        logLine('✗ ' + (spec.label || spec.key) + '：' + String((err && err.message) || err), 'err');
      }
    }

    if (isTrip && values.rows.length) {
      await fillLineItems(scenario, values.rows, steps);
    }

    if (saveCheck.checked) {
      var saveSpec = CFG.save_button || {};
      var saveSel = saveSpec.selector;
      if (!saveSel || saveSel === TODO) {
        steps.push({ field: saveSpec.label || '保存', status: 'todo', detail: '保存按钮定位器待配置' });
        logLine('! 保存：定位器待配置', 'warn');
      } else {
        var saveNode = findEl(saveSel);
        if (!saveNode) {
          steps.push({ field: saveSpec.label || '保存', status: 'error', detail: '未找到保存按钮' });
          logLine('✗ 保存：未找到按钮', 'err');
        } else {
          clickEl(saveNode);
          steps.push({ field: saveSpec.label || '保存', status: 'filled', detail: '已点击保存' });
          logLine('✓ 保存：已点击', 'ok');
        }
      }
    } else {
      logLine('（未勾选自动保存，OA 表单只填不提交）', 'dim');
    }

    var failed = steps.filter(function (s) { return s.status === 'error'; });
    var todos = steps.filter(function (s) { return s.status === 'todo'; });
    logLine('完成：成功 ' + steps.filter(function (s) { return s.status === 'filled'; }).length +
      ' · 跳过 ' + steps.filter(function (s) { return s.status === 'skipped'; }).length +
      ' · 失败 ' + failed.length + ' · 待配置 ' + todos.length,
      failed.length ? 'err' : 'ok');
    report({ kind: 'run', scenario_id: scenario.id, url: location.href, steps: steps, at: new Date().toISOString() });
    runBtn.disabled = false;
    runBtn.textContent = '代填';
  }

  runBtn.addEventListener('click', function () { run(); });

  window.__SD_OA_PANEL__ = {
    toggle: function () { card.style.display = card.style.display === 'none' ? 'flex' : 'none'; },
    open: function () { card.style.display = 'flex'; },
    close: function () { card.style.display = 'none'; },
    // 卸载旧面板：书签每次带 ?t= 重新加载本文件前先调它，保证拿到的是最新面板
    destroy: function () {
      try { if (host.parentNode) host.parentNode.removeChild(host); } catch (e) {}
      try { window.__SD_OA_PANEL__ = null; } catch (e) {}
    },
  };

})();