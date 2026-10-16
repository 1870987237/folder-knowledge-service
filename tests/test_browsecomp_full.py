from types import SimpleNamespace
import asyncio
import json
from pathlib import Path

import pytest

from benchmarks.agent.tasks import DiskCorpus, load_dataset
from benchmarks.browsecomp.full_prepare import materialize
from benchmarks.browsecomp.full_run import FullService, interrupted_execution, result_row
from benchmarks.agent.runner import Recorder
from folder_kb.errors import KnowledgeError
from folder_kb.retrieval import Retrieval
from folder_kb.search_large import LargeSearch


def dataset(path):
    doc=dict(docid='1',text='# Example\nA kiwi orchard produces fruit.\n')
    question=dict(query_id='1',query='Which fruit grows in the example orchard?',answer='kiwi',evidence_docs=[doc],gold_docs=[doc])
    materialize(path,[question],[doc,dict(docid='2',text='A pear garden grows elsewhere.\n')],[],expected_questions=1,expected_documents=2)
    return path


def test_full_import_keeps_every_document_and_question(tmp_path):
    root=dataset(tmp_path/'dataset')
    corpus,tasks,meta=load_dataset(root)
    assert isinstance(corpus,DiskCorpus) and len(corpus)==2 and len(tasks)==1
    assert meta['provenance']['scope']=='synthetic-full-import-fixture'
    assert corpus.verify()==meta['corpus_sha256']
    assert 'kiwi' in corpus['documents/1.md']
    # No silent filtering of changed or extra source data.
    (root/'knowledge/extra.md').write_text('extra',encoding='utf-8')
    with pytest.raises(ValueError,match='Unexpected'):corpus.verify()
    (root/'knowledge/extra.md').unlink()
    (root/'knowledge/documents/1.md').write_text('changed',encoding='utf-8')
    with pytest.raises(ValueError,match='differs'):corpus['documents/1.md']
    with pytest.raises(ValueError,match='changed'):corpus.verify()


def test_full_import_rejects_missing_evidence_and_wrong_counts(tmp_path):
    q=dict(query_id='1',query='Where?',answer='here',evidence_docs=[{'docid':'3'}],gold_docs=[{'docid':'3'}])
    with pytest.raises(ValueError,match='Missing official'):
        materialize(tmp_path/'missing',[q],[dict(docid='1',text='text')],[],expected_questions=1,expected_documents=1)
    with pytest.raises(ValueError,match='count mismatch'):
        materialize(tmp_path/'count',[q],[],[],expected_questions=2,expected_documents=1)
    assert not (tmp_path/'missing/manifest.json').exists()


def test_large_plugin_real_build_search_state_and_mutation(tmp_path,monkeypatch):
    root=dataset(tmp_path/'dataset');out=tmp_path/'output';out.mkdir()
    monkeypatch.setenv('KB_SEARCH_TRUSTED_PYTHON','1')
    monkeypatch.setenv('KB_SEARCH_PLUGIN_ALLOWLIST','')
    service=FullService(root,out,Recorder())
    try:
        service.install(); retrieval=Retrieval(service.registry)
        meta=retrieval.build('benchmark-corpus')
        assert meta['files']==2 and meta['chunks']==2
        assert retrieval.state('benchmark-corpus')['status']=='ready'
        result=retrieval.search('benchmark-corpus','kiwi',limit=5)
        assert result['results'][0]['path']=='documents/1.md'
        assert 'kiwi' in result['results'][0]['text']
        assert result['scope']['indexed_files']==2
        async def mcp_check():
            from folder_kb.capabilities import Capabilities,Policy
            key=service.registry.create_key('full-protocol-test',['benchmark-corpus'],None)
            Capabilities(service.registry).policy(key['key']['id'],Policy(permissions=[dict(library_id='benchmark-corpus',grep=False,search=True)]))
            async with service.client(key) as client:
                tools=(await client.session.list_tools()).tools
                assert 'search' in {t.name for t in tools} and 'grep' not in {t.name for t in tools}
                answer=await client.call('search',query='kiwi',library_id='benchmark-corpus')
                assert '_error' not in answer and answer['results'][0]['path']=='documents/1.md'
            service.registry.revoke_key(key['key']['id'])
        asyncio.run(mcp_check())
        assert retrieval.search('benchmark-corpus','kiwi',path='other')['results']==[]
        assert not list((retrieval.directory/'runtime').iterdir())
        _,folder,index,identity=retrieval.context('benchmark-corpus')
        old_pointer=retrieval.plugin_pointer('benchmark-corpus').read_bytes()
        import folder_kb.search_large as large
        with monkeypatch.context() as patch:
            patch.setattr(large,'execute',lambda *a,**k:(_ for _ in ()).throw(KnowledgeError('test','fail',502)))
            with pytest.raises(KnowledgeError):retrieval.build('benchmark-corpus')
        assert retrieval.plugin_pointer('benchmark-corpus').read_bytes()==old_pointer
        # No per-call index copies; generation mutation is detected.
        _,artifacts=LargeSearch(retrieval).generation('benchmark-corpus')
        with (artifacts/'search.sqlite3').open('ab') as f:f.write(b'changed')
        with pytest.raises(KnowledgeError,match='artifacts changed'):retrieval.search('benchmark-corpus','kiwi')
    finally:
        service.close()


