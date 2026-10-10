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
  ul{margin:0;padding:0}
  #addrow{display:inline-block;margin:10px 0;color:#3f6fd8}
  button.ant-btn{margin-left:12px;padding:5px 16px;border:1px solid #e3e7f1;border-radius:8px;background:#18181a;color:#fff}
  #saved{color:#0f6b4f;margin-top:10px}
</style></head>
<body>
<table>
  <tr><td class="lb">报销类型</td><td><div class="ant-select" data-fieldmark="field6970" data-options="差旅费用|公务限额费用"></div></td>
      <td class="lb">公司名称</td><td><div class="ant-select" data-fieldmark="field6969" data-options="上海复星旅游管理有限公司|珠海市横琴复星旅文旅游发展有限公司"></div></td></tr>
  <tr><td class="lb">是否含超标</td><td><div class="ant-select" data-fieldmark="field7014" data-options="是|否"></div></td>
      <td class="lb">是否差旅/业务招待报销</td><td><div class="ant-select" data-fieldmark="field6974" data-options="是|否"></div></td></tr>
  <tr><td class="lb">报销事由</td><td colspan="3"><input id="field6946"></td></tr>
  <tr><td class="lb">实际出差天数</td><td><input id="tripdays"></td><td></td><td></td></tr>
  <tr><td class="lb">相关流程</td><td colspan="3"><div class="ant-select" data-options="出差申请-0918 上海行|出差申请-0925 北京行|日常费用报销"></div></td></tr>
  <tr><td class="lb">是否全电子发票</td><td><div class="ant-select" data-fieldmark="field29328" data-options="是|否"></div></td>
      <td class="lb">相关票据</td><td><div data-fieldmark="field6951"><input type="file" multiple></div></td></tr>
  <tr><td class="lb">报销场景</td><td><div class="ant-select" data-fieldmark="field6926_0" data-options="境内住宿|境内机票|境内火车票"></div></td>
      <td class="lb">发票类型</td><td><div class="ant-select" data-fieldmark="field6925_0" data-options="9%|6%|3%|13%"></div></td></tr>
  <tr><td class="lb">事项描述</td><td><input id="field6920_0"></td>
      <td class="lb">金额</td><td><input id="field6932_0"></td></tr>
</table>
<a href="#" id="addrow">＋ 新增</a>
<button class="ant-btn">保 存</button>
<pre id="saved"></pre>
<script>
function setupSelect(root){
  var options = (root.getAttribute('data-options')||'').split('|').filter(Boolean);
  var selection = document.createElement('div'); selection.className='ant-select-selection';
  var rendered = document.createElement('div'); rendered.className='ant-select-selection__rendered';
  var hidden = document.createElement('input'); hidden.type='hidden';
  var search = document.createElement('input'); search.className='ant-select-search__field';
  selection.appendChild(rendered); root.appendChild(selection); root.appendChild(hidden); root.appendChild(search);
  var dd=null;
  function ensure(){
    if(dd) return dd;
    dd=document.createElement('div'); dd.className='ant-select-dropdown ant-select-dropdown-hidden';
    var ul=document.createElement('ul');
    options.forEach(function(o){
      var li=document.createElement('li'); li.className='ant-select-dropdown-menu-item'; li.textContent=o;
      li.addEventListener('click', function(){ hidden.value=o; rendered.textContent=o; hide(); });
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
}
Array.prototype.forEach.call(document.querySelectorAll('.ant-select'), setupSelect);

var rowCount = 1;
document.getElementById('addrow').addEventListener('click', function(e){
  e.preventDefault();
  var i = rowCount++;
  var table = document.querySelector('table');
  var tr1 = document.createElement('tr');
  tr1.innerHTML = '<td class="lb">报销场景</td><td><div class="ant-select" data-fieldmark="field6926_'+i+'" data-options="境内住宿|境内机票|境内火车票"></div></td>'
    + '<td class="lb">发票类型</td><td><div class="ant-select" data-fieldmark="field6925_'+i+'" data-options="9%|6%|3%|13%"></div></td>';
  var tr2 = document.createElement('tr');
  tr2.innerHTML = '<td class="lb">事项描述</td><td><input id="field6920_'+i+'"></td><td class="lb">金额</td><td><input id="field6932_'+i+'"></td>';
  table.appendChild(tr1); table.appendChild(tr2);
  Array.prototype.forEach.call(tr1.querySelectorAll('.ant-select'), setupSelect);
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