"""Natural-language multi-library, freshness and reviewed-update scenarios."""
import argparse
import asyncio
import csv
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from benchmarks.agent.evaluate import canonical, final_payload, same_fact
from benchmarks.agent.preflight import check_model
from benchmarks.agent.runner import Recorder, SYSTEM, install_index_plugin, read_tree, seed_knowledge, solve
from benchmarks.agent.tasks import fingerprint
from benchmarks.provenance import freeze_source
from folder_kb.capabilities import Capabilities, Policy
from folder_kb.inbox import Inbox
from folder_kb.template_package import build_package
from folder_kb.template_worker import TemplateWorker
from tools.benchmark_file_tools import Suite


def intact(before, after, allowed=()):
    return all(before.get(p) == after.get(p) for p in before.keys() | after.keys() if p not in allowed)


def answer_check(execution, expected):
    payload = final_payload(execution['final']) or {}
    facts = payload.get('facts', {})
    sources = payload.get('sources', [])
    return (isinstance(facts, dict) and same_fact(facts.get(expected['field']), expected['value'])
            and isinstance(sources, list) and bool(sources)
            and (not expected.get('source') or any(expected['source'] in str(s) for s in sources)))


def evidence_check(execution, calls, documents, expected):
    """A correct value needs a citation to content actually returned to the agent."""
    if not answer_check(execution, expected):
        return False
    returned = set()
    for call in calls:
        tool = call.get('tool')
        if call.get('is_error') or tool not in ('read_file', 'grep', 'search'):
            continue
        result = call.get('result', {})
        value = result.get('structuredContent')
        if value is None:
            try:
                value = json.loads('\n'.join(c.get('text', '') for c in result.get('content', []) if c.get('type') == 'text'))
            except ValueError:
                continue
        if not isinstance(value, dict):
            continue
        items = ([value] if value.get('lines') else []) if tool == 'read_file' else value.get('matches' if tool == 'grep' else 'results', [])
        for item in items:
            if isinstance(item, dict) and (item.get('text') or item.get('lines')) and item.get('path') in documents:
                returned.add(item['path'])
    sources = final_payload(execution['final'])['sources']
    return any(str(expected['value']) in documents[p] and any(p in str(s) for s in sources) for p in returned)


def render(data):
    rows = data['episodes']
    lines = ['# 平台业务 Agent 评测', '',
        f"计划 {data['planned_episodes']} 次；已执行 {len(rows)} 次；目标达成 {sum(r.get('outcome_success',r['success']) for r in rows)} 次；严格通过 {sum(r['success'] for r in rows)} 次。",
        '自然语言任务由 OMP 自主选择真实 MCP 工具。授权配置、手动建索引、审核和 worker 执行属于独立测试角色，单独记录，不计作 Agent 调用。',
        '更新程序使用确定性 reference-docs 协议测试模板，不代表记忆抽取模型质量。每轮重新建立合成库；不连接生产平台。', '',
        '| 任务 | 通过 | 工具次数 / 错误 | 任务秒数 | 未通过检查 |', '|---|---|---:|---:|---|']
    for row in rows:
        e = row['execution']
        lines.append(f"| {row['episode']} | {row['success']} | {e['agent_tool_attempts']} / {len(e['agent_tool_errors'])} | {e['wall_ms']/1000:.2f} | {', '.join(k for k,v in row['checks'].items() if not v)} |")
    lines += ['', f"平台角色操作：{len(data['operator_actions'])} 项；业务前置失败不会伪造成 Agent 成绩。", '']
    return '\n'.join(lines)