def test_large_source_changes_and_bad_plugin_results(tmp_path,monkeypatch):
    root=dataset(tmp_path/'dataset');out=tmp_path/'output';out.mkdir()
    monkeypatch.setenv('KB_SEARCH_TRUSTED_PYTHON','1');monkeypatch.setenv('KB_SEARCH_PLUGIN_ALLOWLIST','')
    service=FullService(root,out,Recorder())
    try:
        service.install();retrieval=Retrieval(service.registry);retrieval.build('benchmark-corpus')
        import folder_kb.search_large as large
        with monkeypatch.context() as patch:
            patch.setattr(large,'execute',lambda *a,**k:dict(protocol_version='1.1',results=[dict(path='../answer.json')],has_more=False))
            with pytest.raises(KnowledgeError) as exc:retrieval.search('benchmark-corpus','kiwi')
            assert exc.value.code=='search_plugin_invalid'
        (root/'knowledge/documents/2.md').write_text('new content',encoding='utf-8')
        assert retrieval.state('benchmark-corpus')['status']=='stale'
        with pytest.raises(KnowledgeError) as exc:retrieval.search('benchmark-corpus','kiwi')
        assert exc.value.code=='index_stale'
    finally:service.close()


def test_interrupted_attempt_is_retained_as_failure(tmp_path):
    root=dataset(tmp_path/'dataset');corpus,tasks,_=load_dataset(root)
    directory=tmp_path/'episode';directory.mkdir()
    execution,calls=interrupted_execution(directory)
    known={p:e['sha256'] for p,e in corpus.entries.items()}
    result=result_row(tasks[0],execution,calls,known)
    assert result['interrupted'] and not result['valid_run'] and result['returncode']!=0
    assert result['evaluation']['answer_correct'] is None


@pytest.mark.parametrize('body',['short kiwi\n','first\r\nkiwi\r\nlast','a'*400+'kiwi'+'b'*5000,'first\n'+'kiwi\n'*500])
def test_corpus_excerpt_preserves_source_lines(body):
    from folder_kb.search_plugin import validate_hits
    path=Path('examples/plugins/sqlite-corpus/package/search.py')
    namespace={'__name__':'corpus_plugin'};exec(compile(path.read_bytes(),str(path),'exec'),namespace)
    start,end,text=namespace['excerpt'](body,'kiwi')
    hits=dict(protocol_version='1.1',results=[dict(path='a.md',start_line=start,end_line=end,text=text,title='',score=1)],has_more=False)
    assert validate_hits(hits,[('a.md',body,'hash','version')],5,'.')


