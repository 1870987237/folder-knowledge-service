'use strict';

let templatePage = {templates:[],next_offset:null}, templateOffset = 0;
let inboxPage = {items:[],next_after:null}, inboxAfter = 0;
let inboxFilters = {library_id:'',status:''};
const inboxClaims = new Map();
const inboxLabels = {pending:'待处理',processing:'处理中',completed:'已处理'};
const kindLabels = {new_content:'新增内容',correction:'纠错',observation:'观察记录'};
const outcomeLabels = {accepted:'采纳',partially_accepted:'部分采纳',rejected:'不采纳'};
const jsonText = value => escapeHTML(JSON.stringify(value,null,2));
const templateURL = definition => `/templates/${encodeURIComponent(definition.id)}/versions/${encodeURIComponent(definition.version)}`;

function resetKnowledgeAdmin(){
  inboxClaims.clear();templatePage={templates:[],next_offset:null};templateOffset=0;
  inboxPage={items:[],next_after:null};inboxAfter=0;inboxFilters={library_id:'',status:''};
}
async function loadKnowledgeAdmin(currentToken){
  const selected=page;
  if(selected==='templates'){
    const offset=templateOffset,data=await api('/templates?'+new URLSearchParams({offset,limit:50}));
    if(token===currentToken && offset===templateOffset)templatePage=data;
  }
  if(selected==='inbox'){
    const query=new URLSearchParams(Object.entries({...inboxFilters,after:inboxAfter,limit:50}).filter(([,v])=>v!==''));
    const signature=query.toString(),data=await api('/inbox?'+query);
    const current=new URLSearchParams(Object.entries({...inboxFilters,after:inboxAfter,limit:50}).filter(([,v])=>v!=='')).toString();
    if(token===currentToken && signature===current)inboxPage=data;
  }
}
function renderKnowledgeAdmin(){
  if(!['templates','inbox'].includes(page))return false;
  const action=$('#page-action');action.classList.remove('hidden');
  action.textContent=page==='templates'?'+ 注册模板':'刷新';
  action.onclick=()=>page==='templates'?registerTemplateForm():refresh().catch(report);
  $('#view').innerHTML=page==='templates'?templatesView():inboxView();
  for(const field of ['library_id','status'])$('#inbox-'+field)?.addEventListener('change',event=>{
    inboxFilters[field]=event.target.value;inboxAfter=0;inboxPage={items:[],next_after:null};refresh().catch(report);
  });
  return true;
}
function templatesView(){
  return `<div class="panel">${templatePage.templates.length?`<div class="table-wrap"><table><thead><tr><th>模板 / 版本</th><th>开发者</th><th>能力</th><th>安装状态</th><th>操作</th></tr></thead><tbody>${templatePage.templates.map(row=>{
    const d=row.definition;
    return `<tr><td><strong>${escapeHTML(d.name)}</strong><small class="mono">${escapeHTML(d.id)} · ${escapeHTML(d.version)}</small></td><td>${escapeHTML(d.maintainer)}</td><td>${d.capabilities.map(c=>badge(c==='update'?'增量更新':'初始化')).join(' ')}</td><td>${badge(row.status==='installed'?'已安装':'待上传',row.status==='installed'?'good':'warn')}</td><td><div class="actions"><button class="text-button" data-kb-action="template-detail" data-id="${escapeHTML(d.id)}" data-version="${escapeHTML(d.version)}">详情</button>${row.status==='installed'?'':`<button class="text-button" data-kb-action="install" data-id="${escapeHTML(d.id)}" data-version="${escapeHTML(d.version)}">上传程序包</button>`}</div></td></tr>`;
  }).join('')}</tbody></table></div>`:empty('暂无模板','注册开发者提供的 registration.json，再上传对应的 ZIP 程序包。')}</div><div class="actions">${templateOffset?'<button class="secondary" data-kb-action="templates-first">首页</button>':''}${templatePage.next_offset!==null?'<button class="secondary" data-kb-action="templates-next">下一页</button>':''}</div>`;
}
function inboxView(){
  return `<div class="filters"><select id="inbox-library_id" aria-label="筛选知识库"><option value="">全部知识库</option>${snapshot.libraries.map(l=>`<option value="${l.id}" ${inboxFilters.library_id===l.id?'selected':''}>${escapeHTML(l.name)}</option>`).join('')}</select><select id="inbox-status" aria-label="筛选处理状态"><option value="">全部状态</option>${Object.entries(inboxLabels).map(([v,n])=>`<option value="${v}" ${inboxFilters.status===v?'selected':''}>${n}</option>`).join('')}</select></div><div class="panel">${inboxPage.items.length?`<div class="table-wrap"><table><thead><tr><th>提交时间</th><th>知识库</th><th>材料</th><th>提交者</th><th>状态</th><th>操作</th></tr></thead><tbody>${inboxPage.items.map(r=>`<tr><td>${escapeHTML(date(r.created_at))}</td><td>${escapeHTML(libraryName(r.library_id))}</td><td class="wrap-cell"><strong>${escapeHTML(r.title||r.submission_id)}</strong><small>${escapeHTML(kindLabels[r.kind]||'')} · ${escapeHTML(r.submission_id)}</small></td><td>${escapeHTML(snapshot.keys.find(k=>k.id===r.submitted_by.id)?.agent_name||r.submitted_by.id)}</td><td>${badge(inboxLabels[r.status],r.status==='completed'?'good':r.status==='processing'?'warn':'')}${r.status==='processing'&&r.lease_until*1000<=Date.now()?'<small>租约已过期，可重新领取</small>':''}</td><td><button class="text-button" data-kb-action="inbox-detail" data-id="${r.id}">查看 / 处理</button></td></tr>`).join('')}</tbody></table></div>`:empty('暂无材料')}</div><div class="actions">${inboxAfter?'<button class="secondary" data-kb-action="inbox-first">首页</button>':''}${inboxPage.next_after!==null?'<button class="secondary" data-kb-action="inbox-next">下一页</button>':''}</div>`;
}

