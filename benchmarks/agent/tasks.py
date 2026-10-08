"""Load editable dataset files; only knowledge/ is exposed through the platform."""
import csv
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path, PurePosixPath
import re


DEFAULT_DATASET = Path(__file__).resolve().parents[1] / 'datasets' / 'basic'
TASK_COLUMNS = ('id', 'kind', 'prompt', 'write_policy')


@dataclass
class Task:
    id: str
    kind: str
    prompt: str
    facts: tuple[str, ...] = ()
    source: str | None = None
    expected_files: dict[str, str | None] = field(default_factory=dict)
    contains: dict[str, tuple[str, ...]] = field(default_factory=dict)
    absent: dict[str, tuple[str, ...]] = field(default_factory=dict)
    refuse: bool = False
    readonly: bool = False
    answer_fields: dict = field(default_factory=dict)
    reference: dict | None = None


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _relative_path(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
            or PurePosixPath(value).is_absolute() or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError(f'Expected a relative file path using /: {value!r}')
    return value


def _is_link(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def corpus(dataset=DEFAULT_DATASET):
    root = Path(dataset).resolve() / 'knowledge'
    if not root.is_dir() or _is_link(root):
        raise ValueError(f'Knowledge must be a regular directory: {root}')
    files = {}
    for path in sorted(root.rglob('*')):
        if _is_link(path) or not path.resolve().is_relative_to(root):
            raise ValueError(f'Knowledge links are not supported: {path}')
        if path.is_file():
            name = _relative_path(path.relative_to(root).as_posix())
            files[name] = path.read_text(encoding='utf-8')
    if not files:
        raise ValueError(f'Knowledge directory has no UTF-8 text files: {root}')
    return files


def _oracle(identifier, value):
    allowed = {'facts', 'source', 'expected_files', 'contains', 'absent', 'refuse', 'answer_fields', 'reference'}
    if not isinstance(value, dict) or not value or value.keys() - allowed:
        raise ValueError(f'{identifier}: invalid or empty expectations')
    result = dict(value)
    if 'reference' in result:
        ref = result['reference']
        if (not isinstance(ref, dict) or set(ref) != {'benchmark', 'query_id', 'answer', 'evidence_docids', 'gold_docids'}
                or ref.get('benchmark') != 'browsecomp-plus'
                or not isinstance(ref.get('query_id'), str) or not re.fullmatch(r'[0-9]+', ref['query_id'])
                or not isinstance(ref.get('answer'), str) or not ref['answer'].strip()):
            raise ValueError(f'{identifier}: invalid external benchmark reference')
        for key in ('evidence_docids', 'gold_docids'):
            if not isinstance(ref[key], list) or not ref[key] or not all(isinstance(v, str) and re.fullmatch(r'[0-9]+', v) for v in ref[key]):
                raise ValueError(f'{identifier}: invalid {key}')
    if 'facts' in result:
        if not isinstance(result['facts'], list) or not all(isinstance(x, str) for x in result['facts']):
            raise ValueError(f'{identifier}: facts must be a list of strings')
        result['facts'] = tuple(result['facts'])
    if result.get('source') is not None:
        _relative_path(result['source'])
    for key in ('expected_files', 'contains', 'absent'):
        if key not in result:
            continue
        if not isinstance(result[key], dict):
            raise ValueError(f'{identifier}: {key} must be an object keyed by file path')
        converted = {}
        for path, expected in result[key].items():
            _relative_path(path)
            if key == 'expected_files':
                if expected is not None and not isinstance(expected, str):
                    raise ValueError(f'{identifier}: expected_files values must be text or null')
                converted[path] = expected
            else:
                if not isinstance(expected, list) or not expected or not all(isinstance(x, str) and x for x in expected):
                    raise ValueError(f'{identifier}: {key} values must be nonempty lists of phrases')
                converted[path] = tuple(expected)
        result[key] = converted
    if 'refuse' in result and not isinstance(result['refuse'], bool):
        raise ValueError(f'{identifier}: refuse must be a boolean')
    if 'answer_fields' in result and not isinstance(result['answer_fields'], dict):
        raise ValueError(f'{identifier}: answer_fields must be an object')
    if not any(result.get(k) for k in allowed):
        raise ValueError(f'{identifier}: expectations must contain an outcome check')
    return result


def tasks(dataset=DEFAULT_DATASET):
    root = Path(dataset)
    expectations = json.loads((root / 'expectations.json').read_text(encoding='utf-8-sig'))
    if not isinstance(expectations, dict):
        raise ValueError('expectations.json must be an object keyed by task ID')
    result, seen = [], set()
    with (root / 'tasks.csv').open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, strict=True)
        if not reader.fieldnames or len(reader.fieldnames) != len(TASK_COLUMNS) or set(reader.fieldnames) != set(TASK_COLUMNS):
            raise ValueError('tasks.csv columns must be: ' + ','.join(TASK_COLUMNS))
        for row in reader:
            label = f'tasks.csv line {reader.line_num}'
            if None in row or any(v is None or not v.strip() for v in row.values()):
                raise ValueError(f'{label}: missing or extra fields')
            identifier = row['id']
            # R/W also controls evidence requirements in the existing grader.
            if not re.fullmatch(r'[RW][A-Za-z0-9_-]+', identifier) or identifier in seen:
                raise ValueError(f'{label}: invalid or duplicate task ID {identifier!r}; use R... or W...')
            if row['write_policy'] not in ('direct', 'readonly'):
                raise ValueError(f'{label}: write_policy must be direct or readonly')
            if identifier not in expectations:
                raise ValueError(f'{identifier}: missing expectations')
            seen.add(identifier)
            result.append(Task(identifier, row['kind'], row['prompt'],
                               readonly=row['write_policy'] == 'readonly',
                               **_oracle(identifier, expectations[identifier])))
    if not result or expectations.keys() - seen:
        raise ValueError('Task IDs in tasks.csv and expectations.json must match and not be empty')
    return result


def load_dataset(dataset=DEFAULT_DATASET):
    """Freeze input content once per run, before starting the service or model."""
    documents, definitions = corpus(dataset), tasks(dataset)
    for task in definitions:
        if task.source and task.source not in documents:
            raise ValueError(f'{task.id}: citation source is missing from knowledge/: {task.source}')
        if task.reference:
            if not task.readonly:
                raise ValueError('BrowseComp-Plus tasks must be read-only')
            for did in set(task.reference['evidence_docids'] + task.reference['gold_docids']):
                if f'documents/{did}.md' not in documents:
                    raise ValueError(f'{task.id}: required evidence/gold document {did} is missing')
    public = [{k: v for k, v in asdict(t).items() if k in ('id', 'kind', 'prompt', 'readonly')} for t in definitions]
    hidden = [{k: v for k, v in asdict(t).items() if k not in ('kind', 'prompt', 'readonly')} for t in definitions]
    metadata = {'name': Path(dataset).resolve().name, 'corpus_files': len(documents), 'task_count': len(definitions),
                'corpus_sha256': fingerprint(documents), 'tasks_sha256': fingerprint(public),
                'expectations_sha256': fingerprint(hidden)}
    manifest = Path(dataset) / 'manifest.json'
    if manifest.is_file():
        value = json.loads(manifest.read_text(encoding='utf-8'))
        if value.get('corpus_sha256') != metadata['corpus_sha256']:
            raise ValueError('Dataset differs from its import manifest')
        metadata['provenance'] = value
    return documents, definitions, metadata
