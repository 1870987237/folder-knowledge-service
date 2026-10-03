import os
import time
import hashlib
import json

from folder_kb.capabilities import Capabilities
from folder_kb.release_gc import collect
from tests.test_capabilities import native
from tests.test_collaboration import env, headers


def start(env):
    c, r = env
    _, user, lid, key = native(c, r)
    return c, r, user, lid, key, headers(key['token'])


def apply(c, h, changes, request_id='batch', **kwargs):
    return c.post('/api/v1/files/changes', headers=h,
                  json=dict(changes=changes, request_id=request_id, **kwargs))


def manifest(c, h, **kwargs):
    response = c.get('/api/v1/files/manifest', headers=h, params=kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def test_nested_import_one_copy_one_publication_and_atomic_failure(env, monkeypatch):
    c, r, _, lid, key, h = start(env)
    import folder_kb.file_writes as module
    original = module.copy_workspace
    copies = []
    def copy(*args, **kwargs):
        copies.append(args[1])
        assert kwargs['captured'] is not None
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'copy_workspace', copy)
    before = r.resolve(key['token'], lid)[1]['path']
    changes = [{'operation': 'mkdir', 'path': '文档/指南', 'create_parents': True}]
    changes += [{'operation': 'create', 'path': f'文档/指南/{i}.md', 'content': f'# 内容 {i}'} for i in range(28)]
    body = dict(changes=changes, request_id='import-28')
    response = c.post('/api/v1/files/changes', headers=h, json=body)
    assert response.status_code == 200, response.text
    assert len(copies) == 1 and len(response.json()['results']) == 29
    assert c.post('/api/v1/files/changes', headers=h, json=body).json()['duplicate']
    assert len(copies) == 1
    with r.store.transaction() as db:
        assert db.execute('SELECT count(*) FROM file_write_receipts').fetchone()[0] == 1
    assert (r.pool.root / before / 'README.md').is_file()
    active = r.resolve(key['token'], lid)[1]['path']
    releases = set((r.pool.root / 'document-releases').iterdir())
    response = apply(c, h, [{'operation': 'create', 'path': 'new.md', 'content': 'staged'},
                            {'operation': 'create', 'path': 'README.md', 'content': 'conflict'}], 'fail')
    assert response.status_code == 409 and response.json()['error'] == 'file_exists'
    assert r.resolve(key['token'], lid)[1]['path'] == active
    assert set((r.pool.root / 'document-releases').iterdir()) == releases
    assert not (r.pool.root / active / 'new.md').exists()


def test_manifest_versions_pagination_replace_and_delete(env):
    c, r, _, lid, key, h = start(env)
    changes = [{'operation': 'create', 'path': f'notes/{i}.md', 'content': str(i), 'create_parents': True} for i in range(3)]
    assert apply(c, h, changes).status_code == 200
    first = manifest(c, h, limit=2)
    second = manifest(c, h, offset=first['next_offset'], limit=2, expected_library_version=first['library_version'])
    assert first['library_version'] == second['library_version']
    all_items = manifest(c, h)['entries']
    assert all('content' not in entry for entry in all_items)
    files = {x['path']: x for x in all_items if x['type'] == 'file'}
    read = c.get('/api/v1/read', headers=h, params={'path': 'notes/0.md'}).json()
    assert files['notes/0.md']['version'] == read['version']
    assert len(files['notes/0.md']['sha256']) == 64
    changes = [{'operation': 'replace', 'path': 'notes/0.md', 'content': 'updated'},
               {'operation': 'delete', 'path': 'notes/1.md'}]
    response = apply(c, h, changes, 'replace-delete', expected_library_version=first['library_version'])
    assert response.status_code == 200, response.text
    assert c.get('/api/v1/read', headers=h, params={'path': 'notes/0.md'}).json()['lines'][0]['text'] == 'updated'
    assert c.get('/api/v1/read', headers=h, params={'path': 'notes/1.md'}).status_code == 404
    assert apply(c, h, changes, 'stale', expected_library_version=first['library_version']).json()['error'] == 'version_changed'
    assert c.get('/api/v1/files/manifest', headers=h, params={'expected_library_version': first['library_version']}).status_code == 409
    current = manifest(c, h)
    assert apply(c, h, [{'operation': 'delete', 'path': 'notes'}], 'not-empty', expected_library_version=current['library_version']).json()['error'] == 'directory_not_empty'
    assert apply(c, h, [{'operation': 'delete', 'path': 'notes', 'recursive': True}], 'recursive', expected_library_version=current['library_version']).status_code == 200
    assert not any(x['path'].startswith('notes') for x in manifest(c, h)['entries'])