@pytest.fixture
def isolated_omp_version(monkeypatch):
    """These runner contract tests simulate the agent, including its version probe."""
    import benchmarks.browsecomp.full_run as runner
    original = runner.subprocess.check_output
    def check_output(command, *args, **kwargs):
        if command == ['omp', '--version']:
            return 'synthetic-omp-test-version\n'
        return original(command, *args, **kwargs)
    monkeypatch.setattr(runner.subprocess, 'check_output', check_output)


def test_unreadable_corpus_records_failure_before_starting_service(tmp_path,monkeypatch,isolated_omp_version):
    import benchmarks.browsecomp.full_run as runner
    root=dataset(tmp_path/'dataset');out=tmp_path/'run'
    args=SimpleNamespace(dataset=root,output=out,allow_fixture=True,tasks=None,model='test/solver',judge_model='test/solver',
        timeout=10,omp='omp',models_config=Path('unused'),resume=False,prepare_only=True)
    def blocked(self,**kwargs):
        raise OSError(22,'Corpus access blocked')
    def unexpected_service(*args):
        pytest.fail('Service must not start with an unreadable corpus')
    monkeypatch.setattr(DiskCorpus,'verify',blocked)
    monkeypatch.setattr(runner,'FullService',unexpected_service)
    with pytest.raises(OSError):asyncio.run(runner.run(args))
    status=json.loads((out/'status.json').read_text(encoding='utf-8'))
    assert status['state']=='interrupted'
    assert status['stage']=='verify_corpus'
    assert status['error_type']=='OSError'
    assert not (out/'execution/results.json').exists()


def test_persistent_runner_resume_and_direct_ranking(tmp_path,monkeypatch,isolated_omp_version):
    import benchmarks.browsecomp.full_run as runner
    from benchmarks.browsecomp.full_retrieval import run as rank
    root=dataset(tmp_path/'dataset');out=tmp_path/'run'
    args=SimpleNamespace(dataset=root,output=out,allow_fixture=True,tasks=None,model='test/solver',judge_model='test/solver',
        timeout=10,omp='omp',models_config=Path('unused'),resume=False,prepare_only=False)
    attempts=[]
    async def health(args):return {'ok':True}
    async def solve(service,recorder,key,directory,prompt,args,**kwargs):
        attempts.append(prompt)
        result=Retrieval(service.registry).search('benchmark-corpus','kiwi')
        calls=[dict(tool='search',result={'structuredContent':result},is_error=False,duration_ms=1)]
        final=json.dumps(dict(answer='kiwi',completed=True,facts={'final_answer':'kiwi'},sources=['documents/1.md']))
        execution=dict(parse_events(''),valid_run=True,timed_out=False,returncode=0,wall_ms=10,final=final,
            tool_calls=1,tool_errors=0,tools_used={'search':1},agent_tool_attempts=1)
        (directory/'tool-trace.json').write_text(json.dumps(calls),encoding='utf-8')
        return execution,calls
    async def judge(row,task,args,root):return dict(episode=row['episode'],correct=True)
    from benchmarks.agent.runner import parse_events
    monkeypatch.setattr(runner,'check_model',health);monkeypatch.setattr(runner,'solve',solve);monkeypatch.setattr(runner,'judge_one',judge)
    asyncio.run(runner.run(args))
    assert len(attempts)==1
    args.resume=True;asyncio.run(runner.run(args))
    assert len(attempts)==1
    rankargs=SimpleNamespace(dataset=root,run=out,output=tmp_path/'rank',resume=False)
    rank(rankargs)
    ranked=json.loads((rankargs.output/'results.json').read_text(encoding='utf-8'))
    assert ranked['queries'][0]['metrics']['evidence']['1000']['recall']==1
    assert (rankargs.output/'run.trec').is_file()
    # A saved trace mutation must stop resume rather than change its grade silently.
    (out/'execution/episodes/RBCP_1-search-1/tool-trace.json').write_text('[]',encoding='utf-8')
    with pytest.raises(ValueError,match='evidence changed'):asyncio.run(runner.run(args))
