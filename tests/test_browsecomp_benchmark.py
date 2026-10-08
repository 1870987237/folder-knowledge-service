"""Adapter tests use invented records, never publish official questions/answers."""
import base64
import hashlib
import json

import pytest

from benchmarks.agent.evaluate import grade
from benchmarks.agent.tasks import load_dataset
from benchmarks.browsecomp.prepare import CANARY, assemble, decode, write_dataset
from benchmarks.browsecomp.evaluate import export, returned_documents, report


def doc(identifier, text):
    return {'docid': identifier, 'text': text, 'url': 'https://example.org/' + identifier}


def record():
    return {'query_id': '100', 'query': 'Which sample release is current?', 'answer': 'Example-42',
            'evidence_docs': [doc('1', '# Index\nConsult the release note.'), doc('2', '# Release\nExample-42')],
            'gold_docs': [doc('2', '# Release\nExample-42')],
            'negative_docs': [doc('3', '# Other\nAn unrelated manual.'), doc('4', '# Other\nA second manual.')]}


def test_publisher_obfuscation_protocol_preserves_id():
    key = hashlib.sha256(CANARY.encode()).digest()
    text = 'Sample 中文'.encode()
    encrypted = base64.b64encode(bytes(b ^ key[i % 32] for i, b in enumerate(text))).decode()
    assert decode({'query_id': '100', 'query': encrypted}) == {'query_id': '100', 'query': 'Sample 中文'}


def test_subset_is_reproducible_has_complete_evidence_and_hard_negatives():
    first = assemble([record()], 1, 42)
    assert first == assemble([record()], 1, 42)
    docs, public, hidden, _ = first
    assert len(docs) == 3 and {'documents/1.md', 'documents/2.md'} <= docs.keys()
    assert public[0]['prompt'] == record()['query'] and public[0]['write_policy'] == 'readonly'
    assert 'answer' not in public[0]
    assert hidden['RBCP_100']['reference']['answer'] == 'Example-42'
    assert all(not any(label in path for label in ('evidence', 'gold', 'negative')) for path in docs)


def test_import_and_grade_without_leaking_answers(tmp_path):
    write_dataset(tmp_path, [record()], 1, 42, {'repository': 'synthetic-test'})
    documents, definitions, metadata = load_dataset(tmp_path)
    assert metadata['provenance']['official_leaderboard_comparable'] is False
    assert (tmp_path / 'tasks.csv').read_bytes().startswith(b'\xef\xbb\xbf')
    calls = [{'tool': 'search', 'is_error': False, 'result': {'structuredContent': {
        'results': [{'path': 'documents/1.md', 'text': 'Index'}, {'path': 'documents/2.md', 'text': 'Release'}]}}}]
    final = json.dumps({'answer': 'The release is Example-42.', 'facts': {'final_answer': 'Example-42'},
                        'sources': ['documents/2.md'], 'completed': True})
    result = grade(definitions[0], documents, documents, final, calls)
    assert result['success'] and result['answer_exact_match']
    assert result['answer_correct'] is None and result['judge_status'] == 'not_run'
    assert result['evidence_recall'] == 1 and result['gold_recall'] == 1
    assert result['cited_evidence_recall'] == .5
    wrong = final.replace('Example-42', 'Example-420')
    assert not grade(definitions[0], documents, documents, wrong, calls)['success']
    assert not grade(definitions[0], documents, documents, wrong, calls)['false_completion']
    (tmp_path / 'knowledge/documents/2.md').write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='manifest'):
        load_dataset(tmp_path)


def test_metadata_listing_and_errors_do_not_count_as_evidence():
    known = {'documents/1.md': 'a', 'documents/2.md': 'b'}
    calls = [
        {'tool': 'glob', 'result': {'structuredContent': {'files': [{'path': 'documents/1.md'}]}}},
        {'tool': 'read_file', 'is_error': True, 'result': {'structuredContent': {'path': 'documents/1.md', 'lines': ['a']}}},
        {'tool': 'read_file', 'result': {'structuredContent': {'path': 'documents/1.md', 'lines': []}}},
        {'tool': 'search', 'result': {'structuredContent': {'results': [{'path': 'documents/999.md'}]}}},
        {'tool': 'search', 'result': {'structuredContent': {'results': [{'path': 'documents/1.md'}]}}},
        {'tool': 'grep', 'result': {'structuredContent': {'matches': [{'path': 'documents/2.md'}]}}},
        {'tool': 'read_file', 'result': {'structuredContent': {'path': 'documents/2.md', 'lines': ['b']}}},
    ]
    assert returned_documents(calls, known) == {'2'}


def test_report_does_not_label_execution_failure_as_wrong_answer():
    row = {'variant': 'grep', 'task_id': 'RBCP_100', 'valid_run': False, 'timed_out': False,
           'returncode': 1, 'agent_tool_attempts': 0, 'wall_ms': 1000, 'tools_used': {}, 'tool_errors': 0,
           'evaluation': {'answer_exact_match': False, 'evidence_recall': 0}}
    result = report({'model': 'example/model', 'dataset': {}, 'episodes': [row]})
    assert '执行失败，未判分' in result
    assert '未匹配，需语义判分' not in result


@pytest.mark.parametrize('change', ['missing_evidence', 'duplicate_question', 'conflicting_document', 'path_injection', 'no_negatives'])
def test_invalid_source_data_cannot_silently_reduce_difficulty(change):
    row = record()
    rows = [row]
    if change == 'missing_evidence': row['evidence_docs'] = []
    elif change == 'duplicate_question': rows.append(record())
    elif change == 'conflicting_document': row['gold_docs'][0]['text'] = 'conflicting'
    elif change == 'path_injection': row['gold_docs'][0]['docid'] = '../../outside'
    else: row['negative_docs'] = []
    with pytest.raises(ValueError): assemble(rows, 1, 42)


def test_official_export_separates_configurations_and_excludes_references(tmp_path):
    ev = {'benchmark': 'browsecomp-plus', 'query_id': '100', 'answer': 'Candidate answer',
          'prediction': 'Example-42', 'cited_docids': ['2'], 'retrieved_docids': ['1', '2'], 'claimed_completed': True}
    row = {'variant': 'search', 'repeat': 1, 'evaluation': ev, 'valid_run': True,
           'timed_out': False, 'returncode': 0, 'tools_used': {'search': 1}}
    data = {'episodes': [row], 'model': 'example/model', 'dataset': {'provenance': {'scope': 'reduced-corpus-internal-adaptation'}}}
    export(data, tmp_path)
    result = json.loads((tmp_path / 'search-repeat1/100.json').read_text())
    assert result['status'] == 'completed' and result['retrieved_docids'] == ['1', '2']
    assert '[2]' in result['result'][0]['output']
    assert 'reference_answer' not in result and result['metadata']['official_leaderboard_comparable'] is False
    with pytest.raises(ValueError): export(data, tmp_path)