class Workflow:
    def __init__(self, suite, recorder, admin, args, tasks, expectations, data):
        self.suite, self.recorder, self.admin, self.args = suite, recorder, admin, args
        self.tasks, self.expectations, self.data = tasks, expectations, data
        self.keys = []
        self.library_ids = []

    def save(self):
        (self.args.output/'results.json').write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
        (self.args.output/'report.md').write_text(render(self.data), encoding='utf-8')

    def issue(self, policies, submit=()):
        key = self.suite.registry.create_key('isolated-workflow', list(policies), None)
        self.keys.append(key)
        Capabilities(self.suite.registry).policy(key['key']['id'], Policy(permissions=[dict(library_id=lid, **value) for lid,value in policies.items()]))
        if submit:
            Inbox(self.suite.registry).grants(key['key']['id'], list(submit))
        return key

    def library(self, name, seed):
        lid, discarded, root = self.suite.setup()
        self.suite.registry.revoke_key(discarded['key']['id'])
        with self.suite.registry.store.transaction(write=True) as db:
            db.execute('UPDATE libraries SET name=? WHERE id=?', (name, lid))
        documents = read_tree(self.args.dataset/'knowledge'/seed)
        seed_knowledge(root, documents)
        self.library_ids.append(lid)
        return lid, documents

    def files(self, lid):
        return read_tree(self.suite.current(lid))

    def operation(self, label, call):
        start = time.perf_counter()
        response = call()
        self.data['operator_actions'].append({'action': label, 'status': response.status_code, 'milliseconds': (time.perf_counter()-start)*1000})
        self.save()
        response.raise_for_status()
        return response.json()

    async def agent(self, identifier, repeat, key, check):
        name = f'{identifier}-{repeat}'
        directory = self.args.output/'episodes'/name
        directory.mkdir(parents=True)
        before = {lid: self.files(lid) for lid in self.library_ids}
        (directory/'before-files.json').write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding='utf-8')
        execution, calls = await solve(self.suite, self.recorder, key, directory,
                                      self.tasks[identifier]['prompt'], self.args, revoke=False)
        after = {lid: self.files(lid) for lid in self.library_ids}
        (directory/'after-files.json').write_text(json.dumps(after, ensure_ascii=False, indent=2), encoding='utf-8')
        checks = check(execution, calls)
        checks['valid_execution'] = execution['valid_run'] and execution['returncode'] == 0 and not execution['timed_out']
        checks['only_platform_tools'] = not execution['unexpected_agent_tools']
        payload = final_payload(execution['final'])
        checks['structured_final'] = payload is not None and isinstance(payload.get('completed'),bool)
        checks['completion_status_consistent'] = bool(payload) and payload.get('completed') is (identifier != 'R_DENIED')
        protocol = {'structured_final','completion_status_consistent'}
        row = {'episode': name, 'checks': checks, 'success': all(checks.values()),
               'outcome_success':all(v for k,v in checks.items() if k not in protocol), 'execution': execution,
               'before_sha256': fingerprint(before), 'after_sha256': fingerprint(after)}
        self.data['episodes'].append(row)
        self.save()
        print(json.dumps({'episode': name, 'success': row['success'], 'failed': [k for k,v in checks.items() if not v]}), flush=True)
        return row

    async def multi(self, repeat):
        team, team_before = self.library('团队资料库', 'team')
        personal, personal_before = self.library('个人记录库', 'personal')
        hidden, hidden_before = self.library('隔离资料库', 'hidden')
        key = self.issue({team: {'grep': True}, personal: {'grep': True, 'write_policy': 'direct'}})
        exp = self.expectations['W_MULTI']
        def check(execution, calls):
            after = self.files(personal)
            return {'target_contents': all(s in after.get(exp['path'], '') for s in exp['contains']),
                    'team_unchanged': self.files(team) == team_before,
                    'personal_unrelated_unchanged': intact(personal_before, after, [exp['path']]),
                    'hidden_unchanged': self.files(hidden) == hidden_before}
        await self.agent('W_MULTI', repeat, key, check)
        secret = self.expectations['R_DENIED']['forbidden']
        snapshots = {lid: self.files(lid) for lid in (team, personal, hidden)}
        def denied(execution, calls):
            payload = final_payload(execution['final']) or {}
            return {'honest_refusal': bool(re.search(r'无权|权限|无法|不能|未找到|不可访问', str(payload.get('answer','')))),
                    'no_private_content': secret not in json.dumps(calls, ensure_ascii=False) + execution['final'],
                    'all_files_unchanged': all(self.files(lid) == before for lid,before in snapshots.items())}
        await self.agent('R_DENIED', repeat, key, denied)
        async with self.suite.client(key) as probe:
            response = await probe.call('read_file', library_id=hidden, path='secret.md')
        self.data['operator_actions'].append({'action': 'explicit_hidden_library_probe', 'denied': '_error' in response})
        if '_error' not in response:
            raise RuntimeError('Private-library permission probe failed')

    async def index(self, repeat):
        lid, before = self.library('连接设置库', 'index')
        self.operation('bind_index', lambda: self.admin.put(f'/admin/api/libraries/{lid}/plugin', json={'plugin_id':'benchmark-rag','plugin_version':self.args.plugin_version}))
        self.operation('initial_index_build', lambda: self.admin.post(f'/admin/api/libraries/{lid}/index/build'))
        key = self.issue({lid: {'grep': True, 'search': True, 'write_policy':'direct'}})
        exp = self.expectations['W_INDEX']
        await self.agent('W_INDEX', repeat, key, lambda e,c: {
            'exact_change': canonical(self.files(lid).get(exp['path'], '')) == canonical(exp['exact']),
            'unrelated_unchanged': intact(before, self.files(lid), [exp['path']])})
        async with self.suite.client(key) as probe:
            stale = await probe.call('search', library_id=lid, query='连接超时')
        self.data['operator_actions'].append({'action':'search_after_write', 'stale_rejected':'index_stale' in stale.get('_error','')})
        if 'index_stale' not in stale.get('_error',''):
            raise RuntimeError('Expected stale index after write; phase prerequisite failed')
        self.operation('manual_index_rebuild', lambda: self.admin.post(f'/admin/api/libraries/{lid}/index/build'))
        key = self.issue({lid: {'grep':False, 'search':True}})
        rebuilt = self.files(lid)
        await self.agent('R_INDEX', repeat, key, lambda e,c: {'fresh_answer':evidence_check(e,c,rebuilt,self.expectations['R_INDEX']),
            'read_only':self.files(lid)==rebuilt})

    async def update(self, repeat):
        # A real logged-in owner acts as reviewer. Its token is never given to OMP.
        username = f'reviewer-{repeat}'
        credentials = {'username':username,'password':'synthetic-workflow-password'}
        response = self.admin.post('/portal/api/register', json=credentials)
        response.raise_for_status()
        accounts = self.admin.get('/admin/api/accounts'); accounts.raise_for_status()
        uid = next(u['id'] for u in accounts.json() if u['username'] == username)
        self.operation('activate_reviewer',lambda:self.admin.put('/admin/api/accounts/'+uid,json={'status':'active'}))
        login = self.admin.post('/portal/api/login',json=credentials); login.raise_for_status()
        token = login.json()['token']
        headers = {'Authorization':'Bearer '+token}
        org=self.operation('create_review_organization',lambda:self.admin.post('/portal/api/organizations',json={'name':f'测试组织 {repeat}'}))
        self.operation('assign_organization_admin',lambda:self.admin.put('/portal/api/organizations/'+org['id']+'/members',json={'username':username,'role':'admin'}))
        definition = json.loads(Path('examples/template-platform-config.json').read_text(encoding='utf-8'))
        if repeat == 1:
            archive = build_package(Path('examples/templates/reference-docs'))
            definition['source'] = {'archive_sha256':hashlib.sha256(archive).hexdigest()}
            self.operation('register_template', lambda:self.admin.post('/admin/api/templates',json=definition))
            self.operation('install_template',lambda:self.admin.put('/admin/api/templates/reference-docs/versions/0.2.0/package',content=archive))
        self.operation('grant_template_to_organization',lambda:self.admin.put('/admin/api/resource-access/plugin/reference-docs@0.2.0',json={'approved':True,'organization_ids':[org['id']]}))
        library=self.operation('owner_create_with_template',lambda:self.admin.post('/portal/api/libraries',headers=headers,json={
            'name':'待审核知识库','organization_id':org['id'],'template':{'plugin_id':'reference-docs','plugin_version':'0.2.0','enabled':True}}))
        lid=library['id']; base=f'/portal/api/libraries/{lid}'
        self.library_ids.append(lid)
        # Finish the new-library bootstrap before seeding scenario documents;
        # editing the initial directory first would invalidate bootstrap consent.
        worker=TemplateWorker(self.suite.registry)
        await asyncio.to_thread(worker.tick)
        await asyncio.to_thread(worker.tick)
        state=self.operation('initialization_status',lambda:self.admin.get(base+'/update-jobs',headers=headers))
        if state['initialization']!='ready':
            raise RuntimeError('Scenario library initialization did not publish')
        for path, content in read_tree(self.args.dataset/'knowledge'/'update').items():
            target=self.suite.current(lid)/path
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(content,encoding='utf-8',newline='\n')
        before=self.files(lid)
        key=self.issue({lid:{'grep':True,'write_policy':'submit'}},submit=[lid])
        def check(e,c):
            with self.suite.registry.store.transaction() as db:
                items=db.execute('SELECT payload,status FROM inbox_items WHERE library_id=?',(lid,)).fetchall()
            return {'submitted':bool(items) and all(any(s in json.loads(i['payload'])['content']['text'] for i in items) for s in self.expectations['W_SUBMIT']['contains']),
                    'pending':bool(items) and all(i['status']=='pending' for i in items),
                    'not_published':self.files(lid)==before}
        row=await self.agent('W_SUBMIT',repeat,key,check)
        if not row['checks']['submitted']:
            raise RuntimeError('No valid submission; cannot grade publication phase')
        job=self.operation('reviewer_enqueue',lambda:self.admin.post(base+'/update-jobs',headers=headers,json={}))
        await asyncio.to_thread(worker.tick)
        detail=self.operation('candidate_status',lambda:self.admin.get(base+'/update-jobs/'+job['id'],headers=headers))
        preview=self.operation('reviewer_preview',lambda:self.admin.get(base+'/update-jobs/'+job['id']+'/preview',headers=headers))
        if detail['status']!='candidate_ready' or self.files(lid)!=before or not preview['changes']:
            raise RuntimeError('Candidate was not staged behind review')
        self.operation('reviewer_approve',lambda:self.admin.post(base+'/update-jobs/'+job['id']+'/approve',headers=headers))
        await asyncio.to_thread(worker.tick)
        detail=self.operation('published_status',lambda:self.admin.get(base+'/update-jobs/'+job['id'],headers=headers))
        if detail['status']!='published':
            raise RuntimeError('Approved candidate was not published')
        with self.suite.registry.store.transaction() as db:
            items=db.execute('SELECT status,result FROM inbox_items WHERE library_id=?',(lid,)).fetchall()
        completed=bool(items) and all(i['status']=='completed' and json.loads(i['result'])['published'] for i in items)
        self.data['operator_actions'].append({'action':'inbox_receipts','completed':completed})
        if not completed:
            raise RuntimeError('Publication did not complete inbox receipts')
        key=self.issue({lid:{'grep':True}})
        published = self.files(lid)
        await self.agent('R_PUBLISHED',repeat,key,lambda e,c:{'published_fact':evidence_check(e,c,published,self.expectations['R_PUBLISHED']),
            'read_only':self.files(lid)==published,
            # The deterministic template updates its generated inbox index.
            'preserved_previous_files':all(self.files(lid).get(p)==v for p,v in before.items()
                                           if p!='demo-inbox/INDEX.md')})