def test_clear_errors_validation_and_parent_creation(env):
    c, r, _, lid, key, h = start(env)
    body = dict(operation='create', path='missing/a.md', content='hi', request_id='missing')
    response = c.post('/api/v1/write', headers=h, json=body)
    assert response.status_code == 404 and response.json()['error'] == 'parent_not_found'
    assert 'Parent directory does not exist' in response.text
    response = c.post('/api/v1/write', headers=h, json=dict(body, path='README.md/a.md', request_id='parent-file'))
    assert response.status_code == 409 and response.json()['error'] == 'parent_not_directory'
    assert c.post('/api/v1/write', headers=h, json=dict(body, create_parents=True)).status_code == 200
    for i, path in enumerate(('../out.md', '/root.md', '.hidden/a.md', 'a/../b.md', 'a//b.md', 'a\\b.md', 'CON.md', 'a./b.md')):
        response = c.post('/api/v1/write', headers=h, json=dict(body, path=path, request_id=f'invalid-{i}', create_parents=True))
        assert response.status_code == 400 and response.json()['error'] == 'invalid_path', response.text
    assert c.post('/api/v1/write', headers=h, json=dict(body, request_id='中文名称')).status_code == 422
    for changes in ([], [{'operation': 'delete', 'path': 'README.md'}],
                    [{'operation': 'mkdir', 'path': 'x'}, {'operation': 'mkdir', 'path': 'X'}],
                    [{'operation': 'mkdir', 'path': str(i)} for i in range(101)],
                    [{'operation': 'create', 'path': f'{i}.md', 'content': 'a' * 230000} for i in range(5)],
                    [{'operation': 'create', 'path': 'large.md', 'content': '中' * 90000}]):
        assert apply(c, h, changes, 'invalid').status_code == 422
    assert apply(c, h, [{'operation': 'delete', 'path': 'missing'}, {'operation': 'mkdir', 'path': 'missing/nested'}],
                 'overlap', expected_library_version=manifest(c, h)['library_version']).status_code == 422


def test_file_cas_permissions_and_external_change_rollback(env, monkeypatch):
    c, r, user, lid, key, h = start(env)
    data = manifest(c, h)
    version = next(x['version'] for x in data['entries'] if x['path'] == 'README.md')
    changes = [{'operation': 'delete', 'path': 'README.md', 'expected_version': version}]
    response = apply(c, h, changes)
    assert response.status_code == 200
    assert apply(c, h, changes).json()['duplicate']
    assert apply(c, h, changes, 'again').json()['error'] == 'version_changed'
    assert apply(c, h, [{'operation': 'mkdir', 'path': 'x'}]).json()['error'] == 'request_conflict'
    assert c.put(f'/portal/api/libraries/{lid}/agent-settings', headers=headers(user), json={'direct_enabled': False}).status_code == 200
    assert apply(c, h, changes).status_code == 403
    assert 'apply_changes' not in Capabilities(r).discovery(key['token'])[0]
    assert c.put(f'/portal/api/libraries/{lid}/agent-settings', headers=headers(user), json={'direct_enabled': True}).status_code == 200
    import folder_kb.file_writes as module
    original = module.copy_workspace
    active = r.resolve(key['token'], lid)[1]['path']
    releases = set((r.pool.root / 'document-releases').iterdir())
    def changed(source, target, **kwargs):
        original(source, target, **kwargs)
        (source / 'external.md').write_text('External change', encoding='utf-8')
    monkeypatch.setattr(module, 'copy_workspace', changed)
    assert apply(c, h, [{'operation': 'mkdir', 'path': 'x'}], 'external').json()['error'] == 'version_changed'
    assert r.resolve(key['token'], lid)[1]['path'] == active
    assert set((r.pool.root / 'document-releases').iterdir()) == releases


