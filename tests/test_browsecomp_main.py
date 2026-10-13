"""Main benchmark integrity tests use invented questions only."""
import asyncio
import copy
import csv
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.agent.tasks import load_dataset
from benchmarks.browsecomp.prepare import select_questions,write_dataset
from benchmarks.browsecomp.scorecard import clustered_interval,paired_gain,summarize
from benchmarks.browsecomp.review import export_review,apply_review
from benchmarks.browsecomp.campaign import read_config,run
from benchmarks.retrieval.queries import query_from
from tests.test_browsecomp_benchmark import record


def test_holdout_sampling_is_seeded_and_excludes_development():
    rows=[dict(record(),query_id=str(i)) for i in range(20)]
    a=select_questions(rows,5,42,{'0','1'})
    assert a==select_questions(list(reversed(rows)),5,42,{'0','1'})
    assert not {r['query_id'] for r in a}&{'0','1'}
    assert a!=select_questions(rows,5,43,{'0','1'})
    with pytest.raises(ValueError):select_questions(rows,19,42,{'0','1'})
    with pytest.raises(ValueError):select_questions(rows+[rows[0]],2,42)


def metric(q,repeat,correct):
    return {'task_id':q,'repeat':repeat,'answer_correct':correct,'evidence_recall':.5,'gold_recall':1,
            'agent_tool_attempts':2,'server_calls':2,'server_errors':0,'wall_seconds':1,
            'status':'completed','usage':{'totalTokens':100}}


def test_intervals_cluster_repeats_and_keep_unknown_denominators():
    ci=clustered_interval({'a':[1,1,1],'b':[0,0,0]})
    assert ci==clustered_interval({'a':[1],'b':[0]})
    assert ci['questions']==2 and ci['mean']==.5
    summary=summarize([metric('a',1,True),metric('b',1,None)])
    assert summary['confirmed_correct_rate']['mean']==.5
    assert summary['judged_accuracy']==1 and summary['judgment_coverage']==.5
    assert summary['incorrect']==0 and summary['unjudged']==1
    assert clustered_interval({'a':[1,1]})['low'] is None


def test_paired_gains_do_not_cherry_pick_missing_or_unknown_pairs():
    a=[metric('a',1,True),metric('b',1,True)]
    b=[metric('a',1,False),metric('b',1,None)]
    result=paired_gain(a,b)
    assert result['confirmed_gain']['mean']==1
    assert result['judged_wins']==1 and result['pairs_with_unjudged']==1
    with pytest.raises(ValueError):paired_gain(a,b[:1])
    with pytest.raises(ValueError):paired_gain(a+a,b)


def test_query_validation_never_truncates_or_accepts_tools():
    value={'final':json.dumps({'answer':'query','completed':True,'facts':{'query':'known terms'}}),
           'valid_run':True,'returncode':0,'timed_out':False,'agent_tool_attempts':0}
    assert query_from(value)=='known terms'
    without_completed=copy.deepcopy(value)
    without_completed['final']=json.dumps({'facts':{'query':'known terms'}})
    assert query_from(without_completed)=='known terms'
    with pytest.raises(ValueError):query_from(dict(value,agent_tool_attempts=1))
    with pytest.raises(ValueError):query_from(dict(value,valid_run=False))
    value['final']=json.dumps({'answer':'query','completed':True,'facts':{'query':'x'*201}})
    with pytest.raises(ValueError):query_from(value)


def test_query_format_retry_is_bounded_logged_and_never_uses_answers(review_fixture,tmp_path,monkeypatch):
    from benchmarks.retrieval import queries
    _,dataset,_=review_fixture
    async def healthy(_):return {'ok':True}
    seen=[]
    async def fake_solve(suite,recorder,key,directory,prompt,args,**kwargs):
        assert key is None and kwargs['closed_book'] and 'Example-42' not in prompt
        seen.append(prompt)
        final='not json' if len(seen)==1 else json.dumps({'facts':{'query':'sample release current'}})
        return {'final':final,'valid_run':True,'returncode':0,'timed_out':False,'agent_tool_attempts':0},[]
    monkeypatch.setattr(queries,'check_model',healthy);monkeypatch.setattr(queries,'solve',fake_solve)
    args=SimpleNamespace(dataset=dataset,output=tmp_path/'queries',model='test',labels='evidence')
    result=asyncio.run(queries.run(args))
    assert result['status']=='completed' and result['queries'][0]['format_attempts']==2
    assert [a['valid_query'] for a in result['attempts']]==[False,True]
    assert seen[0]==seen[1]


