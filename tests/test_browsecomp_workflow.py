from contextlib import contextmanager
import io
import json
from types import SimpleNamespace
import sys
import time

import pytest

from benchmarks.browsecomp import workflow
from benchmarks.browsecomp.full_prepare import atomic_json
from folder_kb.errors import KnowledgeError
from folder_kb.search_large import source_documents


def test_workflow_child_failure_is_recorded_and_logs_are_retained(tmp_path):
    worker=workflow.Workflow(tmp_path,poll_seconds=.01)
    with pytest.raises(RuntimeError,match='exited with status 7'):
        worker.execute('prepare',[sys.executable,'-c',"import sys; print('failure detail',file=sys.stderr); sys.exit(7)"])
    result=workflow.read_status(tmp_path/'workflow.json')
    assert result['state']=='interrupted' and result['stage']=='prepare'
    assert result['returncode']==7 and result['child_pid'] is None
    assert 'failure detail' in (tmp_path/'prepare.stderr.log').read_text()


def test_workflow_heartbeat_changes_during_child_and_stale_status_is_not_running(tmp_path):
    worker=workflow.Workflow(tmp_path,poll_seconds=.01)
    worker.execute('probe',[sys.executable,'-c','import time; time.sleep(.1)'])
    value=json.loads((tmp_path/'workflow.json').read_text())
    assert value['returncode']==0 and value['worker_pid']>0
    value['updated_at']=time.time()-60
    atomic_json(tmp_path/'workflow.json',value)
    assert workflow.read_status(tmp_path/'workflow.json')['effective_state']=='interrupted'
    value['state']='completed';atomic_json(tmp_path/'workflow.json',value)
    assert workflow.read_status(tmp_path/'workflow.json')['effective_state']=='completed'


def test_workflow_stops_before_later_phases_on_failure(tmp_path,monkeypatch):
    args=SimpleNamespace(output=tmp_path/'run',dataset=tmp_path/'dataset',allow_fixture=True,
        model='test/model',judge_model='test/model',timeout=10,omp='omp',models_config=tmp_path/'models.yml',resume=False)
    monkeypatch.setattr(workflow,'load_dataset',lambda _:({'a':'b'},['task'],{'name':'fixture'}))
    monkeypatch.setattr(workflow,'source_hash',lambda:'fixed')
    stages=[]
    def execute(self,stage,command,progress_file=None):
        stages.append(stage)
        if stage=='mcp_check':raise RuntimeError('MCP unavailable')
    monkeypatch.setattr(workflow.Workflow,'execute',execute)
    with pytest.raises(RuntimeError,match='MCP unavailable'):workflow.run(args)
    assert stages==['preflight','prepare','mcp_check']
    value=json.loads((args.output/'workflow.json').read_text())
    assert value['state']=='interrupted'
    args.resume=True
    args.model='different/model'
    with pytest.raises(ValueError,match='unchanged'):workflow.run(args)


def test_bounded_source_reads_keep_document_order_and_propagate_source_changes():
    class Folder:
        @contextmanager
        def _open_text(self,name):
            if name=='0.md':time.sleep(.05)
            yield io.BytesIO(name.encode()),None,'version'
    files=[(f'{i}.md','version',4) for i in range(20)]
    assert [d['path'] for d in source_documents(Folder(),files)]==[n for n,_,_ in files]
    with pytest.raises(KnowledgeError) as exc:
        list(source_documents(Folder(),[('changed.md','old-version',4)]))
    assert exc.value.code=='index_source_changed'
