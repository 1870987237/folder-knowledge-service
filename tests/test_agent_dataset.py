"""Editable datasets must preserve task text and keep oracles out of the library."""
import csv
import json
import shutil

import pytest

from benchmarks.agent.runner import read_tree, seed_knowledge
from benchmarks.agent.tasks import DEFAULT_DATASET, TASK_COLUMNS, corpus, load_dataset, tasks


@pytest.fixture
def dataset(tmp_path):
    target = tmp_path / 'suite'
    shutil.copytree(DEFAULT_DATASET, target)
    return target


def write_tasks(root, rows, encoding='utf-8-sig'):
    with (root / 'tasks.csv').open('w', encoding=encoding, newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=TASK_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def rows(root):
    with (root / 'tasks.csv').open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def test_default_corpus_matches_published_baseline_and_only_mounts_knowledge():
    documents, definitions, metadata = load_dataset()
    assert len(documents) == 24 and len(definitions) == 12
    assert metadata['corpus_sha256'] == '76b93d1f3851a5f2b0bc4facf3c2ce3f416185fecbb875bc3ff5898b00e48519'
    assert set(documents) == {p.relative_to(DEFAULT_DATASET / 'knowledge').as_posix()
                              for p in (DEFAULT_DATASET / 'knowledge').rglob('*.md')}
    assert all(not t.readonly for t in definitions[:-1]) and definitions[-1].readonly
    assert 'facts 对象' in definitions[0].prompt


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig'])
def test_csv_roundtrip_preserves_chinese_quotes_commas_and_newlines(dataset, encoding):
    values = rows(dataset)
    prompt = '检查“中文”，英文逗号, "双引号"\n第二行仍是同一个任务。'
    values[0]['prompt'] = prompt
    write_tasks(dataset, values, encoding)
    assert tasks(dataset)[0].prompt == prompt
    assert len(tasks(dataset)) == 12


@pytest.mark.parametrize('field,value,error', [
    ('id', 'R02', 'duplicate'),
    ('id', '../escape', 'invalid'),
    ('write_policy', 'write', 'write_policy'),
    ('prompt', '', 'missing'),
])
def test_invalid_csv_fails_before_running_agent(dataset, field, value, error):
    values = rows(dataset)
    values[0][field] = value
    write_tasks(dataset, values)
    with pytest.raises(ValueError, match=error):
        load_dataset(dataset)


@pytest.mark.parametrize('change,error', [('missing', 'missing expectations'), ('extra', 'must match'),
                                          ('source', 'source is missing'), ('path', 'relative file path'),
                                          ('type', 'boolean'), ('unknown', 'invalid')])
def test_invalid_oracles_rejected(dataset, change, error):
    path = dataset / 'expectations.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    if change == 'missing':
        del value['R01']
    elif change == 'extra':
        value['R99'] = {'refuse': True}
    elif change == 'source':
        value['R01']['source'] = 'missing.md'
    elif change == 'path':
        value['W01']['contains'] = {'../outside.md': ['text']}
    elif change == 'type':
        value['R05']['refuse'] = 'false'
    else:
        value['R01']['answr_fields'] = {'owner': 'wrong key'}
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match=error):
        load_dataset(dataset)


def test_seeding_replaces_default_readme_and_changes_only_run_copy(dataset, tmp_path):
    source = dataset / 'knowledge'
    (source / 'README.md').unlink()
    documents = corpus(dataset)
    target = tmp_path / 'new-library'
    target.mkdir()
    (target / 'README.md').write_text('# Platform default', encoding='utf-8')
    seed_knowledge(target, documents)
    assert read_tree(target) == documents
    (target / 'memory/偏好.md').write_text('changed by agent', encoding='utf-8')
    assert corpus(dataset) == documents


def test_custom_dataset_is_independent_of_working_directory(dataset, monkeypatch, tmp_path):
    values = rows(dataset)[:1]
    values[0]['id'] = 'R_custom'
    values[0]['prompt'] = '请查负责人。'
    write_tasks(dataset, values)
    (dataset / 'expectations.json').write_text(json.dumps({'R_custom': {'answer_fields': {'owner': '林澈'}}}), encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    _, definitions, metadata = load_dataset(dataset)
    assert definitions[0].id == 'R_custom' and metadata['task_count'] == 1
    assert len(load_dataset()[1]) == 12
