"""本地 Mock OA（开发自测用）。

模拟 e-cology「境内个人费用报销审批表」的关键结构：
- 表单放在 iframe 里（对应真实 OA 的 static4form），验证面板的跨 frame 定位；
- 报销场景 / 发票类型等用简化的 ant-select 复刻（.ant-select-search__field +
  .ant-select-dropdown + hidden input，提交判据与真 OA 一致）；
- 明细行字段名带 _0 后缀，「新增」按钮复制出行 1。

端点（复用真实 oa_assistant 路由，因此 panel.js / invoice-amounts / report 都是真的）：
    GET  /oa         外层页（iframe 容器，书签面板注入在这里）
    GET  /oa-form    iframe 里的表单

用法：
    cd backend && uv run python scripts/mock_oa_server.py 8899
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api import oa_assistant  # noqa: E402

app = FastAPI()
app.include_router(oa_assistant.router)

_OUTER = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>Mock OA</title></head>
<body style="margin:0;font:12px -apple-system,'PingFang SC',sans-serif">
<div style="padding:8px 12px;background:#2b3a67;color:#fff">Mock OA（本地自测）· 真实 OA 表单在下面 iframe 里</div>
<iframe src="/oa-form" style="width:100%;height:680px;border:0"></iframe>
</body></html>"""

