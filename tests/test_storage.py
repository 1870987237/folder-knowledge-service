from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from folder_kb.backup import backup
from folder_kb.core import KnowledgeError
from folder_kb.management import Registry
from folder_kb.server import Settings, create_app


ADMIN = 'sqlite-test-administrator-token-123456789'


@pytest.fixture
def registry(tmp_path):
    root = tmp_path / 'knowledge'
    (root / 'guide').mkdir(parents=True)
    (root / 'guide/README.md').write_text('reference', encoding='utf-8')
    result = Registry(str(root), str(tmp_path / 'state'))
    result.save_library('guide', 'Guide', 'guide', True, create=True)
    return result


def legacy(tmp_path):
    root, state = tmp_path / 'knowledge', tmp_path / 'state'
    (root / 'guide').mkdir(parents=True)
    (root / 'guide/README.md').write_text('legacy content', encoding='utf-8')
    (state / 'audit').mkdir(parents=True)
    tokens = {kind: 'fkb_' + kind + '_legacy-test-secret' for kind in ('active', 'expired', 'revoked')}
    created = '2026-01-01T00:00:00+00:00'
    catalog = {'version': 1, 'libraries': {'guide': {
        'id': 'guide', 'name': 'Legacy guide', 'path': 'guide', 'enabled': True,
        'created_at': created, 'updated_at': created}}, 'keys': {}}
    for kind, token in tokens.items():
        catalog['keys'][kind] = {
            'id': kind, 'agent_name': kind, 'digest': hashlib.sha256(token.encode()).hexdigest(),
            'library_ids': ['guide'], 'created_at': created,
            'expires_at': (datetime.now(timezone.utc)-timedelta(days=1)).isoformat() if kind == 'expired' else None,
            'revoked_at': created if kind == 'revoked' else None}
    (state / 'catalog.json').write_text(json.dumps(catalog), encoding='utf-8')
    (state / 'activity.json').write_text(json.dumps({'since': created, 'keys': {
        'active': {'calls': 9, 'errors': 2, 'total_ms': 15.5, 'last_seen': created}}}), encoding='utf-8')
    for day in range(1, 10):
        event = {'kind': 'access', 'timestamp': f'2026-01-{day:02}T00:00:00+00:00', 'status': 'ok',
                 'key_id': 'active', 'library_id': 'guide', 'tool': 'read_file', 'path': 'README.md'}
        (state / f'audit/2026-01-{day:02}.jsonl').write_text(json.dumps(event)+'\n', encoding='utf-8')
    return root, state, tokens


def test_legacy_import_is_atomic_idempotent_and_preserves_auth(tmp_path):
    root, state, tokens = legacy(tmp_path)
    original = {p: p.read_bytes() for p in state.rglob('*') if p.is_file()}
    r = Registry(str(root), str(state))
    assert r.authenticate(tokens['active'])['library_ids'] == ['guide']
    for kind in ('expired', 'revoked'):
        with pytest.raises(KnowledgeError, match='Invalid'):
            r.authenticate(tokens[kind])
    assert r.snapshot()['metrics']['calls'] == 9
    assert len(r.recent_events()['events']) == 9  # Includes all dates, not just the last seven files.
    assert all(p.read_bytes() == content for p, content in original.items())
    r.run(tokens['active'], 'guide', 'read_file', {'path': 'README.md'}, 'rest')
    reopened = Registry(str(root), str(state))
    assert reopened.snapshot()['metrics']['calls'] == 10
    assert len(reopened.recent_events()['events']) == 10
    # A stale legacy file cannot restore grants after a successful import.
    reopened.update_key_grants('active', [])
    assert Registry(str(root), str(state)).authenticate(tokens['active'])['library_ids'] == []
    with reopened.store.transaction() as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        dump = '\n'.join(db.iterdump())
    assert all(token not in dump for token in tokens.values())


def test_failed_import_leaves_originals_and_can_retry(tmp_path):
    root, state, _ = legacy(tmp_path)
    broken = state / 'audit/2026-01-09.jsonl'
    valid = broken.read_bytes()
    broken.write_bytes(valid + b'{broken json\n')
    with pytest.raises(ValueError, match='migration rolled back'):
        Registry(str(root), str(state))
    assert broken.read_bytes() == valid + b'{broken json\n'
    with sqlite3.connect(state / 'management.sqlite3') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    broken.write_bytes(valid)
    assert len(Registry(str(root), str(state)).recent_events()['events']) == 9


def test_concurrent_startup_imports_only_once(tmp_path):
    root, state, _ = legacy(tmp_path)
    def start(_):
        return Registry(str(root), str(state))
    with ThreadPoolExecutor(max_workers=4) as pool:
        instances = list(pool.map(start, range(4)))
    assert all(len(r.recent_events()['events']) == 9 for r in instances)


