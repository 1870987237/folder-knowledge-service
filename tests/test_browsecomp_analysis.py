"""Only synthetic task data; check benchmark accounting and comparison integrity."""
import copy
import hashlib
import json

import pytest

from benchmarks.agent.preflight import check_model
from benchmarks.browsecomp.analyze import analyze, comparison, execution_status, render, search_diagnostics
from benchmarks.browsecomp.prepare import write_dataset
from benchmarks.agent.tasks import load_dataset
from tests.test_browsecomp_benchmark import record


def call(tool, paths, error=False):
    return {'tool': tool, 'is_error': error, 'duration_ms': 10,
            'arguments': {'query': 'sensitive-query'},
            'result': {'structuredContent': {'results': [{'path': p, 'text': 'body'} for p in paths]}}}


def test_search_gain_accounts_for_prior_tools_and_errors():
    paths = ['documents/1.md', 'documents/2.md']
    read = {'tool': 'read_file', 'duration_ms': 3,
            'result': {'structuredContent': {'path': paths[0], 'lines': ['body']}}}
    calls = [read, call('search', paths), call('search', paths), call('search', paths, True)]
    steps = search_diagnostics(calls, dict.fromkeys(paths), {'1', '2'})
    assert steps[0]['new_documents'] == 1 and steps[0]['new_evidence_documents'] == 1
    assert steps[0]['cumulative_evidence_documents'] == 2
    assert steps[1]['new_documents'] == 0 and steps[1]['repeated_arguments']
    assert steps[2]['error'] and steps[2]['returned_documents'] == 0
    assert 'sensitive-query' not in json.dumps(steps)


@pytest.fixture
def fixture(tmp_path):
    dataset, run = tmp_path / 'dataset', tmp_path / 'run'
    write_dataset(dataset, [record()], 1, 42, {'repository': 'synthetic'})
    _, _, metadata = load_dataset(dataset)
    row = {'episode': 'RBCP_100-search-1', 'task_id': 'RBCP_100', 'variant': 'search', 'repeat': 1,
           'valid_run': True, 'returncode': 0, 'timed_out': False, 'wall_ms': 1000,
           'agent_tool_attempts': 1, 'agent_tool_errors': [],
           'evaluation': {'protocol_success': True, 'answer_exact_match': True, 'gold_recall': 1,
                          'evidence_recall': .5}}
    path = run / 'episodes' / row['episode']
    path.mkdir(parents=True)
    (path / 'tool-trace.json').write_text(json.dumps([call('search', ['documents/2.md'])]))
    data = {'episodes': [row], 'model': 'test/model', 'dataset': metadata,
            'plan': {'planned_episodes': 1, 'timeout_seconds': 180}}
    (run / 'results.json').write_text(json.dumps(data))
    return run, dataset, data


def test_report_distinguishes_unjudged_and_redacts_task_content(fixture):
    run, dataset, _ = fixture
    result = analyze(run, dataset)
    assert result['episodes'][0]['answer_correct'] is None
    text = render(result)
    assert '0/0/1' in text and '未判分' in text
    assert 'sensitive-query' not in text and 'Example-42' not in text
    assert 'Which sample' not in text and 'documents/2.md' not in text


def test_judge_results_are_bound_to_exact_run(fixture, tmp_path):
    run, dataset, _ = fixture
    path = tmp_path / 'judge.json'
    judge = {'run_sha256': 'wrong', 'episodes': [{'episode': 'RBCP_100-search-1', 'correct': True}]}
    path.write_text(json.dumps(judge))
    with pytest.raises(ValueError, match='different run'):
        analyze(run, dataset, path)
    judge['run_sha256'] = hashlib.sha256((run / 'results.json').read_bytes()).hexdigest()
    path.write_text(json.dumps(judge))
    assert analyze(run, dataset, path)['episodes'][0]['answer_correct'] is True


def test_timeout_never_becomes_answer_error(fixture):
    run, dataset, data = fixture
    data['episodes'][0]['timed_out'] = True
    data['plan']['planned_episodes'] = 3
    (run / 'results.json').write_text(json.dumps(data))
    summary = analyze(run, dataset)
    assert summary['missing_episodes'] == 2
    assert summary['episodes'][0]['status'] == 'timeout'
    assert summary['episodes'][0]['answer_correct'] is None