// Form errors stay beside the input; polling only refreshes the page behind the dialog.
function bindAdminForm(id,submit){
  const form=$(id);
  form.onsubmit=async event=>{
    event.preventDefault();const button=event.submitter;button.disabled=true;
    const error=form.querySelector('[role=alert]');error.textContent='';
    try{await submit(form);}catch(e){error.textContent=e.message;}finally{button.disabled=false;}
  };
}
function parseObject(text){
  let value;try{value=JSON.parse(text);}catch{throw new Error('请输入有效的 JSON。');}
  if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('JSON 顶层必须是对象。');
  return value;
}
function registerTemplateForm(){
  modal('注册模板',`<form id="template-register"><div class="field"><label for="registration-file">导入 registration.json</label><input id="registration-file" type="file" accept=".json,application/json"></div><div class="field"><label for="registration-json">模板元数据</label><textarea id="registration-json" class="code-input" rows="14" required placeholder="粘贴开发者提供的 registration.json"></textarea></div><p class="hint">模板版本固定；变更元数据请注册新版本。</p><p class="error" role="alert"></p><div class="modal-actions"><button class="primary">注册</button></div></form>`);
  $('#registration-file').onchange=async event=>{
    const file=event.target.files[0],form=$('#template-register');if(!file)return;
    try{if(file.size>65536)throw new Error('元数据文件不能超过 64 KiB。');const value=parseObject(await file.text());if(form.isConnected)form.querySelector('textarea').value=JSON.stringify(value,null,2);}
    catch(e){form.querySelector('[role=alert]').textContent=e.message;}
  };
  bindAdminForm('#template-register',async form=>{
    const result=await api('/templates',{method:'POST',body:JSON.stringify(parseObject(form.querySelector('textarea').value))});
    if(!token)return;
    installTemplateForm(result);await refresh();toast(result.duplicate?'该版本已注册':'模板已注册，请上传程序包');
  });
}
function installTemplateForm(row){
  const d=row.definition;
  if(row.status==='installed'){templateDetail(row);return;}
  modal('上传程序包 · '+d.name,`<form id="template-install"><p><code>${escapeHTML(d.id)} · ${escapeHTML(d.version)}</code></p><div class="field"><label for="package-file">package.zip（最多 32 MiB）</label><input id="package-file" type="file" accept=".zip,application/zip" required></div><p class="hint">安装会校验注册摘要和包结构，不执行程序。</p><p class="error" role="alert"></p><div class="modal-actions"><button class="primary">上传并安装</button></div></form>`);
  bindAdminForm('#template-install',async form=>{
    const file=form.querySelector('input').files[0];
    if(!file||file.size>32*1024*1024)throw new Error('请选择不超过 32 MiB 的 ZIP 文件。');
    const result=await api(templateURL(d)+'/package',{method:'PUT',headers:{'Content-Type':'application/zip'},body:file});
    if(!token)return;templateDetail(result);await refresh();toast('程序包已安装');
  });
}
function templateDetail(row){
  const d=row.definition;
  modal('模板详情 · '+d.name,`<div class="detail-grid"><span>版本</span><code>${escapeHTML(d.id)} · ${escapeHTML(d.version)}</code><span>状态</span><div>${badge(row.status==='installed'?'已安装':'待上传',row.status==='installed'?'good':'warn')}</div><span>开发者</span><span>${escapeHTML(d.maintainer)}</span><span>说明</span><span>${escapeHTML(d.description||'—')}</span></div><p class="hint">当前支持安装和绑定；更新程序调度与自动发布尚未开放。</p><details class="metadata-details"><summary>完整定义及校验摘要</summary><pre class="preview">${jsonText(row)}</pre></details>`);
}
async function installedTemplates(){
  let offset=0,rows=[];
  do{const data=await api('/templates?'+new URLSearchParams({offset,limit:100}));rows.push(...data.templates);offset=data.next_offset;}while(offset!==null);
  return rows.filter(r=>r.status==='installed'&&r.definition.capabilities.includes('update'));
}
async function templateBindingForm(library){
  const [current,templates]=await Promise.all([api(`/libraries/${library.id}/template`),installedTemplates()]);
  if(!token)return;
  const b=current.binding,w=b?.workspace;
  const currentIndex=templates.findIndex(r=>r.definition.id===b?.template_id&&r.definition.version===b?.template_version);
  modal('模板配置 · '+library.name,`<form id="template-binding"><div class="field"><label for="binding-template">模板版本</label><select id="binding-template" required><option value="">选择已安装的模板</option>${templates.map((r,i)=>`<option value="${i}" ${i===currentIndex?'selected':''} ${r.definition.required_secrets.length?'disabled':''}>${escapeHTML(r.definition.name)} · ${escapeHTML(r.definition.version)}${r.definition.required_secrets.length?'（暂不支持凭据注入）':''}</option>`).join('')}</select>${templates.length?'':'<p class="hint">请先在模板页注册并安装支持增量更新的程序包。</p>'}</div><div class="scope-summary"><span>Agent 检索目录</span><code>${escapeHTML(library.path)}</code></div><div class="field"><label for="workspace-mode">更新程序工作范围</label><select id="workspace-mode"><option value="default">仅当前检索目录</option><option value="custom">指定完整工作目录</option></select></div><div id="workspace-fields" class="hidden">${library.repository?`<div class="field"><label>Git 仓库</label><p>${escapeHTML(repositoryState.repositories.find(r=>r.id===library.repository.repository_id)?.name||library.repository.repository_id)}</p></div><div class="field"><label for="workspace-root">仓库内工作目录</label><input id="workspace-root" value="${escapeHTML(w?.workspace_subpath||'.')}"></div>`:`<div class="field"><label for="workspace-root">工作根目录（相对挂载根）</label><div class="form-row"><input id="workspace-root" value="${escapeHTML(w?.root_path||library.path)}"><button type="button" id="choose-dir" class="secondary">选择目录</button></div></div>`}<div class="field"><label for="knowledge-subpath">检索目录（相对工作根）</label><input id="knowledge-subpath" value="${escapeHTML(w?.knowledge_subpath||'.')}"></div><div class="field"><label for="workspace-excludes">额外排除路径（每行一项，相对工作根）</label><textarea id="workspace-excludes" rows="3">${escapeHTML((w?.exclude_paths||[]).join('\n'))}</textarea></div><p class="hint">工作根包含检索目录及辅助文件；二者组合必须与当前挂载匹配。.git、.env、私钥等默认排除。</p></div><div class="field"><label for="template-config">实例配置（JSON）</label><textarea id="template-config" class="code-input" rows="5">${jsonText(b?.config||{})}</textarea><details class="metadata-details"><summary>查看配置规范</summary><pre id="config-schema" class="preview"></pre></details></div><p class="hint">保存后仅更新绑定配置，不启动程序或发布知识。</p><p class="error" role="alert"></p><div class="modal-actions">${b?'<button type="button" id="template-unbind" class="danger">解除绑定</button>':''}<button class="primary" ${templates.length?'':'disabled'}>保存绑定</button></div></form>`);
  $('#workspace-mode').value=w?'custom':'default';
  function syncScope(){
    const custom=$('#workspace-mode').value==='custom';$('#workspace-fields').classList.toggle('hidden',!custom);
    for(const id of ['workspace-root','knowledge-subpath'])$('#'+id).required=custom;
  }
  $('#workspace-mode').onchange=syncScope;syncScope();
  function showSchema(){const selected=$('#binding-template').value;$('#config-schema').textContent=selected===''?'选择模板后显示':JSON.stringify(templates[Number(selected)].definition.config_schema,null,2);}
  $('#binding-template').onchange=showSchema;showSchema();
  if($('#choose-dir'))$('#choose-dir').onclick=()=>directoryBrowser($('#template-binding'),$('#workspace-root'));
  bindAdminForm('#template-binding',async form=>{
    const d=templates[Number($('#binding-template').value)]?.definition;if(!d)throw new Error('请选择模板。');
    const body={template_id:d.id,template_version:d.version,config:parseObject($('#template-config').value)};
    if($('#workspace-mode').value==='custom'){
      if(d.protocol_version!=='1.1')throw new Error('指定完整工作目录需要协议 1.1 的模板。');
      body.workspace={...(library.repository?{type:'git',repository_id:library.repository.repository_id,workspace_subpath:$('#workspace-root').value.trim()}:{type:'local',root_path:$('#workspace-root').value.trim()}),knowledge_subpath:$('#knowledge-subpath').value.trim(),exclude_paths:$('#workspace-excludes').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean)};
    }
    await api(`/libraries/${library.id}/template`,{method:'PUT',body:JSON.stringify(body)});$('#modal').close();toast('模板绑定已保存');await refresh();
  });
  if($('#template-unbind'))$('#template-unbind').onclick=async event=>{
    const button=event.target;button.disabled=true;
    try{await api(`/libraries/${library.id}/template`,{method:'DELETE'});$('#modal').close();toast('已解除绑定');await refresh();}catch(e){$('#template-binding [role=alert]').textContent=e.message;}finally{button.disabled=false;}
  };
}
async function submissionGrantsForm(key){
  const grants=await api(`/keys/${key.id}/inbox-grants`);if(!token)return;
  modal('提交授权 · '+key.agent_name,`<form id="submission-grants"><div class="checks">${snapshot.libraries.filter(l=>key.library_ids.includes(l.id)).map(l=>`<label class="check"><input type="checkbox" name="libraries" value="${l.id}" ${grants.submit_library_ids.includes(l.id)?'checked':''}>${escapeHTML(l.name)}${l.enabled?'':'（已停用）'}</label>`).join('')||'<p>请先为此 Key 分配知识库访问权限。</p>'}</div><p class="hint">允许向所选知识库的收件箱提交材料，不授予正式文件写入权限。</p><p class="error" role="alert"></p><div class="modal-actions"><button class="primary">保存提交授权</button></div></form>`);
  bindAdminForm('#submission-grants',async form=>{await api(`/keys/${key.id}/inbox-grants`,{method:'PUT',body:JSON.stringify({library_ids:new FormData(form).getAll('libraries')})});$('#modal').close();toast('提交授权已保存');});
}
async function inboxDetail(id){
  const row=await api('/inbox/'+id);if(!token)return;
  renderInboxDetail(row);
}
function renderInboxDetail(row){
  const m=row.material,active=row.status==='processing'&&row.lease_until*1000>Date.now();
  if(!active)inboxClaims.delete(row.id);
  const own=active&&inboxClaims.has(row.id),claimable=row.status!=='completed'&&!active;
  modal(m.title||'材料详情',`<div class="detail-grid"><span>知识库</span><span>${escapeHTML(libraryName(row.library_id))}</span><span>类型 / 状态</span><div>${badge(kindLabels[m.kind])} ${badge(inboxLabels[row.status])}</div><span>提交时间</span><span>${escapeHTML(date(row.created_at))}</span><span>提交编号</span><code>${escapeHTML(row.submission_id)}</code></div><pre class="material-text">${escapeHTML(m.content.text)}</pre><details class="metadata-details"><summary>来源与场景</summary><pre class="preview">${jsonText({source:m.source,context:m.context,submitted_by:row.submitted_by})}</pre></details>${row.result?`<section class="result-box"><h3>处理结果 · ${escapeHTML(outcomeLabels[row.result.outcome])}</h3><p class="material-text">${escapeHTML(row.result.summary)}</p>${row.result.references?.length?`<p>引用：${row.result.references.map(escapeHTML).join('、')}</p>`:''}${row.result.version?`<p>版本：<code>${escapeHTML(row.result.version)}</code></p>`:''}</section>`:''}${active?`<p class="hint">${own?'本页面已领取（刷新页面会丢失处理凭证）':'材料已被领取，本页面没有处理凭证'} · 租约到期 ${escapeHTML(date(row.lease_until*1000))}</p>`:''}${own?`<form id="inbox-complete"><div class="field"><label for="inbox-outcome">处理结论</label><select id="inbox-outcome">${Object.entries(outcomeLabels).map(([v,n])=>`<option value="${v}">${n}</option>`).join('')}</select></div><div class="field"><label for="inbox-summary">处理说明</label><textarea id="inbox-summary" rows="4" maxlength="4000" required></textarea></div><div class="field"><label for="inbox-references">引用路径（可选，每行一项）</label><textarea id="inbox-references" rows="2"></textarea></div><div class="field"><label for="inbox-version">知识版本（可选）</label><input id="inbox-version" maxlength="200"></div><p class="hint">回填结果仅记录处理结论，不会修改或发布知识文件。</p><p class="error" role="alert"></p><div class="modal-actions"><button type="button" id="inbox-form-reload" class="secondary">刷新状态</button><button type="button" id="inbox-release" class="secondary">放回待处理</button><button class="primary">保存处理结果</button></div></form>`:`<p id="inbox-detail-error" class="error" role="alert"></p><div class="modal-actions"><button id="inbox-reload" class="secondary">刷新状态</button>${claimable?'<button id="inbox-claim" class="primary">领取处理（15 分钟）</button>':''}</div>`}`);
  if($('#inbox-reload'))$('#inbox-reload').onclick=()=>inboxDetail(row.id).catch(e=>{$('#inbox-detail-error').textContent=e.message;});
  if($('#inbox-claim'))$('#inbox-claim').onclick=async event=>{
    const button=event.target;button.disabled=true;
    try{const currentToken=token,result=await api(`/inbox/${row.id}/claim`,{method:'POST',body:JSON.stringify({lease_seconds:900})});if(!token||token!==currentToken)return;inboxClaims.set(row.id,result.claim_token);renderInboxDetail(result);await refresh();}
    catch(e){if($('#inbox-detail-error'))$('#inbox-detail-error').textContent=e.message;else report(e);}finally{button.disabled=false;}
  };
  if(own){
    $('#inbox-form-reload').onclick=()=>inboxDetail(row.id).catch(e=>{if($('#inbox-complete'))$('#inbox-complete [role=alert]').textContent=e.message;});
    bindAdminForm('#inbox-complete',async()=>{
      const body={claim_token:inboxClaims.get(row.id),outcome:$('#inbox-outcome').value,summary:$('#inbox-summary').value,references:$('#inbox-references').value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean),version:$('#inbox-version').value||null};
      const result=await api(`/inbox/${row.id}/complete`,{method:'POST',body:JSON.stringify(body)});inboxClaims.delete(row.id);if(!token)return;renderInboxDetail(result);await refresh();toast('处理结果已保存');
    });
    $('#inbox-release').onclick=async event=>{
      const button=event.target;button.disabled=true;
      try{const result=await api(`/inbox/${row.id}/release`,{method:'POST',body:JSON.stringify({claim_token:inboxClaims.get(row.id)})});inboxClaims.delete(row.id);if(!token)return;renderInboxDetail(result);await refresh();}
      catch(e){if($('#inbox-complete'))$('#inbox-complete [role=alert]').textContent=e.message;else report(e);}finally{button.disabled=false;}
    };
  }
}
$('#view').addEventListener('click',async event=>{
  const button=event.target.closest('[data-kb-action]');if(!button)return;
  const action=button.dataset.kbAction,id=button.dataset.id;button.disabled=true;
  try{
    if(action==='binding')await templateBindingForm(snapshot.libraries.find(l=>l.id===id));
    if(action==='submit-grants')await submissionGrantsForm(snapshot.keys.find(k=>k.id===id));
    if(action==='inbox-detail')await inboxDetail(id);
    if(action==='install'||action==='template-detail'){
      const row=await api(templateURL({id,version:button.dataset.version}));if(token)(action==='install'?installTemplateForm:templateDetail)(row);
    }
    if(action==='templates-first'||action==='templates-next'){templateOffset=action.endsWith('first')?0:templatePage.next_offset;await refresh();}
    if(action==='inbox-first'||action==='inbox-next'){inboxAfter=action.endsWith('first')?0:inboxPage.next_after;await refresh();}
  }catch(e){report(e);}finally{button.disabled=false;}
});