_FORM = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>报销审批表</title>
<style>
  body{font:13px -apple-system,'PingFang SC',sans-serif;color:#1f2333;padding:16px}
  table{border-collapse:collapse;width:100%}
  td{border:1px solid #e3e7f1;padding:6px 8px;vertical-align:middle}
  td.lb{background:#fafbfd;color:#5b6274;width:130px}
  input[type=text],input[type=number],input:not([type]){height:24px;width:100%;box-sizing:border-box;
    border:1px solid #e3e7f1;border-radius:6px;padding:0 6px;font:inherit}
  .ant-select{position:relative;min-height:24px}
  .ant-select-selection{border:1px solid #e3e7f1;border-radius:6px;padding:3px 6px;min-height:18px}
  .ant-select-search__field{position:absolute;top:4px;left:6px;right:6px;width:calc(100% - 12px);
    height:18px;border:0;outline:0;background:transparent;font:inherit}
  .ant-select-dropdown{position:absolute;z-index:100;background:#fff;border:1px solid #e3e7f1;
    border-radius:6px;box-shadow:0 6px 18px rgba(0,0,0,.12);max-height:180px;overflow:auto;min-width:160px}
  .ant-select-dropdown-hidden{display:none}
  .ant-select-dropdown-menu-item{list-style:none;padding:5px 10px;cursor:pointer;font:12px inherit}
  .ant-select-dropdown-menu-item:hover{background:#f2f4f9}
  .wea-upload-list-item-ops .anticon-cross{display:inline-block;width:14px;height:14px;float:right;margin:4px;
    background:url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14"><path d="M2 2 L12 12 M12 2 L2 12" stroke="%23888" stroke-width="1.6"/></svg>') no-repeat}
  ul{margin:0;padding:0}
  #addrow{display:inline-block;margin:10px 0;color:#3f6fd8}
  button.ant-btn{margin-left:12px;padding:5px 16px;border:1px solid #e3e7f1;border-radius:8px;background:#18181a;color:#fff}
  #saved{color:#0f6b4f;margin-top:10px}
</style></head>
<body>
<table>
  <tr><td class="lb">报销类型</td><td><div class="ant-select" data-fieldmark="field6970" data-options="差旅费用|日常费用|公务限额费用"></div></td>
      <td class="lb">公司名称</td><td><div class="ant-select" data-fieldmark="field6969" data-options="上海复星旅游管理有限公司|珠海市横琴复星旅文旅游发展有限公司"></div></td></tr>
  <tr><td class="lb">是否含超标</td><td><div class="ant-select" data-fieldmark="field7014" data-options="是|否"></div></td>
      <td class="lb">是否差旅/业务招待报销</td><td><div class="ant-select" data-fieldmark="field6974" data-options="是|否"></div></td></tr>
  <tr><td class="lb">报销事由</td><td colspan="3"><input id="field6946"></td></tr>
  <tr><td class="lb">实际出差天数</td><td><input id="tripdays"></td><td></td><td></td></tr>
  <tr><td class="lb">相关流程</td><td colspan="3"><div class="ant-select" data-options="出差申请-0918 上海行|出差申请-0925 北京行|日常费用报销"></div></td></tr>
  <tr><td class="lb">相关票据</td><td><div data-fieldmark="field6951" class="wea-upload wea-field">
      <div class="wea-upload-list"></div>
      <div class="wea-upload-container"><input type="file" multiple></div></div></td>
      <td class="lb">其他参考资料</td><td><div data-fieldmark="field6973" class="wea-upload wea-field">
      <div class="wea-upload-list"></div>
      <div class="wea-upload-container"><input type="file" multiple></div></div></td></tr>
  <tr><td class="lb">是否全电子发票</td><td><div class="ant-select" data-fieldmark="field29328" data-options="是|否"></div></td><td></td><td></td></tr>
  <tr><td class="lb">费用承担部门</td><td><div class="ant-select" data-fieldmark="field7012" data-prefill="AI+" data-options="AI+|核定费用承担部门|核定"></div></td><td></td><td></td></tr>
  <tr><td class="lb">收款单位名称</td><td><div class="ant-select" data-options="上海测试科技有限公司|某某网络有限公司|上海测试"></div></td><td></td><td></td></tr>
  <tr><td class="lb">报销场景</td><td><div class="ant-select" data-fieldmark="field6926_0" data-options="通讯费用|交通费用|飞机|火车汽车船|IT服务外包|相关票据|境内住宿|境内机票|境内火车票"></div></td>
      <td class="lb">发票类型</td><td><div class="ant-select" data-fieldmark="field6925_0" data-options="普通发票|增值税普通发票 ｜ 3%|增值税普通发票 ｜ 6%|增值税普通发票 ｜ 9%|增值税专用发票|增值税专用发票 ｜ 3%|增值税专用发票 ｜ 6%|增值税专用发票 ｜ 9%|增值税专用发票 ｜ 13%|增值税电子普通发票（境内客运）|增值税电子普通发票（境内客运） ｜ 3%|增值税电子普通发票（境内客运） ｜ 9%|电子普通发票|9%|6%|3%|13%"></div></td></tr>
  <tr><td class="lb">事项描述</td><td><input type="checkbox" class="row-check" data-row="0"><input id="field6920_0"></td>
      <td class="lb">金额</td><td><input id="field6932_0"></td></tr>
  <!-- 境内公司付款（workflow 56）明细：fieldmark 直用 -->
  <tr><td class="lb">报销场景</td><td><div class="ant-select" data-fieldmark="field6827_0" data-options="IT服务外包|IT硬件设备|公司通讯费|普通发票|相关票据"></div></td>
      <td class="lb">发票类型</td><td><div class="ant-select" data-fieldmark="field6826_0" data-options="普通发票|增值税普通发票 ｜ 3%|增值税普通发票 ｜ 6%|增值税普通发票 ｜ 9%|增值税专用发票|增值税专用发票 ｜ 3%|增值税专用发票 ｜ 6%|增值税专用发票 ｜ 9%|增值税专用发票 ｜ 13%|增值税电子普通发票（境内客运）|增值税电子普通发票（境内客运） ｜ 3%|增值税电子普通发票（境内客运） ｜ 9%|电子普通发票|9%|6%|3%|13%"></div></td></tr>
  <tr><td class="lb">事项描述</td><td><input type="checkbox" class="row-check" data-row="0"><input id="field6821_0"></td>
      <td class="lb">金额</td><td><input id="field6829_0"></td></tr>
  <tr class="detail_button_row"><td colspan="4" class="detail_1_0_15">
      <div class="detailButtonDiv">
        <i id="delbutton0" name="delbutton0" class="icon-coms-form-delete-hot detailBtn" title="删除">－</i>
        <i id="copybutton0" name="copybutton0" class="icon-coms-copy-hot detailBtn" title="复制">⧉</i>
      </div></td></tr>
</table>
<!-- 固定表头里的「禁用副本」：面板不许点这个 -->
<table id="oTable0_head" class="excelDetailTable"><tr><td class="detail_1_0_15">
  <div class="detailButtonDiv"><i id="delbutton0" class="icon-coms-form-delete-hot detailBtn detailBtnDisabled" title="删除">－</i></div>
</td></tr></table>
<a href="#" id="addrow">＋ 新增</a>
<button class="ant-btn">保 存</button>
<pre id="saved"></pre>
<script>
// 回归埋点：记录代填过程中的退格键与附件变化（面板要求「先清空再填」）
window.__sdTrace = { backspace: {}, fileChanges: 0, fileCleared: 0 };
window.__sdResetTrace = function () { window.__sdTrace = { backspace: {}, fileChanges: 0, fileCleared: 0 }; };
document.addEventListener('keydown', function (e) {
  if ((e.key || '') !== 'Backspace') return;
  var t = e.target || {};
  var name = t.id || t.className || t.tagName || '?';
  var m = window.__sdTrace.backspace;
  m[name] = (m[name] || 0) + 1;
}, true);
document.addEventListener('change', function (e) {
  var t = e.target || {};
  if (t.type !== 'file') return;
  window.__sdTrace.fileChanges++;
  if (!t.files || !t.files.length) window.__sdTrace.fileCleared++;
  renderUploadList(t);
}, true);

// 把选中的文件画成真实 OA 的上传列表；每个文件右侧一个「×」= 删除（会弹确认）
function renderUploadList(input) {
  var box = input.closest ? input.closest('.wea-upload') : null;
  if (!box) return;
  var list = box.querySelector('.wea-upload-list');
  if (!list) return;
  list.innerHTML = '';
  Array.prototype.forEach.call(input.files || [], function (f, idx) {
    var item = document.createElement('div');
    item.className = 'wea-upload-list-item';
    item.innerHTML = '<div class="wea-upload-list-item-file"><div class="wea-upload-list-content">'
      + '<a class="wea-field-link" title="' + f.name + '">' + f.name + '</a>'
      + '</div><div class="wea-upload-list-item-ops">'
      + '<i class="anticon anticon-cross" data-idx="' + idx + '" title="删除" style="cursor:pointer"></i>'
      + '</div></div>';
    list.appendChild(item);
  });
}
document.addEventListener('click', function (e) {
  var x = e.target && e.target.closest ? e.target.closest('.wea-upload-list-item-ops .anticon-cross') : null;
  if (!x) return;
  e.preventDefault();
  var name = (x.closest('.wea-upload-list-item').querySelector('.wea-field-link') || {}).textContent || '';
  showConfirm('确定要删除' + name + '吗？', function () { x.closest('.wea-upload-list-item').remove(); });
});
function setupSelect(root){
  var options = (root.getAttribute('data-options')||'').split('|').filter(Boolean);
  var prefill = root.getAttribute('data-prefill') || '';
  var selection = document.createElement('div'); selection.className='ant-select-selection';
  var rendered = document.createElement('div'); rendered.className='ant-select-selection__rendered';
  var hidden = document.createElement('input'); hidden.type='hidden';
  var search = document.createElement('input'); search.className='ant-select-search__field';
  // 预填（真实 OA 会给「费用承担部门」预填当前部门，如 AI+）
  var preset = root.getAttribute('data-prefill') || root.getAttribute('data-value') || '';
  if (preset) { hidden.value = preset; rendered.textContent = preset; }
  selection.appendChild(rendered); root.appendChild(selection); root.appendChild(hidden); root.appendChild(search);
  var dd=null;
  // 选中值渲染成 chip —— 真实 OA（费用承担部门）就是这样：值本身是可点链接，
  // chip 上还挂着内联的跳转处理器，删除图标也是链接。面板点它就会把页面带走。
  function render(){
    rendered.innerHTML = '';
    if (!hidden.value) { search.style.display=''; return; }
    search.style.display='none';
    var chip = document.createElement('span');
    chip.className = 'ant-select-selection__choice';
    chip.setAttribute('onclick', "window.__sdTrace.valueChipClicks=(window.__sdTrace.valueChipClicks||0)+1;"
      + "window.__sdTrace.chipInlineNav=(window.__sdTrace.chipInlineNav||0)+1;");
    var link = document.createElement('a');
    link.className = 'wea-field-link';
    link.setAttribute('href', '/dept-page');
    link.setAttribute('title', hidden.value);
    link.textContent = hidden.value;
    var rm = document.createElement('a');
    rm.className = 'ant-select-selection__choice__remove';
    rm.setAttribute('href', '/dept-page');
    rm.setAttribute('title', '删除');
    rm.textContent = '×';
    rm.addEventListener('click', function(e){
      // 删除处理器挂在图标自身（antd 同款）；外部跳转必须已被面板拦掉，否则这里记录
      if (e.defaultPrevented) window.__sdTrace.navPrevented = (window.__sdTrace.navPrevented||0)+1;
      else window.__sdTrace.navAllowed = (window.__sdTrace.navAllowed||0)+1;
      hidden.value=''; render();
    });
    chip.appendChild(link); chip.appendChild(rm); rendered.appendChild(chip);
  }
  function ensure(){
    if(dd) return dd;
    dd=document.createElement('div'); dd.className='ant-select-dropdown ant-select-dropdown-hidden';
    var ul=document.createElement('ul');
    options.forEach(function(o){
      var li=document.createElement('li'); li.className='ant-select-dropdown-menu-item'; li.textContent=o;
      li.addEventListener('click', function(){ hidden.value=o; render(); hide(); });
      ul.appendChild(li);
    });
    dd.appendChild(ul); document.body.appendChild(dd);
    return dd;
  }
  function hide(){ if(dd) dd.classList.add('ant-select-dropdown-hidden'); }
  function show(kw){
    ensure(); dd.classList.remove('ant-select-dropdown-hidden');
    var r=root.getBoundingClientRect();
    dd.style.left=(r.left)+'px'; dd.style.top=(r.bottom+2)+'px';
    Array.prototype.forEach.call(dd.querySelectorAll('li'), function(li){
      li.style.display = (!kw || li.textContent.indexOf(kw)>=0) ? '' : 'none';
    });
  }
  search.addEventListener('focus', function(){ show(search.value); });
  search.addEventListener('click', function(){ show(search.value); });
  search.addEventListener('input', function(){ show(search.value); });
  // 真实 OA：点选值区（含 chip 一带）也会展开下拉
  selection.addEventListener('click', function(){ show(search.value); });
  if (prefill) hidden.value = prefill;
  render();
}
// 值链接/删除图标被点到时的统一埋点（含「默认跳转是否被拦」）；# 锚点不算跳转
document.addEventListener('click', function (e) {
  var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
  if (!a) return;
  if (String(a.getAttribute('href') || '').charAt(0) === '#') return;
  window.__sdTrace.linkClicks = (window.__sdTrace.linkClicks||0)+1;
  if (e.defaultPrevented) window.__sdTrace.navPrevented = (window.__sdTrace.navPrevented||0)+1;
  else window.__sdTrace.navAllowed = (window.__sdTrace.navAllowed||0)+1;
}, false);
Array.prototype.forEach.call(document.querySelectorAll('.ant-select'), setupSelect);

var rowCount = 1;
// 删/加行后把明细行号重排成 0..n-1（真实 OA 的明细是按位置渲染的）
function renumberRows() {
  var seen = {};
  Array.prototype.forEach.call(document.querySelectorAll('.row-check'), function (a) { seen[a.getAttribute('data-row')] = 1; });
  var uniq = Object.keys(seen).map(Number).sort(function (x, y) { return x - y; });
  uniq.forEach(function (oldIdx, newIdx) {
    if (oldIdx === newIdx) return;
    ['field6926_', 'field6827_'].forEach(function (m) {
      var el = document.querySelector('[data-fieldmark="' + m + oldIdx + '"]');
      if (el) el.setAttribute('data-fieldmark', m + newIdx);
    });
    ['field6920_', 'field6932_', 'field6821_', 'field6829_'].forEach(function (m) {
      var el = document.getElementById(m + oldIdx);
      if (el) el.id = m + newIdx;
    });
    Array.prototype.forEach.call(document.querySelectorAll('.row-check[data-row="' + oldIdx + '"]'), function (a) {
      a.setAttribute('data-row', String(newIdx));
    });
  });
  rowCount = uniq.length;
  return uniq.length;
}
function showConfirm(text, onOk) {
  var wrap = document.createElement('div');
  wrap.className = 'ant-modal-wrap';
  wrap.innerHTML = '<div class="ant-modal ant-confirm ant-confirm-confirm"><div class="ant-modal-content">'
    + '<div class="ant-modal-body"><span class="ant-confirm-title">信息确认</span><div>' + text + '</div>'
    + '<div class="ant-confirm-btns"><button type="button" class="ant-btn ant-btn-primary"><span>确 定</span></button>'
    + '<button type="button" class="ant-btn ant-btn-ghost"><span>取 消</span></button></div></div></div></div>';
  document.body.appendChild(wrap);
  wrap.querySelector('.ant-btn-primary').addEventListener('click', function () { wrap.remove(); onOk(); });
  wrap.querySelector('.ant-btn-ghost').addEventListener('click', function () { wrap.remove(); });
}

// 明细「-」：真实 OA 要先勾中行、点完还要在弹窗里点「确 定」
document.addEventListener('click', function (e) {
  var btn = e.target && e.target.closest ? e.target.closest('#delbutton0, i.detailBtn[title="删除"]') : null;
  if (!btn) return;
  e.preventDefault();
  if (/disabled/i.test(String(btn.className || ''))) return;   // 固定表头的禁用副本
  var checked = Array.prototype.slice.call(document.querySelectorAll('.row-check')).filter(function (c) { return c.checked; });
  if (!checked.length) return;                                  // 没勾中就不删（真实行为）
  showConfirm('确定要删除吗？', function () {
    var seen = {};
    checked.forEach(function (c) {
      var i = c.getAttribute('data-row');
      if (seen[i]) return;
      seen[i] = 1;
      ['field6926_', 'field6827_'].forEach(function (m) {
        var el = document.querySelector('[data-fieldmark="' + m + i + '"]');
        var tr = el && el.closest('tr');
        if (tr) tr.remove();
      });
      ['field6920_', 'field6932_', 'field6821_', 'field6829_'].forEach(function (m) {
        var el = document.getElementById(m + i);
        var tr = el && el.closest('tr');
        if (tr) tr.remove();
      });
    });
    renumberRows();
  });
});

document.getElementById('addrow').addEventListener('click', function(e){
  e.preventDefault();
  renumberRows();
  var i = rowCount++;
  var table = document.querySelector('table');
  var tr1 = document.createElement('tr');
  tr1.innerHTML = '<td class="lb">报销场景</td><td><div class="ant-select" data-fieldmark="field6926_'+i+'" data-options="IT服务外包|交通费用|相关票据|境内住宿|境内机票|境内火车票"></div></td>'
    + '<td class="lb">发票类型</td><td><div class="ant-select" data-fieldmark="field6925_'+i+'" data-options="普通发票|增值税普通发票 ｜ 3%|增值税普通发票 ｜ 6%|增值税普通发票 ｜ 9%|增值税专用发票|增值税专用发票 ｜ 3%|增值税专用发票 ｜ 6%|增值税专用发票 ｜ 9%|增值税专用发票 ｜ 13%|增值税电子普通发票（境内客运）|增值税电子普通发票（境内客运） ｜ 3%|增值税电子普通发票（境内客运） ｜ 9%|电子普通发票|9%|6%|3%|13%"></div></td>';
  var tr2 = document.createElement('tr');
  tr2.innerHTML = '<td class="lb">事项描述</td><td><input type="checkbox" class="row-check" data-row="'+i+'"><input id="field6920_'+i+'"></td><td class="lb">金额</td><td><input id="field6932_'+i+'"></td>';
  var tr3 = document.createElement('tr');
  tr3.innerHTML = '<td class="lb">报销场景</td><td><div class="ant-select" data-fieldmark="field6827_'+i+'" data-options="IT服务外包|IT硬件设备|公司通讯费|普通发票"></div></td>'
    + '<td class="lb">发票类型</td><td><div class="ant-select" data-fieldmark="field6826_'+i+'" data-options="普通发票|增值税普通发票 ｜ 3%|增值税普通发票 ｜ 6%|增值税普通发票 ｜ 9%|增值税专用发票|增值税专用发票 ｜ 3%|增值税专用发票 ｜ 6%|增值税专用发票 ｜ 9%|增值税专用发票 ｜ 13%|增值税电子普通发票（境内客运）|增值税电子普通发票（境内客运） ｜ 3%|增值税电子普通发票（境内客运） ｜ 9%|电子普通发票|9%|6%|3%|13%"></div></td>';
  var tr4 = document.createElement('tr');
  tr4.innerHTML = '<td class="lb">事项描述</td><td><input type="checkbox" class="row-check" data-row="'+i+'"><input id="field6821_'+i+'"></td><td class="lb">金额</td><td><input id="field6829_'+i+'"></td>';
  table.appendChild(tr1); table.appendChild(tr2); table.appendChild(tr3); table.appendChild(tr4);
  Array.prototype.forEach.call(tr1.querySelectorAll('.ant-select'), setupSelect);
  Array.prototype.forEach.call(tr3.querySelectorAll('.ant-select'), setupSelect);
});
document.querySelector('button.ant-btn').addEventListener('click', function(){
  document.getElementById('saved').textContent = '已点击保存（mock）';
});
</script>
</body></html>"""


@app.get("/oa")
def oa_outer() -> HTMLResponse:
    return HTMLResponse(_OUTER)


@app.get("/oa-form")
def oa_form() -> HTMLResponse:
    return HTMLResponse(_FORM)


if __name__ == "__main__":
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")