@pytest.mark.parametrize('field,value', [('model', 'other'), ('timeout_seconds', 1),
    ('source_sha256', 'other-code'), ('system_prompt_sha256', 'other-prompt'), ('missing_episodes', 1)])
def test_unfair_comparisons_rejected(fixture, field, value):
    baseline = analyze(*fixture[:2])
    other = copy.deepcopy(baseline)
    other[field] = value
    with pytest.raises(ValueError):
        comparison([baseline, other])


def test_plugin_change_allowed_but_task_matrix_must_match(fixture):
    baseline = analyze(*fixture[:2])
    other = copy.deepcopy(baseline)
    other['plugin_sha256'] = 'different-index'
    assert 'Search' in comparison([baseline, other])
    other['episodes'][0]['repeat'] = 2
    with pytest.raises(ValueError, match='matrix'):
        comparison([baseline, other])


def test_failed_model_configuration_is_redacted(tmp_path):
    import asyncio
    from types import SimpleNamespace
    result = asyncio.run(check_model(SimpleNamespace(model='test', omp='missing', models_config=tmp_path / 'absent')))
    assert not result['ok'] and result['status'] == 'configuration_or_launch_failed'
    assert str(tmp_path) not in json.dumps(result)


def test_program_index_works_with_long_report_directory(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from benchmarks.agent.runner import install_index_plugin, seed_knowledge
    from tools.benchmark_file_tools import Suite
    monkeypatch.setenv('KB_SEARCH_TRUSTED_PYTHON', '0')
    monkeypatch.setenv('KB_SEARCH_PLUGIN_ALLOWLIST', '')
    suite = Suite(tmp_path / ('long-report-name-' * 7), short_fixture=True)
    root = suite.run_root
    client = None
    try:
        args = SimpleNamespace(plugin_definition=Path('examples/plugins/bm25-markdown/definition.json'),
            plugin_source=Path('examples/plugins/bm25-markdown/package'), trust_plugin=True)
        client = install_index_plugin(suite, args)
        lid, key, folder = suite.setup()
        seed_knowledge(folder, {'documents/1.md': '# Orchard\nApple trees grow here.\n'})
        assert client.put(f'/admin/api/libraries/{lid}/plugin', json={
            'plugin_id': 'benchmark-rag', 'plugin_version': args.plugin_version}).status_code == 200
        built = client.post(f'/admin/api/libraries/{lid}/index/build')
        assert built.status_code == 200, built.text
        assert built.json()['engine'] == 'bm25-markdown-v2'
        assert built.json()['files'] == 1
        import os
        assert os.environ['KB_SEARCH_PLUGIN_ALLOWLIST'] == suite.result['plugin']['package_sha256']
    finally:
        if client:
            client.close()
        suite.close()
    assert not root.exists()


def test_preflight_failure_writes_plan_without_starting_service(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from benchmarks.agent import runner
    from benchmarks.agent.tasks import DEFAULT_DATASET
    async def failed(_):
        return {'ok': False, 'status': 'provider_or_agent_failed'}
    monkeypatch.setattr(runner, 'check_model', failed)
    monkeypatch.setattr(runner, 'Suite', lambda *a, **k: pytest.fail('Service must not start'))
    output = tmp_path / 'failed-run'
    args = SimpleNamespace(dataset=DEFAULT_DATASET, tasks=None, output=output,
                           variants=['search'], repeats=1, timeout=180, model='test')
    with pytest.raises(RuntimeError, match='preflight failed'):
        asyncio.run(runner.run(args))
    assert json.loads((output / 'plan.json').read_text())['planned_episodes'] == 12
    assert not (output / 'results.json').exists()


def test_one_command_produces_failure_report_without_invented_scores(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from benchmarks.browsecomp import run as workflow
    async def failed(_):
        raise RuntimeError('preflight failed')
    monkeypatch.setattr(workflow, 'run_agent', failed)
    args = SimpleNamespace(output=tmp_path / 'experiment', omp='omp', model='test', models_config=tmp_path/'model',
        dataset=tmp_path/'dataset', variants=['search'], repeats=1, timeout=1,
        trust_plugin=False, plugin_definition=None, plugin_source=None)
    with pytest.raises(RuntimeError):
        asyncio.run(workflow.run(args))
    assert '没有 Agent 成绩' in (args.output/'report.md').read_text(encoding='utf-8')
    assert not (args.output/'metrics.json').exists()