function adminErrorMessage(data){
  const messages={
    workspace_view_mismatch:'工作根与检索子目录组合后，必须与当前知识库挂载目录一致。',
    workspace_binding_conflict:'同一工作范围的知识库必须使用相同模板、配置和排除规则。',
    workspace_repository_mismatch:'请选择当前知识库关联的 Git 仓库。',
    workspace_unavailable:'工作目录不可用，请检查知识库及 Git 发布状态。',
    workspace_protocol_required:'完整工作目录要求模板协议 1.1。',
    invalid_template_config:'实例配置不符合模板规范，请展开“查看配置规范”核对。',
    template_version_conflict:'该版本已注册且元数据不同，请使用新版本号。',
    template_digest_mismatch:'ZIP 摘要与注册信息不匹配，请上传同一次打包生成的文件。',
    invalid_template_package:'程序包结构不合法，请使用标准打包工具生成 ZIP。',
    template_not_installed:'模板尚未安装，请先上传程序包。',
    template_package_changed:'程序包已发生变更，无法使用该版本。',
    template_package_unavailable:'程序包不存在或不可用，请检查服务端存储。',
    inbox_claim_conflict:'材料已被领取、已完成或租约已过期，请刷新状态后重试。',
    library_disabled:'知识库已停用，请先启用。',
    invalid_grants:'提交授权必须在此 Key 的当前访问范围内。'
  };
  if(messages[data.error])return messages[data.error];
  if(Array.isArray(data.detail))return data.detail.map(e=>`${(e.loc||[]).slice(1).join('.')}：${e.msg}`).join('；');
  return '';
}
