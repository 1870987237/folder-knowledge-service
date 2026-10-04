from benchmarks.agent.report import percentile, ratio, render


def test_percentile_interpolates_and_ratios_keep_denominator():
    assert percentile([100, 400, 200, 300], 50) == 250
    assert percentile([100, 200], 95) == 195
    assert ratio(3, 4) == '3/4（75.0%）'
    assert ratio(0, 0) == '—'


def test_host_failure_counts_even_when_server_did_not_receive_call():
    episode = {
        'episode': 'R01-grep-1', 'task_id': 'R01', 'kind': 'lookup', 'variant': 'grep',
        'valid_run': True, 'wall_ms': 5000, 'timed_out': False, 'publications': 0,
        'tool_calls': 1, 'tool_errors': 0, 'agent_tool_attempts': 2, 'agent_tool_results': 2,
        'agent_tool_errors': [{'tool': 'unknown'}], 'repeated_identical_errors': 0,
        'tools_used': {'read_file': 1}, 'usage': {'input': 200, 'output': 50},
        'evaluation': {'success': False, 'error_recovered': False, 'false_completion': True,
                       'unintended_changes': [], 'failed_checks': ['answer_field:release_id']},
    }
    report = render({'episodes': [episode], 'model': 'fake', 'omp_version': 'fake',
                     'benchmark_version': '1', 'started_at': 'test', 'environment': {'commit': 'abcdef'}}, {})
    assert '1/2（50.0%）' in report
    assert '1/1（100.0%）' in report
    assert '0/1（0.0%）' in report
    assert 'answer_field:release_id' in report