def test_foreign_keys_and_busy_errors(registry):
    with pytest.raises(KnowledgeError) as invalid:
        with registry.store.transaction(write=True) as db:
            db.execute('INSERT INTO key_libraries VALUES (?,?)', ('unknown-key', 'guide'))
    assert invalid.value.code == 'state_unavailable'
    # An external writer must result in a controlled 503, without partial changes.
    with registry.store.transaction(write=True):
        with pytest.raises(KnowledgeError) as busy:
            registry.create_key('blocked', ['guide'], None)
    assert busy.value.status == 503 and busy.value.code == 'state_busy'
    assert registry.snapshot()['keys'] == []


def test_mutation_and_audit_rollback_together(registry, monkeypatch):
    key = registry.create_key('worker', ['guide'], None)
    def fail(*args):
        raise RuntimeError('injected audit failure')
    with monkeypatch.context() as patch:
        patch.setattr(registry.store, 'event', fail)
        with pytest.raises(RuntimeError):
            registry.update_key_grants(key['key']['id'], [])
        with pytest.raises(RuntimeError):
            registry.create_key('must-roll-back', ['guide'], None)
        with pytest.raises(RuntimeError):
            registry.run(key['token'], None, 'read_file', {'path': 'README.md'}, 'rest')
    assert registry.authenticate(key['token'])['library_ids'] == ['guide']
    assert len(registry.snapshot()['keys']) == 1
    assert registry.snapshot()['metrics']['calls'] == 0
    assert not registry.snapshot()['active_requests']


def test_concurrent_instances_count_same_key_without_lost_updates(registry):
    key = registry.create_key('worker', ['guide'], None)
    instances = [Registry(str(registry.pool.root), str(registry.directory)) for _ in range(4)]
    def use(i):
        return instances[i % 4].run(key['token'], None, 'read_file', {'path': 'README.md'}, 'rest')
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(list(pool.map(use, range(40)))) == 40
    snapshot = registry.snapshot()
    assert snapshot['metrics']['calls'] == 40
    assert len([e for e in registry.recent_events()['events'] if e['kind'] == 'access']) == 40


def test_history_filters_and_cursor_survive_new_events(registry):
    with registry.store.transaction(write=True) as db:
        for day in range(1, 6):
            registry.store.event(db, {'kind': 'access', 'timestamp': f'2026-01-0{day}T00:00:00Z',
                                     'key_id': 'historic', 'library_id': 'guide',
                                     'status': 'error' if day % 2 else 'ok', 'tool': 'read_file'})
    app = create_app(Settings(str(registry.pool.root), admin_token=ADMIN,
                              state_dir=str(registry.directory), allowed_hosts=('testserver',)))
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer '+ADMIN}
        params = {'limit':2, 'key_id':'historic', 'library_id':'guide'}
        first = client.get('/admin/api/activity', headers=headers, params=params).json()
        assert len(first['events']) == 2 and first['next_before_id']
        with registry.store.transaction(write=True) as db:
            registry.store.event(db, {'kind': 'access', 'key_id':'historic', 'status':'ok', 'library_id':'guide'})
        second = client.get('/admin/api/activity', headers=headers,
                            params=dict(params, before_id=first['next_before_id'])).json()
        assert len(second['events']) == 2
        assert not {e['event_id'] for e in first['events']} & {e['event_id'] for e in second['events']}
        filtered = client.get('/admin/api/activity', headers=headers, params={
            'status':'error', 'start_time':'2026-01-02T00:00:00Z', 'end_time':'2026-01-04T00:00:00Z'}).json()
        assert len(filtered['events']) == 1 and filtered['events'][0]['timestamp'].startswith('2026-01-03')
        for invalid in ({'status':'oops'}, {'before_id':0}, {'before_id':99999}, {'start_time':'yesterday'},
                        {'start_time':'2026-01-01T00:00:00'}):
            assert client.get('/admin/api/activity', headers=headers, params=invalid).status_code == 400


def test_online_backup_restores_committed_wal_and_does_not_overwrite(registry, tmp_path):
    key = registry.create_key('worker', ['guide'], None)
    # Keep another connection open so backup must include committed WAL pages.
    with registry.store.connection():
        registry.run(key['token'], None, 'read_file', {'path': 'README.md'}, 'rest')
        target = backup(registry.directory, tmp_path / 'restored/management.sqlite3')
    restored = Registry(str(registry.pool.root), str(target.parent))
    assert restored.authenticate(key['token'])['id'] == key['key']['id']
    assert restored.snapshot()['metrics']['calls'] == 1
    with pytest.raises(FileExistsError):
        backup(registry.directory, target)
    with pytest.raises(FileExistsError):
        backup(registry.directory, registry.store.path)


def test_unknown_database_version_is_not_overwritten(registry):
    with registry.store.transaction(write=True) as db:
        db.execute('PRAGMA user_version=99')
    with pytest.raises(ValueError, match='Unsupported'):
        Registry(str(registry.pool.root), str(registry.directory))
    with registry.store.transaction() as db:
        assert db.execute('SELECT count(*) FROM libraries').fetchone()[0] == 1
