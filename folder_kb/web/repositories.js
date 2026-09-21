'use strict';
const jobLabels={queued:'等待同步',running:'同步中',succeeded:'已发布',unchanged:'已是最新',failed:'同步失败'};
function repositoriesView(){
  const rows=repositoryState.repositories;
  return `${repositoryState.worker_online?'':'<div class="banner">同步进程未在线，任务将保留在队列中。</div>'}<div class="panel">${rows.length?`<div class="table-wrap"><table><thead><tr><th>仓库 / 分支</th><th>已发布版本</th><th>关联知识库</th><th>同步</th><th>操作</th></tr></thead><tbody>${rows.map(r=>`<tr><td><strong>${escapeHTML(r.name)}</strong><small class="repo-url">${escapeHTML(r.url || r.import_path)}</small><small>${escapeHTML(r.branch || '待识别分支')}</small></td><td><code>${escapeHTML(r.current_commit?.slice(0,12)||'尚未发布')}</code><small>${escapeHTML(date(r.last_sync))}</small></td><td>${r.libraries.map(l=>badge(l.name)).join(' ')||'—'}</td><td>${badge(jobLabels[r.job?.status]||'尚未同步',r.job?.status==='failed'?'bad':['queued','running'].includes(r.job?.status)?'warn':'good')}<small>${!r.enabled?'自动同步已暂停':r.interval_seconds?`每 ${r.interval_seconds/60} 分钟`:'手动更新'}</small>${r.enabled&&r.next_sync?`<small>下次 ${escapeHTML(date(r.next_sync*1000))}</small>`:''}${r.last_error?`<small class="error repo-error">${escapeHTML(r.last_error)}</small>`:''}</td><td><div class="actions repo-actions">${[['sync','立即更新'],['bind','关联知识库'],['settings','设置'],['history','同步记录']].map(([action,label])=>`<button class="text-button" data-repo-action="${action}" data-id="${r.id}" ${action==='sync'&&['queued','running'].includes(r.job?.status)||action==='bind'&&!r.release_path?'disabled':''}>${label}</button>`).join('')}</div></td></tr>`).join('')}</tbody></table></div>`:empty('还没有 Git 仓库','添加远端仓库，或接管挂载根目录下已有的仓库。')}</div>`;
}
$('#view').addEventListener('click',async event=>{
  const button=event.target.closest('[data-repo-action]');if(!button)return;
  const repo=repositoryState.repositories.find(r=>r.id===button.dataset.id);if(!repo)return;
  try{
    if(button.dataset.repoAction==='sync'){button.disabled=true;await api(`/repositories/${repo.id}/sync`,{method:'POST'});await refresh();toast('已加入同步队列');}
    if(button.dataset.repoAction==='settings')repositoryForm(repo);
    if(button.dataset.repoAction==='bind')repositoryBindings(repo);
    if(button.dataset.repoAction==='history'){
      const data=await api(`/repositories/${repo.id}/jobs`);
      modal('同步记录 · '+repo.name,`<div class="table-wrap"><table><thead><tr><th>时间</th><th>结果 / 版本</th></tr></thead><tbody>${data.jobs.map(j=>`<tr><td>${escapeHTML(date(j.finished_at||j.created_at))}<small>${escapeHTML({initial:'首次同步',manual:'手动更新',scheduled:'定时更新'}[j.trigger])}</small></td><td>${badge(jobLabels[j.status],j.status==='failed'?'bad':'')}<small><code>${escapeHTML(j.after_commit?.slice(0,12)||j.before_commit?.slice(0,12)||'—')}</code></small>${j.error?`<p class="error">${escapeHTML(j.error)}</p>`:''}</td></tr>`).join('')}</tbody></table></div>`);
    }
  }catch(error){report(error);button.disabled=false;}
});
function repositoryForm(repo=null){
  modal(repo?'仓库设置':'添加 Git 仓库',`<form id="repo-form">
    ${repo?'':'<div class="field"><label for="repo-mode">来源</label><select id="repo-mode"><option value="remote">拉取远端仓库</option><option value="import">接管已有目录</option></select></div>'}
    <div class="field"><label for="repo-name">名称</label><input id="repo-name" name="name" maxlength="80" required value="${escapeHTML(repo?.name||'')}"></div>
    ${repo?`<p class="repo-url">${escapeHTML(repo.url||repo.import_path)} · ${escapeHTML(repo.branch)}</p>`:`<div id="remote-fields"><div class="field"><label for="repo-url">仓库地址</label><input id="repo-url" name="url" placeholder="https://git.example.com/team/knowledge.git" required></div><div class="field"><label for="repo-branch">分支</label><input id="repo-branch" name="branch" value="main" required></div></div><div id="import-fields" class="field hidden"><label for="repo-path">已有 Git 文件夹</label><div class="form-row"><input id="repo-path" name="import_path" placeholder="根目录下的相对路径" disabled><button type="button" id="choose-dir" class="secondary">选择目录</button></div></div>`}
    <div class="field"><label for="repo-credential">凭据配置名（公开仓库可留空）</label><input id="repo-credential" name="credential" pattern="[a-z0-9][a-z0-9_-]{0,63}" value="${escapeHTML(repo?.credential||'')}" placeholder="例如：team-readonly"></div>
    <div class="field"><label for="repo-interval">自动更新间隔（分钟，0 表示手动）</label><input id="repo-interval" name="interval" type="number" min="0" max="43200" step="1" required value="${(repo?.interval_seconds||0)/60}"></div>
    ${repo?`<label class="check"><input name="enabled" type="checkbox" ${repo.enabled?'checked':''}>启用自动更新</label>`:''}
    <p id="repo-error" class="error" role="alert"></p><div class="modal-actions"><button class="primary">${repo?'保存':'添加并同步'}</button></div></form>`);
  if(!repo){
    $('#repo-mode').onchange=()=>{const imported=$('#repo-mode').value==='import';$('#remote-fields').classList.toggle('hidden',imported);$('#import-fields').classList.toggle('hidden',!imported);for(const id of ['repo-url','repo-branch']){$('#'+id).disabled=imported;$('#'+id).required=!imported;}$('#repo-path').disabled=!imported;$('#repo-path').required=imported;};
    $('#choose-dir').onclick=()=>directoryBrowser($('#repo-form'),$('#repo-path'));
  }
  $('#repo-form').onsubmit=async event=>{
    event.preventDefault();const data=new FormData(event.target),button=event.submitter;button.disabled=true;
    const body={name:data.get('name'),credential:data.get('credential').trim(),interval_seconds:Number(data.get('interval'))*60};
    if(repo)body.enabled=data.has('enabled');else if($('#repo-mode').value==='import')body.import_path=data.get('import_path');else Object.assign(body,{url:data.get('url').trim(),branch:data.get('branch').trim()});
    try{await api('/repositories'+(repo?'/'+repo.id:''),{method:repo?'PUT':'POST',body:JSON.stringify(body)});$('#modal').close();await refresh();toast(repo?'设置已保存':'仓库已加入同步队列');}catch(error){$('#repo-error').textContent=error.message;}finally{button.disabled=false;}
  };
}
function repositoryBindings(repo){
  const libraries=snapshot.libraries.filter(l=>!l.repository||l.repository.repository_id===repo.id);
  modal('关联知识库 · '+repo.name,`<form id="binding-form"><div class="field"><label for="binding-library">知识库</label><select id="binding-library"><option value="">新建知识库</option>${libraries.map(l=>`<option value="${l.id}">${escapeHTML(l.name)} · ${l.id}</option>`).join('')}</select></div><div class="field"><label for="binding-id">知识库 ID</label><input id="binding-id" required maxlength="64" pattern="[a-z0-9][a-z0-9_-]{0,63}"></div><div class="field"><label for="binding-name">知识库名称</label><input id="binding-name" required maxlength="80"></div><div class="field"><label for="binding-path">仓库内目录（. 表示整个仓库）</label><input id="binding-path" required value="."></div><p id="binding-error" class="error" role="alert"></p><div class="modal-actions"><button class="primary">保存关联</button></div></form>${repo.libraries.length?`<hr><h3>已有关联</h3>${repo.libraries.map(l=>`<div class="live-row"><span>${escapeHTML(l.name)} <code>${escapeHTML(l.subpath)}</code></span><button class="text-button" data-unbind="${l.library_id}">解除关联</button></div>`).join('')}`:''}`);
  $('#binding-library').onchange=()=>{const lib=libraries.find(l=>l.id===$('#binding-library').value);$('#binding-id').value=lib?.id||'';$('#binding-id').readOnly=!!lib;$('#binding-name').value=lib?.name||'';$('#binding-path').value=lib?.repository?.subpath||'.';};
  $('#binding-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;
    try{await api(`/repositories/${repo.id}/libraries`,{method:'POST',body:JSON.stringify({library_id:$('#binding-id').value,name:$('#binding-name').value,subpath:$('#binding-path').value,existing:!!$('#binding-library').value})});$('#modal').close();await refresh();toast('关联已生效');}catch(error){$('#binding-error').textContent=error.message;}finally{button.disabled=false;}
  };
  $('#modal-body').querySelectorAll('[data-unbind]').forEach(button=>button.onclick=async()=>{
    if(!confirm('解除后保留当前目录及 Key 授权，后续不再跟随仓库更新。'))return;
    try{await api(`/repositories/${repo.id}/libraries/${button.dataset.unbind}`,{method:'DELETE'});await refresh();repositoryBindings(repositoryState.repositories.find(r=>r.id===repo.id));}catch(error){$('#binding-error').textContent=error.message;}
  });
}