def test_gc_preview_retention_references_and_receipts(env):
    c, r, _, lid, key, h = start(env)
    roots = []
    for i in range(4):
        roots.append(r.resolve(key['token'], lid)[1]['path'])
        assert apply(c, h, [{'operation': 'create', 'path': f'{i}.md', 'content': str(i)}], f'batch-{i}').status_code == 200
    active = r.resolve(key['token'], lid)[1]['path']
    ancient = time.time() - 10 * 86400
    for root in roots + [active]:
        os.utime(r.pool.root / root, (ancient, ancient))
    # References outside the write history pin old snapshots.
    with r.store.transaction(write=True) as db:
        db.execute('INSERT INTO metadata VALUES (?,?)', ('protected_release', roots[1]))
    preview = collect(r, keep=1, min_age_days=7)
    assert roots[1] not in preview['candidates'] and active not in preview['candidates']
    assert roots[2] in preview['candidates'] and not preview['removed']
    assert all((r.pool.root / root).exists() for root in roots)
    result = collect(r, keep=1, min_age_days=7, apply=True)
    assert set(result['removed']) == set(preview['candidates'])
    assert (r.pool.root / active).exists() and (r.pool.root / roots[1]).exists()
    retry = apply(c, h, [{'operation': 'create', 'path': '0.md', 'content': '0'}], 'batch-0')
    assert retry.status_code == 200 and retry.json()['duplicate']
    with r.store.transaction() as db:
        assert db.execute('SELECT count(*) FROM file_write_receipts').fetchone()[0] == 4


def test_gc_preserves_parent_mount_and_recent_releases(env):
    c, r, _, lid, key, h = start(env)
    old = r.resolve(key['token'], lid)[1]['path']
    assert apply(c, h, [{'operation': 'mkdir', 'path': 'empty'}]).status_code == 200
    assert not collect(r, keep=1)['candidates']
    ancient = time.time() - 10 * 86400
    os.utime(r.pool.root / old, (ancient, ancient))
    r.save_library('parent', 'parent', '.', True, create=True)
    assert not collect(r, keep=1, apply=True)['removed']
    assert (r.pool.root / old).exists()


def test_legacy_receipt_compatibility_and_final_workspace_bound(env):
    c, r, _, lid, key, h = start(env)
    old_root = r.resolve(key['token'], lid)[1]['path']
    # A receipt generated before the new optional fields must still be retryable.
    body = dict(path='legacy.md', content='old', operation='create', request_id='legacy')
    digest = hashlib.sha256(json.dumps(dict(body, library_id=lid, expected_version=None),
                                      sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with r.store.transaction(write=True) as db:
        db.execute('INSERT INTO file_write_receipts VALUES (?,?,?,?,?,?,?)',
                   (key['id'], 'legacy', lid, digest, json.dumps({'request_id': 'legacy'}), old_root, old_root))
    assert c.post('/api/v1/write', headers=h, json=body).json()['duplicate']
    # Each operation is small, but the final library must still obey the whole-tree limit.
    (r.pool.root / old_root / 'README.md').write_bytes(b'x' * (32 * 1024 * 1024))
    releases = set((r.pool.root / 'document-releases').iterdir())
    result = apply(c, h, [{'operation': 'create', 'path': 'extra.md', 'content': 'x'}], 'too-large')
    assert result.status_code == 413 and result.json()['error'] == 'workspace_too_large'
    assert r.resolve(key['token'], lid)[1]['path'] == old_root
    assert set((r.pool.root / 'document-releases').iterdir()) == releases