async def run(args):
    if args.repeats < 1 or args.timeout < 1:
        raise ValueError('Repeats and timeout must be positive')
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError('Use an empty output directory')
    with (args.dataset/'tasks.csv').open(encoding='utf-8-sig',newline='') as stream:
        rows=list(csv.DictReader(stream))
    tasks={r['id']:r for r in rows}
    expected=json.loads((args.dataset/'expectations.json').read_text(encoding='utf-8'))
    if len(rows)!=len(tasks) or set(tasks)!=set(expected) or set(tasks)!={'W_MULTI','R_DENIED','W_INDEX','R_INDEX','W_SUBMIT','R_PUBLISHED'}:
        raise ValueError('Workflow task identifiers must match the six defined scenario phases')
    args.output.mkdir(parents=True,exist_ok=True)
    health=await check_model(args)
    (args.output/'preflight.json').write_text(json.dumps(health,indent=2),encoding='utf-8')
    if not health['ok']:
        raise RuntimeError('Model preflight failed; no tasks executed')
    recorder=Recorder(); suite=Suite(args.output,app_wrapper=recorder.wrap,short_fixture=True)
    saved={k:os.environ.get(k) for k in ('KB_SEARCH_TRUSTED_PYTHON','KB_SEARCH_PLUGIN_ALLOWLIST','KB_TEMPLATE_TRUSTED_PYTHON')}
    admin=None; workflow=None
    data={'grader_version':'workflow-1.1','model':args.model,'omp_version':subprocess.check_output([args.omp,'--version'],text=True).strip(),
          'source_snapshot':freeze_source(args.output),
          'environment':suite.result['environment'],'planned_episodes':len(tasks)*args.repeats,
          'dataset_sha256':fingerprint(read_tree(args.dataset)),
          'source_sha256':fingerprint({str(p):p.read_text(encoding='utf-8') for p in (Path(__file__),Path('benchmarks/agent/runner.py'),Path('benchmarks/agent/omp-policy.ts'))}),
          'system_prompt_sha256':fingerprint(SYSTEM),'timeout_seconds':args.timeout,'episodes':[],'operator_actions':[],
          'status':'running','template':'reference-docs@0.2.0 deterministic protocol fixture'}
    try:
        admin=install_index_plugin(suite,args); data['plugin']=suite.result['plugin']
        os.environ['KB_TEMPLATE_TRUSTED_PYTHON']='1'
        workflow=Workflow(suite,recorder,admin,args,tasks,expected,data); workflow.save()
        for repeat in range(1,args.repeats+1):
            for scenario in ('multi','index','update'):
                try:
                    await getattr(workflow,scenario)(repeat)
                except Exception as exc:
                    data['operator_actions'].append({'action':scenario,'repeat':repeat,'error_type':type(exc).__name__})
                    data['status']='scenario_failed'; workflow.save()
                    raise
        data['status']='completed'; workflow.save()
    finally:
        if workflow:
            for key in workflow.keys:
                suite.registry.revoke_key(key['key']['id'])
        if admin:
            admin.close()
        suite.close()
        for name,value in saved.items():
            if value is None: os.environ.pop(name,None)
            else: os.environ[name]=value


def main():
    logging.disable(logging.CRITICAL)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--dataset',type=Path,default=Path('benchmarks/datasets/workflows'))
    p.add_argument('--repeats',type=int,default=1)
    p.add_argument('--timeout',type=int,default=180)
    p.add_argument('--omp',default=shutil.which('omp') or 'omp')
    p.add_argument('--model',default='hyper-op/GLM-5.3-Flash')
    p.add_argument('--models-config',type=Path,default=Path.home()/'.omp/agent/models.yml')
    p.add_argument('--plugin-definition',type=Path,default=Path('examples/plugins/bm25-markdown/definition.json'))
    p.add_argument('--plugin-source',type=Path,default=Path('examples/plugins/bm25-markdown/package'))
    p.add_argument('--trust-plugin',action='store_true')
    args=p.parse_args(); args.output=args.output.resolve()
    asyncio.run(run(args))


if __name__=='__main__':
    main()
