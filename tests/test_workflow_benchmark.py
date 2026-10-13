import asyncio
import csv
import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from benchmarks.agent.runner import Recorder
from benchmarks.workflows.run import Workflow, answer_check, intact
from tools.benchmark_file_tools import Suite


def test_oracles_reject_unrelated_changes_and_wrong_answers():
    assert intact({'a':'old','b':'keep'}, {'a':'new','b':'keep'}, ['a'])
    assert not intact({'a':'old','b':'keep'}, {'a':'new'}, ['a'])
    assert not answer_check({'final':json.dumps({'answer':'done','completed':True,'facts':{'x':31},'sources':['a']})}, {'field':'x','value':30})


def test_review_scenario_contract_uses_real_server_and_worker(tmp_path, monkeypatch):
    """Fixture/verification contract test, not an LLM benchmark score."""
    monkeypatch.setenv('KB_TEMPLATE_TRUSTED_PYTHON','1')
    async def check():
        recorder=Recorder(); suite=Suite(tmp_path,app_wrapper=recorder.wrap,short_fixture=True)
        settings=suite.app.user_middleware[0].kwargs['settings']
        admin=httpx.Client(base_url=suite.url,headers={'Authorization':'Bearer '+settings.admin_token},trust_env=False)
        dataset=Path('benchmarks/datasets/workflows').resolve()
        with (dataset/'tasks.csv').open(encoding='utf-8-sig',newline='') as f:
            tasks={r['id']:r for r in csv.DictReader(f)}
        expected=json.loads((dataset/'expectations.json').read_text(encoding='utf-8'))
        args=SimpleNamespace(dataset=dataset,output=tmp_path)
        data={'planned_episodes':2,'episodes':[],'operator_actions':[]}
        flow=Workflow(suite,recorder,admin,args,tasks,expected,data)
        async def simulated_agent(identifier, repeat, key, oracle):
            async with suite.client(key) as probe:
                if identifier=='W_SUBMIT':
                    libs=await probe.call('list_libraries')
                    lid=libs['libraries'][0]['id']
                    result=await probe.call('submit_material',material={'library_id':lid,'submission_id':'contract-1','content':{'text':'白鹭验收保留 WF-9527'}})
                    assert '_error' not in result
                    final={}
                else:
                    final={'answer':'WF-9527','facts':{'receipt':'WF-9527'},'sources':['demo-inbox/INDEX.md'],'completed':True}
                checks=oracle({'final':json.dumps(final)},[])
                assert all(checks.values()),checks
                return {'checks':checks}
        flow.agent=simulated_agent
        try:
            await flow.update(1)
            assert any(r.get('completed') for r in data['operator_actions'])
        finally:
            admin.close(); suite.close()
    asyncio.run(check())