@pytest.fixture
def review_fixture(tmp_path):
    dataset=tmp_path/'dataset';write_dataset(dataset,[record()],1,42,{'repository':'synthetic'})
    _,_,metadata=load_dataset(dataset)
    rows=[]
    for variant,correct in [('closed_book',False),('search',True)]:
        rows.append({'episode':'RBCP_100-'+variant+'-1','task_id':'RBCP_100','variant':variant,'repeat':1,
            'valid_run':True,'timed_out':False,'returncode':0,
            'evaluation':{'answer':'Candidate example','prediction':'Example-42','protocol_success':True}})
    run=tmp_path/'run';run.mkdir();raw=json.dumps({'dataset':metadata,'episodes':rows}).encode()
    (run/'results.json').write_bytes(raw)
    judge=tmp_path/'judgments.json';judge.write_text(json.dumps({'run_sha256':hashlib.sha256(raw).hexdigest(),
        'episodes':[{'episode':r['episode'],'correct':r['variant']=='search'} for r in rows]}))
    return run,dataset,judge


def test_human_review_is_blinded_bound_and_never_overwrites_machine_scores(review_fixture,tmp_path):
    run,dataset,judge=review_fixture;original=judge.read_bytes();queue=tmp_path/'review'
    result=export_review(run,dataset,judge,queue,sample=1)
    assert result['review_items']==2
    path=queue/'review.csv'
    with path.open(encoding='utf-8-sig',newline='') as f:reader=csv.DictReader(f);columns=reader.fieldnames;rows=list(reader)
    assert 'variant' not in columns and 'machine_correct' not in columns
    with pytest.raises(ValueError,match='No human'):apply_review(queue,path,tmp_path/'empty')
    rows[0].update(decision='incorrect',reviewer='reviewer A',reason='Wrong synthetic example')
    with path.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=columns);w.writeheader();w.writerows(rows)
    result=apply_review(queue,path,tmp_path/'reviewed')
    assert result['reviewed']==1 and result['pending']==1 and judge.read_bytes()==original
    checked=json.loads((tmp_path/'reviewed/judgments.json').read_text())
    assert sum('machine_judgment' in r for r in checked['episodes'])==1
    rows[0]['reference_answer']='tampered'
    with path.open('w',encoding='utf-8-sig',newline='') as f:w=csv.DictWriter(f,fieldnames=columns);w.writeheader();w.writerows(rows)
    with pytest.raises(ValueError,match='edited'):apply_review(queue,path,tmp_path/'bad')


def test_campaign_plan_has_control_and_frozen_plugin_inputs(review_fixture,tmp_path):
    _,dataset,_=review_fixture
    config=Path('benchmarks/browsecomp/configs/main.json')
    plan=read_config(config,dataset)
    assert plan['planned_agent_episodes']==12
    assert plan['planned_query_generations']==1
    assert plan['jobs'][0]['kind']=='queries'
    assert plan['configuration']['plugins'][0]['source_sha256']
    args=SimpleNamespace(config=config,dataset=dataset,output=tmp_path/'campaign',resume=False,execute=False)
    asyncio.run(run(args))
    assert json.loads((args.output/'status.json').read_text())['state']=='planned'
    with pytest.raises(ValueError,match='new/empty'):asyncio.run(run(args))
    args.resume=True;asyncio.run(run(args))
    other=json.loads(config.read_text());other['timeout']=1
    args.config=tmp_path/'changed.json';args.config.write_text(json.dumps(other))
    with pytest.raises(ValueError,match='changed'):asyncio.run(run(args))


def test_closed_book_never_probes_mcp_or_exposes_native_tools(tmp_path,monkeypatch):
    from benchmarks.agent import runner
    class Registry:
        def revoke_key(self,*args):pytest.fail('No key should be created for closed book')
    suite=SimpleNamespace(url='http://127.0.0.1:1/unused',registry=Registry())
    source=tmp_path/'models.yml';source.write_text('synthetic')
    directory=tmp_path/'episode';directory.mkdir()
    args=SimpleNamespace(models_config=source,omp='omp',model='fake',timeout=1)
    class Process:
        returncode=0
        async def communicate(self):
            event={'type':'message_end','message':{'role':'assistant','content':[{'type':'text','text':'{}'}],'stopReason':'stop'}}
            return json.dumps(event).encode(),b''
    async def launch(*command,**kwargs):
        assert '--no-tools' in command
        env=kwargs['env'];assert env['FKB_BENCHMARK_TOOLS']=='[]' and env['FKB_BENCHMARK_INSTRUCTIONS']==''
        Path(env['FKB_BENCHMARK_POLICY_TRACE']).write_text('{"type":"provider_tools","names":[]}\n')
        return Process()
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    execution,calls=asyncio.run(runner.solve(suite,runner.Recorder(),None,directory,'Synthetic question',args,closed_book=True))
    assert not calls and execution['valid_run'] and execution['provider_tool_sets']==[[]]
    assert not (directory/'profile').exists()
