'use strict';
const $ = s => document.querySelector(s);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
let token = '', page = 'overview', snapshot = null, events = [], timer = null, refreshing = false;
let filters = {key_id: '', library_id: ''};
const pages = {overview:['服务概览','了解知识库、Agent 访问与服务使用情况。'],libraries:['知识库','在已挂载的目录中注册知识库，变更无需重启服务。'],keys:['Agent 与密钥','为每个 Agent 分配独立密钥和知识库访问范围。'],activity:['访问活动','查看最近调用与当前请求，不将无状态访问标记为持续在线。']};
const badge = (text, kind='') => `<span class="badge ${kind}">${escapeHTML(text)}</span>`;
const empty = (title, description) => `<div class="empty"><strong>${escapeHTML(title)}</strong><p>${escapeHTML(description)}</p></div>`;
const date = value => value ? new Date(value).toLocaleString('zh-CN',{hour12:false}) : '尚无记录';
const libraryName = id => snapshot?.libraries.find(x => x.id === id)?.name || id || '—';
function keyState(key) {return key.revoked_at ? ['已撤销','bad'] : key.expires_at && Date.parse(key.expires_at) <= Date.now() ? ['已过期','warn'] : ['有效','good'];}
function toast(message) {$('#toast').textContent=message;$('#toast').classList.remove('hidden');setTimeout(()=>$('#toast').classList.add('hidden'),3500);}
function report(error) {$('#banner').textContent=error.message;$('#banner').classList.remove('hidden');}
async function api(path, options={}) {
  const response = await fetch('/admin/api'+path,{...options,headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json',...options.headers},cache:'no-store'});
  const data = await response.json();
  if (!response.ok) {
    if(response.status===401 && !$('#app').classList.contains('hidden')) logout();
    throw new Error(data.message || (response.status===401 ? '管理员令牌无效，请重新输入。' : JSON.stringify(data.detail || data.error || '请求失败')));
  }
  return data;
}
function logout(){token='';snapshot=null;events=[];clearInterval(timer);timer=null;$('#modal').close();$('#modal-body').replaceChildren();$('#view').replaceChildren();$('#app').classList.add('hidden');$('#login').classList.remove('hidden');$('#admin-token').value='';}
$('#logout').addEventListener('click',logout);
$('#login-form').addEventListener('submit',async event=>{
  event.preventDefault();token=$('#admin-token').value.trim();$('#login-error').textContent='';
  const button=event.submitter;button.disabled=true;
  try {await refresh();$('#admin-token').value='';$('#login').classList.add('hidden');$('#app').classList.remove('hidden');timer=setInterval(()=>{if(!document.hidden)refresh().catch(report);},10000);}
  catch(error){token='';$('#login-error').textContent=error.message;}finally{button.disabled=false;}
});
$('#refresh').addEventListener('click',()=>refresh().catch(report));
$('#navigation').addEventListener('click',event=>{const target=event.target.closest('[data-page]');if(target){page=target.dataset.page;render();refresh().catch(report);}});
$('#modal-close').addEventListener('click',()=>$('#modal').close());
$('#modal').addEventListener('close',()=>$('#modal-body').replaceChildren());
function modal(title, html){$('#modal-title').textContent=title;$('#modal-body').innerHTML=html;if(!$('#modal').open)$('#modal').showModal();}
async function refresh(){
  if(refreshing)return;refreshing=true;
  try{const currentToken=token;const [next,activity]=await Promise.all([api('/overview'),api('/activity?'+new URLSearchParams({limit:100,...filters}))]);
    if(!token || currentToken!==token)return;snapshot=next;events=activity.events;$('#updated').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});$('#banner').classList.add('hidden');render();
  }finally{refreshing=false;}
}
function render(){
  if(!snapshot)return;
  $('#breadcrumb').textContent=pages[page][0];$('#page-title').textContent=pages[page][0];$('#page-description').textContent=pages[page][1];
  document.querySelectorAll('[data-page]').forEach(x=>x.classList.toggle('active',x.dataset.page===page));
  const action=$('#page-action');action.classList.toggle('hidden',!['libraries','keys'].includes(page));action.textContent=page==='libraries'?'+ 添加知识库':'+ 生成 Agent Key';action.onclick=()=>page==='libraries'?libraryForm():keyForm();
  $('#view').innerHTML=page==='overview'?overview():page==='libraries'?libraryTable():page==='keys'?keysTable():activityView();
  $('#filter-key')?.addEventListener('change',event=>{filters.key_id=event.target.value;refresh().catch(report);});
  $('#filter-library')?.addEventListener('change',event=>{filters.library_id=event.target.value;refresh().catch(report);});
}
function overview(){
  const enabled=snapshot.libraries.filter(x=>x.enabled).length,valid=snapshot.keys.filter(x=>keyState(x)[0]==='有效').length;
  const recent=snapshot.keys.filter(x=>x.activity.last_seen && Date.parse(snapshot.server_time)-Date.parse(x.activity.last_seen)<300000).length;
  return `<div class="stats"><div class="stat"><div class="stat-label">已启用知识库</div><div class="stat-value">${enabled}</div><div class="stat-note">共 ${snapshot.libraries.length} 个目录映射</div></div><div class="stat"><div class="stat-label">有效 Agent Key</div><div class="stat-value">${valid}</div><div class="stat-note">近 5 分钟 ${recent} 个 Key 有访问</div></div><div class="stat"><div class="stat-label">累计工具调用</div><div class="stat-value">${snapshot.metrics.calls.toLocaleString()}</div><div class="stat-note">${snapshot.metrics.errors} 次失败 · 已认证的知识操作</div></div><div class="stat"><div class="stat-label">正在处理</div><div class="stat-value">${snapshot.active_requests.length}</div><div class="stat-note">当前服务进程中的请求</div></div></div>
  <div class="two-col"><div class="panel"><div class="panel-head"><h3>开始使用</h3>${badge('只读访问','good')}</div><div class="panel-body">${[['注册知识目录','选择挂载范围内的目录，赋予稳定的知识库 ID。'],['分配 Agent Key','每个 Agent 单独发放，按知识库授权并设置有效期。'],['搜索、读取与追踪','Agent 通过 MCP 或 REST 获取文件，访问记录自动留存。']].map((x,i)=>`<div class="guide-step"><span class="step">${i+1}</span><div><b>${x[0]}</b><p>${x[1]}</p></div></div>`).join('')}</div></div><div class="panel"><div class="panel-head"><h3>服务接入</h3><small>Bearer Key 认证</small></div><div class="panel-body"><div class="endpoint"><small>MCP · Streamable HTTP</small><code>${escapeHTML(location.origin)}/mcp</code></div><div class="endpoint"><small>REST · 知识库列表</small><code>${escapeHTML(location.origin)}/api/v1/libraries</code></div><div class="endpoint"><small>读取文件 · 指定知识库与相对路径</small><code>/api/v1/read?library_id=team-guide&amp;path=README.md</code></div></div></div></div>
  <div class="panel"><div class="panel-head"><h3>最近访问</h3><small>每 10 秒刷新</small></div>${eventTable(events.filter(x=>x.kind==='access').slice(0,6))}</div>`;
}
function libraryTable(){return `<div class="panel">${snapshot.libraries.length?`<div class="table-wrap"><table><thead><tr><th>知识库</th><th>目录 / 挂载根目录下</th><th>状态</th><th>授权 Key</th><th>操作</th></tr></thead><tbody>${snapshot.libraries.map(row=>`<tr><td><strong>${escapeHTML(row.name)}</strong><small class="mono">${escapeHTML(row.id)}</small></td><td class="mono">${escapeHTML(row.path)}</td><td>${badge(row.enabled?'已启用':'已停用',row.enabled?'good':'')}</td><td>${snapshot.keys.filter(k=>k.library_ids.includes(row.id)&&keyState(k)[0]==='有效').length}</td><td><div class="actions"><button class="text-button" data-action="browse" data-id="${row.id}">浏览</button><button class="text-button" data-action="edit-library" data-id="${row.id}">编辑</button><button class="text-button" data-action="toggle-library" data-id="${row.id}">${row.enabled?'停用':'启用'}</button></div></td></tr>`).join('')}</tbody></table></div>`:empty('还没有知识库','添加一个挂载目录中的文件夹，然后为 Agent 分配访问权限。')}</div><p class="hint">修改目录映射后，该知识库已有的授权 Key 将访问新目录。停用只会阻止后续访问，不删除任何知识文件。</p>`;}
function keysTable(){return `<div class="panel">${snapshot.keys.length?`<div class="table-wrap"><table><thead><tr><th>Agent / Key ID</th><th>可访问知识库</th><th>状态</th><th>最近访问</th><th>调用 / 失败</th><th>操作</th></tr></thead><tbody>${snapshot.keys.map(key=>`<tr><td><strong>${escapeHTML(key.agent_name)}</strong><small class="mono">${escapeHTML(key.id)}</small></td><td>${key.library_ids.map(id=>badge(libraryName(id))).join(' ')}</td><td>${badge(...keyState(key))}<small>${key.expires_at?'到期：'+escapeHTML(date(key.expires_at)):'无到期时间'}</small></td><td>${escapeHTML(date(key.activity.last_seen))}</td><td>${key.activity.calls||0} / ${key.activity.errors||0}</td><td>${key.revoked_at?'—':`<button class="danger" data-action="revoke" data-id="${key.id}">撤销</button>`}</td></tr>`).join('')}</tbody></table></div>`:empty('尚未发放 Agent Key','先注册知识库，再生成仅能读取指定知识库的独立密钥。')}</div><p class="hint">Agent 名称是管理员登记的标签。多个客户端共用同一 Key 时，访问记录将合并；“最近访问”不代表持续在线。</p>`;}
function eventTable(rows){return rows.length?`<div class="table-wrap"><table><thead><tr><th>时间</th><th>Agent / 操作方</th><th>操作</th><th>知识库 / 路径</th><th>结果</th><th>耗时</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${escapeHTML(date(row.timestamp))}</td><td>${escapeHTML(row.agent_name || '管理员')}<small>${escapeHTML(row.transport || '管理操作')}</small></td><td class="mono">${escapeHTML(row.tool || row.action)}</td><td>${escapeHTML(libraryName(row.library_id))}<small class="mono">${escapeHTML(row.path||'')}</small></td><td>${badge(row.status==='ok'?'成功':row.status==='partial'?'结果不完整':'失败',row.status==='ok'?'good':row.status==='partial'?'warn':'bad')}<small>${escapeHTML(row.error||'')}</small></td><td>${row.duration_ms!=null?escapeHTML(row.duration_ms)+' ms':'—'}</td></tr>`).join('')}</tbody></table></div>`:empty('暂无访问记录','Agent 调用知识工具后，访问记录会出现在这里。');}
function activityView(){return `<div class="filters"><select id="filter-key" aria-label="筛选 Agent"><option value="">全部 Agent 与管理操作</option>${snapshot.keys.map(k=>`<option value="${k.id}" ${filters.key_id===k.id?'selected':''}>${escapeHTML(k.agent_name)} · ${k.id.slice(0,6)}</option>`).join('')}</select><select id="filter-library" aria-label="筛选知识库"><option value="">全部知识库</option>${snapshot.libraries.map(k=>`<option value="${k.id}" ${filters.library_id===k.id?'selected':''}>${escapeHTML(k.name)}</option>`).join('')}</select></div><div class="panel"><div class="panel-head"><h3>正在处理的请求</h3>${badge(String(snapshot.active_requests.length))}</div>${snapshot.active_requests.length?snapshot.active_requests.map(x=>`<div class="live-row">${escapeHTML(x.agent_name)} · ${escapeHTML(x.tool)}<span>${escapeHTML(libraryName(x.library_id))}</span></div>`).join(''):empty('当前没有正在处理的请求','此处展示正在执行的知识操作，而非长连接状态。')}</div><div class="panel"><div class="panel-head"><h3>最近记录</h3><small>最多 100 条 · 最近 7 个日志文件的尾部记录</small></div>${eventTable(events)}</div><p class="hint">仅保存操作、路径、结果与耗时，不记录密钥、搜索内容或文档正文。完整历史按日期保存在服务数据目录中。</p>`;}
$('#view').addEventListener('click',async event=>{
  const button=event.target.closest('[data-action]');if(!button)return;
  const id=button.dataset.id,row=snapshot.libraries.find(x=>x.id===id);
  try{
    if(button.dataset.action==='edit-library')libraryForm(row);
    if(button.dataset.action==='browse')await browse(id);
    if(button.dataset.action==='toggle-library'){await api('/libraries/'+id,{method:'PUT',body:JSON.stringify({id:row.id,name:row.name,path:row.path,enabled:!row.enabled})});await refresh();toast(row.enabled?'知识库已停用':'知识库已启用');}
    if(button.dataset.action==='revoke' && confirm('撤销后，这个 Key 的后续请求将被拒绝。确认撤销？')){await api('/keys/'+id+'/revoke',{method:'POST'});await refresh();toast('Key 已撤销');}
  }catch(error){report(error);}
});
function libraryForm(row=null){
  modal(row?'编辑知识库':'添加知识库',`<form id="library-form"><div class="field"><label>知识库名称</label><input name="name" maxlength="80" required value="${escapeHTML(row?.name||'')}" placeholder="例如：团队知识库"></div><div class="field"><label>知识库 ID</label><input name="id" maxlength="64" pattern="[a-z0-9][a-z0-9_-]{0,63}" required ${row?'readonly':''} value="${escapeHTML(row?.id||'')}" placeholder="team-guide"><p class="hint">供 Agent 指定 library_id，创建后不可修改。</p></div><div class="field"><label>挂载根目录下的相对路径</label><div class="form-row"><input name="path" required value="${escapeHTML(row?.path||'')}" placeholder="team-guide"><button type="button" id="choose-dir" class="secondary">选择目录</button></div><div id="directory-browser"></div><p class="hint">只能注册已挂载范围内的目录；“.” 表示整个挂载根目录。</p></div><label class="check"><input name="enabled" type="checkbox" ${!row||row.enabled?'checked':''}>启用知识库</label>${row?'<p class="hint">更换目录会影响已授权给该知识库的全部 Key。</p>':''}<p id="form-error" class="error"></p><div class="modal-actions"><button class="primary" type="submit">保存知识库</button></div></form>`);
  $('#choose-dir').onclick=()=>directoryBrowser('.').catch(error=>$('#form-error').textContent=error.message);
  $('#library-form').onsubmit=async event=>{event.preventDefault();const data=new FormData(event.target);const button=event.submitter;button.disabled=true;
    try{await api('/libraries'+(row?'/'+row.id:''),{method:row?'PUT':'POST',body:JSON.stringify({id:data.get('id'),name:data.get('name'),path:data.get('path'),enabled:data.has('enabled')})});$('#modal').close();await refresh();toast('知识库配置已生效');}catch(error){$('#form-error').textContent=error.message;}finally{button.disabled=false;}};
}
async function directoryBrowser(path,offset=0){
  const data=await api('/directories?'+new URLSearchParams({path,offset}));const parent=path.split('/').slice(0,-1).join('/')||'.';
  $('#directory-browser').innerHTML=`<div class="directory-list"><button type="button" class="directory-item" id="select-directory">✓ 选择当前目录：${escapeHTML(path)}</button>${path!=='.'?`<button type="button" class="directory-item" data-directory="${escapeHTML(parent)}">↰ 上一级</button>`:''}${data.entries.map(x=>`<button type="button" class="directory-item" data-directory="${escapeHTML(x.path)}">▸ ${escapeHTML(x.path)}</button>`).join('')}${data.next_offset!=null?'<button type="button" class="directory-item" id="more-directories">下一页目录</button>':''}</div>${data.partial?'<p class="hint">目录列表不完整，请缩小路径范围。</p>':''}`;
  $('#select-directory').onclick=()=>{$('#library-form input[name=path]').value=path;$('#directory-browser').replaceChildren();};
  document.querySelectorAll('[data-directory]').forEach(x=>x.onclick=()=>directoryBrowser(x.dataset.directory).catch(error=>$('#form-error').textContent=error.message));
  if($('#more-directories'))$('#more-directories').onclick=()=>directoryBrowser(path,data.next_offset).catch(error=>$('#form-error').textContent=error.message);
}
function keyForm(){
  if(!snapshot.libraries.length){toast('请先添加知识库');return;}
  modal('生成 Agent Key',`<form id="key-form"><div class="field"><label>Agent 名称</label><input name="agent_name" required maxlength="80" placeholder="例如：知识问答 Agent"></div><div class="field"><label>授权知识库</label><div class="checks">${snapshot.libraries.map(x=>`<label class="check"><input type="checkbox" name="libraries" value="${x.id}">${escapeHTML(x.name)}${!x.enabled?'（已停用）':''}</label>`).join('')}</div></div><div class="field"><label>到期时间（可选）</label><input type="datetime-local" name="expiry"><p class="hint">按当前浏览器时区输入，留空表示不自动过期。</p></div><p id="form-error" class="error"></p><div class="modal-actions"><button type="submit" class="primary">生成密钥</button></div></form>`);
  $('#key-form').onsubmit=async event=>{event.preventDefault();const data=new FormData(event.target),button=event.submitter;button.disabled=true;
    try{const ids=data.getAll('libraries');if(!ids.length)throw new Error('至少选择一个知识库。');const result=await api('/keys',{method:'POST',body:JSON.stringify({agent_name:data.get('agent_name'),library_ids:ids,expires_at:data.get('expiry')?new Date(data.get('expiry')).toISOString():null})});
      modal('Key 已生成',`<p class="secret-warning">完整密钥仅在这里展示一次。关闭后无法再次查看，请保存到 Agent 的密钥配置中。</p><code id="new-token" class="secret"></code><p class="hint">Agent：${escapeHTML(result.key.agent_name)}<br>MCP：${escapeHTML(location.origin)}/mcp<br>请求头：Authorization: Bearer &lt;Key&gt;</p><div class="modal-actions"><button id="copy-key" class="primary">复制密钥</button></div>`);$('#new-token').textContent=result.token;$('#copy-key').onclick=async()=>{try{await navigator.clipboard.writeText($('#new-token').textContent);toast('已复制');}catch{toast('浏览器不支持复制，请选中密钥手动保存');}};await refresh();
    }catch(error){if($('#form-error'))$('#form-error').textContent=error.message;else report(error);}finally{button.disabled=false;}};
}
async function browse(id,path='.',read=false,startLine=1,offset=0){
  const data=await api('/browse/'+id+'?'+new URLSearchParams({path,read,offset,start_line:startLine}));
  modal('浏览 · '+libraryName(id),`<div class="form-row"><span class="mono">${escapeHTML(path)}</span><button id="browse-up" class="secondary compact">上一级</button></div>${read?'<pre id="file-preview" class="preview"></pre>':`<div class="directory-list">${data.entries.map(x=>`<button class="directory-item" data-browse-path="${escapeHTML(x.path)}" data-read="${x.type==='file'}">${x.type==='directory'?'▸':'▤'} ${escapeHTML(x.path)}</button>`).join('')||'<p class="hint">目录为空</p>'}</div>`}<p class="hint">${read?'文件内容只读；SHA-256：'+escapeHTML(data.sha256):'仅展示支持的文本格式。'}${data.partial?' 本次列表不完整。':''}</p>${(read?data.next_start_line:data.next_offset)!=null?'<button id="browse-next" class="secondary">下一页</button>':''}`);
  $('#browse-up').onclick=()=>browse(id,path.split('/').slice(0,-1).join('/')||'.').catch(report);
  if(read)$('#file-preview').textContent=data.lines.map(x=>`${x.line}  ${x.text}`).join('\n');
  document.querySelectorAll('[data-browse-path]').forEach(x=>x.onclick=()=>browse(id,x.dataset.browsePath,x.dataset.read==='true').catch(report));
  if($('#browse-next'))$('#browse-next').onclick=()=>browse(id,path,read,read?data.next_start_line:1,read?0:data.next_offset).catch(report);
}
