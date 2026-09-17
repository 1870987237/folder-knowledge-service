'use strict';
const $ = s => document.querySelector(s);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
let token = '', page = 'overview', snapshot = null, events = [], timer = null, refreshing = false, refreshQueued = false;
let filters = {key_id: '', library_id: '', status: '', start_time: '', end_time: ''};
let eventCursor = '', nextEventCursor = null;
const pages = {overview:'服务概览',libraries:'知识库',keys:'Agent 与密钥',activity:'访问活动'};
const badge = (text, kind='') => `<span class="badge ${kind}">${escapeHTML(text)}</span>`;
const availabilityBadge = row => badge(row.availability?.message || '状态待检查', row.availability?.status === 'available' ? 'good' : 'bad');
const empty = (title, description='') => `<div class="empty"><strong>${escapeHTML(title)}</strong>${description?`<p>${escapeHTML(description)}</p>`:''}</div>`;
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
$('#navigation').addEventListener('click',event=>{const target=event.target.closest('[data-page]');if(target){page=target.dataset.page;render();refresh().catch(report);}});
$('#modal-close').addEventListener('click',()=>$('#modal').close());
$('#modal').addEventListener('close',()=>$('#modal-body').replaceChildren());
function modal(title, html){$('#modal-title').textContent=title;$('#modal-body').innerHTML=html;if(!$('#modal').open)$('#modal').showModal();}
async function refresh(){
  if(refreshing){refreshQueued=true;return;}refreshing=true;
  try{const currentToken=token;const [next,activity]=await Promise.all([api('/overview'),api('/activity?'+new URLSearchParams(Object.fromEntries(Object.entries({limit:100,...filters,before_id:page==='activity'?eventCursor:''}).filter(([,value])=>value!==''))))]);
    if(!token || currentToken!==token)return;snapshot=next;events=activity.events;nextEventCursor=activity.next_before_id;$('#banner').classList.add('hidden');render();
  }finally{refreshing=false;if(refreshQueued){refreshQueued=false;if(token)refresh().catch(report);}}
}
function render(){
  if(!snapshot)return;
  $('#page-title').textContent=pages[page];
  document.querySelectorAll('[data-page]').forEach(x=>x.classList.toggle('active',x.dataset.page===page));
  const action=$('#page-action');action.classList.toggle('hidden',!['libraries','keys'].includes(page));action.textContent=page==='libraries'?'+ 添加知识库':'+ 生成 Agent Key';action.onclick=()=>page==='libraries'?libraryForm():keyForm();
  $('#view').innerHTML=page==='overview'?overview():page==='libraries'?libraryTable():page==='keys'?keysTable():activityView();
  $('#filter-key')?.addEventListener('change',event=>{filters.key_id=event.target.value;eventCursor='';refresh().catch(report);});
  $('#filter-status')?.addEventListener('change',event=>{filters.status=event.target.value;eventCursor='';refresh().catch(report);});
  for(const field of ['start_time','end_time'])$('#filter-'+field)?.addEventListener('change',event=>{filters[field]=event.target.value?new Date(event.target.value).toISOString():'';eventCursor='';refresh().catch(report);});
  $('#older-events')?.addEventListener('click',()=>{eventCursor=nextEventCursor;refresh().catch(report);});
  $('#latest-events')?.addEventListener('click',()=>{eventCursor='';refresh().catch(report);});
  $('#filter-library')?.addEventListener('change',event=>{filters.library_id=event.target.value;eventCursor='';refresh().catch(report);});
}
function overview(){
  const enabled=snapshot.libraries.filter(x=>x.enabled).length,valid=snapshot.keys.filter(x=>keyState(x)[0]==='有效').length;
  const unavailable=snapshot.libraries.filter(x=>x.enabled && x.availability?.status!=='available').length;
  const recent=snapshot.keys.filter(x=>x.activity.last_seen && Date.parse(snapshot.server_time)-Date.parse(x.activity.last_seen)<300000).length;
  return `<div class="stats"><div class="stat"><div class="stat-label">已启用知识库</div><div class="stat-value">${enabled}</div><div class="stat-note">共 ${snapshot.libraries.length} 个目录映射${unavailable ? ` · ${badge(unavailable+' 个不可用','bad')}` : ''}</div></div><div class="stat"><div class="stat-label">有效 Agent Key</div><div class="stat-value">${valid}</div><div class="stat-note">近 5 分钟 ${recent} 个 Key 有访问</div></div><div class="stat"><div class="stat-label">累计工具调用</div><div class="stat-value">${snapshot.metrics.calls.toLocaleString()}</div><div class="stat-note">${snapshot.metrics.errors} 次失败 · 已认证的知识操作</div></div><div class="stat"><div class="stat-label">正在处理</div><div class="stat-value">${snapshot.active_requests.length}</div><div class="stat-note">当前服务进程中的请求</div></div></div>
  <div class="two-col"><div class="panel"><div class="panel-head"><h3>开始使用</h3>${badge('只读访问','good')}</div><div class="panel-body">${[['注册知识目录','选择挂载范围内的目录，赋予稳定的知识库 ID。'],['分配 Agent Key','每个 Agent 单独发放，按知识库授权并设置有效期。'],['搜索、读取与追踪','Agent 通过 MCP 或 REST 获取文件，访问记录自动留存。']].map((x,i)=>`<div class="guide-step"><span class="step">${i+1}</span><div><b>${x[0]}</b><p>${x[1]}</p></div></div>`).join('')}</div></div><div class="panel"><div class="panel-head"><h3>服务接入</h3><small>Bearer Key 认证</small></div><div class="panel-body"><div class="endpoint"><small>MCP · Streamable HTTP</small><code>${escapeHTML(location.origin)}/mcp</code></div><div class="endpoint"><small>REST · 知识库列表</small><code>${escapeHTML(location.origin)}/api/v1/libraries</code></div><div class="endpoint"><small>读取文件 · 指定知识库与相对路径</small><code>/api/v1/read?library_id=team-guide&amp;path=README.md</code></div></div></div></div>
  <div class="panel"><div class="panel-head"><h3>最近访问</h3><small>每 10 秒刷新</small></div>${eventTable(events.filter(x=>x.kind==='access').slice(0,6))}</div>`;
}
function libraryTable(){return `<div class="panel">${snapshot.libraries.length?`<div class="table-wrap"><table><thead><tr><th>知识库</th><th>目录 / 挂载根目录下</th><th>启用状态</th><th>目录可用性</th><th>授权 Key</th><th>操作</th></tr></thead><tbody>${snapshot.libraries.map(row=>`<tr><td><strong>${escapeHTML(row.name)}</strong><small class="mono">${escapeHTML(row.id)}</small></td><td class="mono">${escapeHTML(row.path)}</td><td>${badge(row.enabled?'已启用':'已停用',row.enabled?'good':'')}</td><td>${availabilityBadge(row)}</td><td>${snapshot.keys.filter(k=>k.library_ids.includes(row.id)&&keyState(k)[0]==='有效').length}</td><td><div class="actions"><button class="text-button" data-action="browse" data-id="${row.id}">浏览</button><button class="text-button" data-action="edit-library" data-id="${row.id}">编辑</button><button class="text-button" data-action="toggle-library" data-id="${row.id}">${row.enabled?'停用':'启用'}</button></div></td></tr>`).join('')}</tbody></table></div>`:empty('还没有知识库','添加一个挂载目录中的文件夹，然后为 Agent 分配访问权限。')}</div>`;}
function keysTable(){return `<div class="panel">${snapshot.keys.length?`<div class="table-wrap"><table><thead><tr><th>Agent / Key ID</th><th>可访问知识库</th><th>状态</th><th>最近访问</th><th>调用 / 失败</th><th>操作</th></tr></thead><tbody>${snapshot.keys.map(key=>`<tr><td><strong>${escapeHTML(key.agent_name)}</strong><small class="mono">${escapeHTML(key.id)}</small></td><td>${key.library_ids.map(id=>badge(libraryName(id))).join(' ') || badge('未授权知识库','warn')}</td><td>${badge(...keyState(key))}<small>${key.expires_at?'到期：'+escapeHTML(date(key.expires_at)):'无到期时间'}</small></td><td>${escapeHTML(date(key.activity.last_seen))}</td><td>${key.activity.calls||0} / ${key.activity.errors||0}</td><td>${key.revoked_at?'—':`<div class="actions"><button class="text-button" data-action="edit-key-grants" data-id="${key.id}">编辑授权</button><button class="danger" data-action="revoke" data-id="${key.id}">撤销</button></div>`}</td></tr>`).join('')}</tbody></table></div>`:empty('尚未发放 Agent Key','先注册知识库，再生成仅能读取指定知识库的独立密钥。')}</div>`;}
function eventTable(rows){return rows.length?`<div class="table-wrap"><table><thead><tr><th>时间</th><th>Agent / 操作方</th><th>操作</th><th>知识库 / 路径</th><th>结果</th><th>耗时</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${escapeHTML(date(row.timestamp))}</td><td>${escapeHTML(row.agent_name || '管理员')}<small>${escapeHTML(row.transport || '管理操作')}</small></td><td class="mono">${escapeHTML(row.tool || row.action)}</td><td>${escapeHTML(libraryName(row.library_id))}<small class="mono">${escapeHTML(row.path||'')}</small></td><td>${badge(row.status==='ok'?'成功':row.status==='partial'?'结果不完整':'失败',row.status==='ok'?'good':row.status==='partial'?'warn':'bad')}<small>${escapeHTML(row.reason ? row.error+' · '+row.reason : row.error||'')}</small></td><td>${row.duration_ms!=null?escapeHTML(row.duration_ms)+' ms':'—'}</td></tr>`).join('')}</tbody></table></div>`:empty('暂无访问记录','Agent 调用知识工具后，访问记录会出现在这里。');}
function localInput(value){if(!value)return '';const d=new Date(value);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}
function activityView(){return `<div class="filters"><select id="filter-key" aria-label="筛选 Agent"><option value="">全部 Agent 与管理操作</option>${snapshot.keys.map(k=>`<option value="${k.id}" ${filters.key_id===k.id?'selected':''}>${escapeHTML(k.agent_name)} · ${k.id.slice(0,6)}</option>`).join('')}</select><select id="filter-library" aria-label="筛选知识库"><option value="">全部知识库</option>${snapshot.libraries.map(k=>`<option value="${k.id}" ${filters.library_id===k.id?'selected':''}>${escapeHTML(k.name)}</option>`).join('')}</select><select id="filter-status" aria-label="筛选结果"><option value="">全部结果</option>${[['ok','成功'],['error','失败'],['partial','结果不完整']].map(([value,label])=>`<option value="${value}" ${filters.status===value?'selected':''}>${label}</option>`).join('')}</select><label>开始时间<input id="filter-start_time" aria-label="开始时间" type="datetime-local" value="${localInput(filters.start_time)}"></label><label>结束时间<input id="filter-end_time" aria-label="结束时间" type="datetime-local" value="${localInput(filters.end_time)}"></label></div><div class="panel"><div class="panel-head"><h3>正在处理的请求</h3>${badge(String(snapshot.active_requests.length))}</div>${snapshot.active_requests.length?snapshot.active_requests.map(x=>`<div class="live-row">${escapeHTML(x.agent_name)} · ${escapeHTML(x.tool)}<span>${escapeHTML(libraryName(x.library_id))}</span></div>`).join(''):empty('当前没有正在处理的请求')}</div><div class="panel"><div class="panel-head"><h3>访问与管理记录</h3></div>${eventTable(events)}</div><div class="actions">${eventCursor?'<button id="latest-events" class="secondary">返回最新</button>':''}${nextEventCursor?'<button id="older-events" class="secondary">更早记录</button>':''}</div>`;}
$('#view').addEventListener('click',async event=>{
  const button=event.target.closest('[data-action]');if(!button)return;
  const id=button.dataset.id,row=snapshot.libraries.find(x=>x.id===id);
  try{
    if(button.dataset.action==='edit-key-grants')keyGrantsForm(snapshot.keys.find(x=>x.id===id));
    if(button.dataset.action==='edit-library')libraryForm(row);
    if(button.dataset.action==='browse')await browse(id);
    if(button.dataset.action==='toggle-library'){await api('/libraries/'+id,{method:'PUT',body:JSON.stringify({id:row.id,name:row.name,path:row.path,enabled:!row.enabled})});await refresh();toast(row.enabled?'知识库已停用':'知识库已启用');}
    if(button.dataset.action==='revoke' && confirm('撤销后，这个 Key 的后续请求将被拒绝。确认撤销？')){await api('/keys/'+id+'/revoke',{method:'POST'});await refresh();toast('Key 已撤销');}
  }catch(error){report(error);}
});
function libraryForm(row=null){
  modal(row?'编辑知识库':'添加知识库',`<form id="library-form"><div class="field"><label>知识库名称</label><input name="name" maxlength="80" required value="${escapeHTML(row?.name||'')}" placeholder="例如：团队知识库"></div><div class="field"><label>知识库 ID</label><input name="id" maxlength="64" pattern="[a-z0-9][a-z0-9_-]{0,63}" required ${row?'readonly':''} value="${escapeHTML(row?.id||'')}" placeholder="team-guide"><p class="hint">供 Agent 指定 library_id，创建后不可修改。</p></div><div class="field"><label for="library-path">知识文件夹</label><div class="form-row"><input id="library-path" name="path" required value="${escapeHTML(row?.path||'')}" placeholder="选择文件夹或填写相对路径"><button type="button" id="choose-dir" class="secondary">选择目录</button></div><p class="hint">只能注册已挂载范围内的目录；“.” 表示整个挂载根目录。</p></div><label class="check"><input name="enabled" type="checkbox" ${!row||row.enabled?'checked':''}>启用知识库</label>${row?'<p class="hint">更换目录会影响已授权给该知识库的全部 Key。</p>':''}<p id="form-error" class="error"></p><div class="modal-actions"><button class="primary" type="submit">保存知识库</button></div></form>`);
  $('#choose-dir').onclick=()=>directoryBrowser();
  $('#library-form').onsubmit=async event=>{event.preventDefault();const data=new FormData(event.target);const button=event.submitter;button.disabled=true;
    try{await api('/libraries'+(row?'/'+row.id:''),{method:row?'PUT':'POST',body:JSON.stringify({id:data.get('id'),name:data.get('name'),path:data.get('path'),enabled:data.has('enabled')})});$('#modal').close();await refresh();toast('知识库配置已生效');}catch(error){$('#form-error').textContent=error.message;}finally{button.disabled=false;}};
}
function directoryBrowser(){
  const form=$('#library-form'),pathInput=form.querySelector('[name=path]'),title=$('#modal-title').textContent;
  const picker=document.createElement('section');picker.className='folder-picker';picker.setAttribute('aria-label','选择知识文件夹');
  const folderIcon='<svg class="folder-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9H3V7Z" fill="currentColor" fill-opacity=".14" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  picker.innerHTML=`<div class="folder-toolbar"><button type="button" class="secondary compact" data-up aria-label="返回上一级">↑ 上一级</button><nav class="folder-crumbs" aria-label="当前目录"></nav></div><input type="search" class="folder-filter" placeholder="筛选已加载的文件夹" aria-label="筛选文件夹"><div class="folder-location"></div><div class="folder-list" aria-label="文件夹列表"></div><button type="button" class="folder-more secondary compact hidden">加载更多</button><p class="folder-error error hidden" role="alert"></p><div class="folder-footer"><div class="folder-selection" aria-live="polite"><span>已选文件夹</span><strong></strong></div><div class="folder-actions"><button type="button" class="secondary" data-cancel>取消</button><button type="button" class="primary" data-confirm>使用此文件夹</button></div></div>`;
  form.classList.add('hidden');form.after(picker);$('#modal-title').textContent='选择文件夹';
  const find=s=>picker.querySelector(s),display=p=>p==='.'?'挂载根目录':'挂载根目录 / '+p;
  let current='.',selected=null,entries=[],nextOffset=null,busy=false,loaded=false;
  function finish(apply=false){
    if(apply && selected)pathInput.value=selected;
    picker.remove();form.classList.remove('hidden');$('#modal-title').textContent=title;
    if(apply)pathInput.focus();else $('#choose-dir').focus();
  }
  function selection(){
    find('.folder-selection strong').textContent=selected?display(selected):'尚未选择';
    find('[data-confirm]').disabled=busy||!selected;
    picker.querySelectorAll('[data-select]').forEach(button=>{const active=button.dataset.select===selected;button.setAttribute('aria-pressed',String(active));button.closest('.folder-row')?.classList.toggle('selected',active);});
  }
  function render(){
    const parts=current==='.'?[]:current.split('/');
    find('.folder-crumbs').innerHTML=[{path:'.',name:'挂载根目录'},...parts.map((name,i)=>({path:parts.slice(0,i+1).join('/'),name}))].map((part,i)=>`${i?'<span aria-hidden="true">/</span>':''}<button type="button" data-open="${escapeHTML(part.path)}" ${part.path===current?'aria-current="location"':''}>${escapeHTML(part.name)}</button>`).join('');
    const query=find('.folder-filter').value.trim().toLocaleLowerCase();
    const filtered=entries.filter(x=>x.path.split('/').pop().toLocaleLowerCase().includes(query));
    find('.folder-location').innerHTML=`<button type="button" data-select="${escapeHTML(current)}" class="folder-current">${folderIcon}<span>选择当前文件夹</span></button>`;
    find('.folder-list').innerHTML=filtered.map(x=>{const name=x.path.split('/').pop();return `<div class="folder-row"><button type="button" class="folder-select" data-select="${escapeHTML(x.path)}" aria-label="选择 ${escapeHTML(name)}">${folderIcon}<span>${escapeHTML(name)}</span><span class="folder-check" aria-hidden="true">✓</span></button><button type="button" class="folder-open" data-open="${escapeHTML(x.path)}" aria-label="打开 ${escapeHTML(name)}">打开 <span aria-hidden="true">›</span></button></div>`;}).join('')||`<div class="folder-empty">${query?'没有匹配的文件夹':nextOffset!=null?'本页没有文件夹，可继续加载':'此目录下没有子文件夹'}</div>`;
    find('.folder-more').classList.toggle('hidden',nextOffset==null);
    setBusy(busy);selection();
  }
  function setBusy(value){
    busy=value;picker.setAttribute('aria-busy',String(value));
    picker.querySelectorAll('button:not([data-cancel])').forEach(x=>x.disabled=value);
    find('.folder-filter').disabled=value;
    find('[data-up]').disabled=value||current==='.';
    find('[data-confirm]').disabled=value||!selected;
    if(!loaded)find('[data-select]').disabled=true;
  }
  async function navigate(path,append=false){
    if(busy)return;setBusy(true);find('.folder-error').classList.add('hidden');
    if(!loaded)find('.folder-list').innerHTML='<div class="folder-empty" role="status">正在加载文件夹…</div>';
    try{
      const data=await api('/directories?'+new URLSearchParams({path,offset:append?nextOffset:0}));
      if(!picker.isConnected)return;
      if(!append){current=data.path||path;entries=[];selected=current;find('.folder-filter').value='';}
      const seen=new Set(entries.map(x=>x.path));entries.push(...data.entries.filter(x=>!seen.has(x.path)));
      nextOffset=data.next_offset;loaded=true;render();
      if(data.partial){find('.folder-error').textContent='目录列表未完整加载，可进入子目录或直接填写路径。';find('.folder-error').classList.remove('hidden');}
    }catch(error){if(picker.isConnected){find('.folder-error').textContent='无法打开文件夹：'+error.message;find('.folder-error').classList.remove('hidden');if(!loaded)find('.folder-list').innerHTML='<div class="folder-empty">请点击“挂载根目录”重新选择</div>';}}
    finally{if(picker.isConnected)setBusy(false);}
  }
  picker.addEventListener('click',event=>{
    const button=event.target.closest('button');if(!button)return;
    if(button.hasAttribute('data-cancel'))return finish();
    if(busy)return;
    if(button.hasAttribute('data-confirm'))return finish(true);
    if(button.hasAttribute('data-up'))return navigate(current.split('/').slice(0,-1).join('/')||'.');
    if(button.hasAttribute('data-open'))return navigate(button.dataset.open);
    if(button.hasAttribute('data-select')){selected=button.dataset.select;selection();return;}
    if(button.classList.contains('folder-more'))navigate(current,true);
  });
  picker.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();finish();}});
  find('.folder-filter').oninput=render;
  render();find('[data-cancel]').focus();navigate(pathInput.value.trim()||'.');
}
function keyGrantsForm(key){
  modal('编辑 Key 授权',`<form id="key-grants-form"><p>Agent：<strong>${escapeHTML(key.agent_name)}</strong></p><div class="field"><label>可访问知识库</label><div class="checks">${snapshot.libraries.map(x=>`<label class="check"><input type="checkbox" name="libraries" value="${x.id}" ${key.library_ids.includes(x.id)?'checked':''}>${escapeHTML(x.name)}${!x.enabled?'（已停用）':''} ${availabilityBadge(x)}</label>`).join('')}</div></div><p class="hint">保存后对后续请求生效，Key 不变。不勾选任何知识库可暂时清空访问权限。已停用或目录异常的知识库恢复后，授权仍会生效。</p><p id="form-error" class="error"></p><div class="modal-actions"><button type="submit" class="primary">保存授权</button></div></form>`);
  $('#key-grants-form').onsubmit=async event=>{event.preventDefault();const data=new FormData(event.target),button=event.submitter;button.disabled=true;
    try{await api('/keys/'+key.id+'/grants',{method:'PUT',body:JSON.stringify({library_ids:data.getAll('libraries')})});$('#modal').close();await refresh();toast('授权已更新，原 Key 可继续使用');}catch(error){$('#form-error').textContent=error.message;}finally{button.disabled=false;}};
}
function keyForm(){
  if(!snapshot.libraries.length){toast('请先添加知识库');return;}
  modal('生成 Agent Key',`<form id="key-form"><div class="field"><label>Agent 名称</label><input name="agent_name" required maxlength="80" placeholder="例如：知识问答 Agent"></div><div class="field"><label>授权知识库</label><div class="checks">${snapshot.libraries.map(x=>`<label class="check"><input type="checkbox" name="libraries" value="${x.id}">${escapeHTML(x.name)}${!x.enabled?'（已停用）':''} ${availabilityBadge(x)}</label>`).join('')}</div></div><div class="field"><label>到期时间（可选）</label><input type="datetime-local" name="expiry"><p class="hint">按当前浏览器时区输入，留空表示不自动过期。</p></div><p id="form-error" class="error"></p><div class="modal-actions"><button type="submit" class="primary">生成密钥</button></div></form>`);
  $('#key-form').onsubmit=async event=>{event.preventDefault();const data=new FormData(event.target),button=event.submitter;button.disabled=true;
    try{const ids=data.getAll('libraries');if(!ids.length)throw new Error('至少选择一个知识库。');const result=await api('/keys',{method:'POST',body:JSON.stringify({agent_name:data.get('agent_name'),library_ids:ids,expires_at:data.get('expiry')?new Date(data.get('expiry')).toISOString():null})});
      modal('Key 已生成',`<p class="secret-warning">完整密钥仅在这里展示一次。关闭后无法再次查看，请保存到 Agent 的密钥配置中。</p><code id="new-token" class="secret"></code><p class="hint">Agent：${escapeHTML(result.key.agent_name)}<br>MCP：${escapeHTML(location.origin)}/mcp<br>请求头：Authorization: Bearer &lt;Key&gt;</p><div class="modal-actions"><button id="copy-key" class="primary">复制密钥</button></div>`);$('#new-token').textContent=result.token;$('#copy-key').onclick=async()=>{try{await navigator.clipboard.writeText($('#new-token').textContent);toast('已复制');}catch{toast('浏览器不支持复制，请选中密钥手动保存');}};await refresh();
    }catch(error){if($('#form-error'))$('#form-error').textContent=error.message;else report(error);}finally{button.disabled=false;}};
}
async function browse(id,path='.',read=false,startLine=1,offset=0,cursor=''){
  const data=await api('/browse/'+id+'?'+new URLSearchParams({path,read,offset,start_line:startLine,...(cursor?{cursor}:{})}));
  modal('浏览 · '+libraryName(id),`<div class="form-row"><span class="mono">${escapeHTML(path)}</span><button id="browse-up" class="secondary compact">上一级</button></div>${read?'<pre id="file-preview" class="preview"></pre>':`<div class="directory-list">${data.entries.map(x=>`<button class="directory-item" data-browse-path="${escapeHTML(x.path)}" data-read="${x.type==='file'}">${x.type==='directory'?'▸':'▤'} ${escapeHTML(x.path)}</button>`).join('')||'<p class="hint">目录为空</p>'}</div>`}<p class="hint">${read?'文件内容只读；版本：'+escapeHTML(data.version):'仅展示支持的文本格式。'}${data.partial?' 本次列表不完整。':''}</p>${(read?data.next_cursor:data.next_offset)!=null?'<button id="browse-next" class="secondary">下一页</button>':''}`);
  $('#browse-up').onclick=()=>browse(id,path.split('/').slice(0,-1).join('/')||'.').catch(report);
  if(read)$('#file-preview').textContent=data.lines.map(x=>`${x.line}${x.column>1?':'+x.column:''}  ${x.text}${x.line_complete===false?' [本行待续]':''}`).join('\n');
  document.querySelectorAll('[data-browse-path]').forEach(x=>x.onclick=()=>browse(id,x.dataset.browsePath,x.dataset.read==='true').catch(report));
  if($('#browse-next'))$('#browse-next').onclick=()=>browse(id,path,read,1,read?0:data.next_offset,read?data.next_cursor:'').catch(report);
}
