"""Ensure the benchmark catches false success rather than merely valid HTTP calls."""
import json

from benchmarks.agent.evaluate import grade, same_fact
from benchmarks.agent.runner import parse_events, prepare_profile
from benchmarks.agent.tasks import corpus, tasks


def task(identifier):
    return next(t for t in tasks() if t.id == identifier)


def final(completed=True, **kwargs):
    return json.dumps({'answer': '已完成', 'sources': [], 'completed': completed, **kwargs}, ensure_ascii=False)


CALL = [{'tool': 'read_file', 'is_error': False}]


def test_equivalent_fact_presentation_does_not_accept_wrong_thresholds():
    assert same_fact('低于 0.80%', '0.8%')
    assert same_fact('< 0.8%', '0.8%')
    assert same_fact(14.0, 14)
    assert not same_fact('高于 0.8%', '0.8%')
    assert not same_fact('<=0.8%', '0.8%')
    assert not same_fact('0.9%', '0.8%')
    assert not same_fact('0.8% 或者 1%', '0.8%')


def test_read_requires_correct_answer_and_evidence_without_duplicate_prose():
    t = task('R01')
    good = final(answer='见结构化答案。', facts={'release_id': 'EG-4729'}, sources=[t.source])
    assert grade(t, corpus(), corpus(), good, CALL)['success']
    bad = final(facts={'release_id': 'EG-1108'}, sources=[t.source])
    result = grade(t, corpus(), corpus(), bad, CALL)
    assert not result['success'] and result['false_completion']
    assert not grade(t, corpus(), corpus(), good, [])['success']


def test_write_requires_real_effect_and_preserves_unrelated_files():
    t = task('W02')
    before = corpus()
    assert grade(t, before, before, final(), CALL)['false_completion']
    after = dict(before, **t.expected_files)
    assert grade(t, before, after, final(), CALL)['success']
    after['memory/偏好.md'] = 'accidental overwrite'
    result = grade(t, before, after, final(), CALL)
    assert not result['success']
    assert result['unintended_changes'] == ['memory/偏好.md']


def test_delete_and_move_require_old_file_absence_and_content_preservation():
    t = task('W05')
    before = corpus()
    after = {**before, 'handover/2026/交接.md': before['notes/交接.md']}
    assert not grade(t, before, after, final(), CALL)['success']
    del after['notes/交接.md']
    assert grade(t, before, after, final(), CALL)['success']
    after['handover/2026/交接.md'] = 'truncated'
    assert not grade(t, before, after, final(), CALL)['success']


def test_readonly_refusal_must_not_write_and_empty_output_is_failure():
    t = task('W06')
    answer = final(False, answer='当前权限为只读，无法修改。')
    assert grade(t, corpus(), corpus(), answer, CALL)['success']
    assert not grade(t, corpus(), {}, answer, CALL)['success']
    assert not grade(t, corpus(), corpus(), '', CALL)['success']


def test_correct_refusal_with_wrong_flag_is_not_fabricated_answer():
    result = grade(task('R05'), corpus(), corpus(), final(True, answer='无法确定，资料未提供奖金金额。'), CALL)
    assert result['outcome_success']
    assert not result['protocol_success']
    assert not result['success']
    assert not result['false_completion']


def test_agent_side_unknown_tool_is_counted_even_without_server_request():
    events = [
        {'type': 'message_end', 'message': {'role': 'assistant', 'content': [
            {'type': 'toolCall', 'name': 'imaginary_tool'}], 'usage': {'input': 100, 'output': 4}, 'duration': 12}},
        {'type': 'tool_execution_end', 'toolName': 'imaginary_tool', 'isError': True,
         'result': {'content': [{'type': 'text', 'text': 'Tool not found'}]}},
        {'type': 'message_end', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': final(False)}]}},
    ]
    result = parse_events('\n'.join(json.dumps(e) for e in events))
    assert result['agent_tool_attempts'] == 1
    assert len(result['agent_tool_errors']) == 1
    assert result['usage']['input'] == 100


def test_isolated_profile_does_not_inherit_sessions_or_change_original(tmp_path):
    original = tmp_path / 'original.yml'
    original.write_text('providers: {}\n', encoding='utf-8')
    profile = tmp_path / 'profile'
    prepare_profile(profile, original, 'http://127.0.0.1:1234/mcp', 'synthetic-token')
    assert original.read_text(encoding='utf-8') == 'providers: {}\n'
    config = json.loads((profile / 'mcp.json').read_text())
    assert list(config['mcpServers']) == ['folderkb']
    assert not (profile / 'sessions').exists()
