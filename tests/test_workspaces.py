import json
from pathlib import Path
import shutil

from fastapi.testclient import TestClient
import pytest

from folder_kb.errors import KnowledgeError
from folder_kb.management import Registry
from folder_kb.server import Settings, create_app
from folder_kb.template_package import build_package
from folder_kb.template_protocol import (JobRequest, Record, TemplateRef, prepare_job,
                                         run_reference, validate_result, write_json)
from folder_kb.templates import TemplateBinding, TemplateRegistration
from folder_kb.workspaces import WorkspaceSpec, workspace_digest
import hashlib


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / 'root'
    workspace = root / 'repo'
    for folder in ('knowledge', 'other-view', 'metadata', 'sources', '.cache'):
        (workspace / folder).mkdir(parents=True)
    (workspace / 'knowledge/INDEX.md').write_text('Public guide', encoding='utf-8')
    (workspace / 'other-view/INDEX.md').write_text('Other guide', encoding='utf-8')
    (workspace / 'metadata/state.json').write_text('{"source":"PRIVATE AUX DATA"}', encoding='utf-8')
    (workspace / 'sources/original.txt').write_text('Original evidence', encoding='utf-8')
    (workspace / '.env').write_text('KEY=must-not-enter-input', encoding='utf-8')
    (workspace / '.cache/cache.txt').write_text('cache', encoding='utf-8')
    app = create_app(Settings(str(root), admin_token='workspace-test-only-administrator-token',
                              state_dir=str(tmp_path / 'state'), allowed_hosts=('testserver',)))
    registry = app.state.registry
    registry.save_library('alpha', 'Alpha', 'repo/knowledge', True, create=True)
    registry.save_library('beta', 'Beta', 'repo/other-view', True, create=True)
    examples = Path(__file__).resolve().parents[1] / 'examples'
    archive = build_package(examples / 'templates/reference-docs')
    definition = json.loads((examples / 'template-platform-config.json').read_text(encoding='utf-8'))
    definition['source'] = {'type': 'uploaded-zip', 'archive_sha256': hashlib.sha256(archive).hexdigest()}
    app.state.templates.register(TemplateRegistration.model_validate(definition))
    result = app.state.templates.install(definition['id'], definition['version'], archive)
    template = TemplateRef(id=definition['id'], version=definition['version'], package_sha256=result['package_sha256'])
    return app, workspace, template


def binding(template, view='knowledge', **changes):
    workspace = {'type': 'local', 'root_path': 'repo', 'knowledge_subpath': view}
    workspace.update(changes)
    return TemplateBinding(template_id=template.id, template_version=template.version, workspace=workspace)


def job_for(app, template, directory):
    scope = app.state.templates.snapshot_source('alpha')
    request = JobRequest(job_id='workspace-job', operation='update', library_id='alpha',
                         base_version=workspace_digest(scope['root'], scope['spec'].exclude_paths), template=template,
                         knowledge_subpath=scope['spec'].knowledge_subpath, workspace_key=scope['workspace_key'],
                         source_version=scope['source_version'], exclude_paths=scope['spec'].exclude_paths)
    record = Record(id='record', material={'submission_id': 'one', 'library_id': 'alpha', 'content': {'text': 'New content'}})
    prepare_job(directory, request, [record], scope['root'])
    package = app.state.registry.directory / 'templates' / template.id / template.version / 'package'
    run_reference(directory, request, [record], package)
    return request, record


def test_full_workspace_and_private_view(setup, tmp_path):
    app, original, template = setup
    app.state.templates.bind('alpha', binding(template))
    key = app.state.registry.create_key('reader', ['alpha'], None)
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer ' + key['token']}
        assert client.get('/api/v1/grep', headers=headers, params={'query': 'PRIVATE AUX DATA'}).json()['matches'] == []
        assert client.get('/api/v1/read', headers=headers, params={'path': '../metadata/state.json'}).status_code == 403
    job = tmp_path / 'job'
    request, record = job_for(app, template, job)
    for prefix in ('input', 'output'):
        assert (job / prefix / 'workspace/metadata/state.json').read_bytes() == (original / 'metadata/state.json').read_bytes()
        assert (job / prefix / 'workspace/sources/original.txt').is_file()
        assert not (job / prefix / 'workspace/.env').exists()
        assert not (job / prefix / 'workspace/.cache').exists()
    assert (original / '.env').is_file()
    result = validate_result(job, request, [record])
    assert result.materials[0].references[0].startswith('knowledge/')
    assert result.candidate_path == 'workspace'


def test_auxiliary_deletion_must_be_explicit(setup, tmp_path):
    app, _, template = setup
    app.state.templates.bind('alpha', binding(template))
    job = tmp_path / 'job'
    request, record = job_for(app, template, job)
    (job / 'output/workspace/metadata/state.json').unlink()
    with pytest.raises(ValueError, match='without declaring deletion'):
        validate_result(job, request, [record])
    result_file = job / 'output/result.json'
    result = json.loads(result_file.read_text(encoding='utf-8'))
    result['deleted_paths'] = ['metadata/state.json']
    write_json(result_file, result)
    validate_result(job, request, [record])
    (job / 'output/workspace/.env').write_text('should be refused')
    with pytest.raises(ValueError, match='excluded'):
        validate_result(job, request, [record])


def test_no_implicit_parent_and_shared_workspace(setup):
    app, workspace, template = setup
    catalog = app.state.templates
    catalog.bind('alpha', TemplateBinding(template_id=template.id, template_version=template.version))
    assert catalog.snapshot_source('alpha')['root'] == workspace / 'knowledge'
    first = catalog.bind('alpha', binding(template))
    second = catalog.bind('beta', binding(template, 'other-view'))
    assert first['binding']['workspace_key'] == second['binding']['workspace_key']
    with pytest.raises(KnowledgeError, match='same template'):
        catalog.bind('beta', binding(template, 'other-view', exclude_paths=['sources']))
    with pytest.raises(KnowledgeError, match='match the library mount'):
        catalog.bind('alpha', binding(template, 'other-view'))
    app.state.registry.save_library('alpha', 'Alpha', 'repo/other-view', True)
    with pytest.raises(KnowledgeError, match='match the library mount'):
        catalog.snapshot_source('alpha')


@pytest.mark.parametrize('changes', [
    {'root_path': '../outside'}, {'knowledge_subpath': '../other'},
    {'exclude_paths': ['knowledge']}, {'knowledge_subpath': '.env'},
])
def test_invalid_scope(changes):
    with pytest.raises(ValueError):
        WorkspaceSpec.model_validate({'type': 'local', 'root_path': 'repo',
                                      'knowledge_subpath': 'knowledge', **changes})


def test_git_source_pinned_and_stable_identity(setup, tmp_path):
    app, workspace, template = setup
    registry, catalog = app.state.registry, app.state.templates
    with registry.store.transaction(write=True) as db:
        db.execute("""INSERT INTO repositories
            (id,name,url,branch,credential,interval_seconds,enabled,created_at,current_commit,release_path)
            VALUES ('repo1','repo1','https://example.com/repo.git','main','',0,1,'now',?,'repo')""", ('a' * 40,))
        db.execute("INSERT INTO repository_libraries VALUES ('alpha','repo1','knowledge')")
    bind = TemplateBinding(template_id=template.id, template_version=template.version,
                           workspace={'type': 'git', 'repository_id': 'repo1', 'knowledge_subpath': 'knowledge'})
    catalog.bind('alpha', bind)
    old = catalog.snapshot_source('alpha')
    assert old['source_version'] == 'a' * 40 and old['root'] == workspace
    next_root = workspace.parent / 'next'
    shutil.copytree(workspace, next_root)
    (next_root / 'metadata/state.json').write_text('new version')
    with registry.store.transaction(write=True) as db:
        db.execute("UPDATE repositories SET current_commit=?,release_path='next' WHERE id='repo1'", ('b' * 40,))
        db.execute("UPDATE libraries SET path='next/knowledge' WHERE id='alpha'")
    new = catalog.snapshot_source('alpha')
    assert old['workspace_key'] == new['workspace_key']
    assert new['source_version'] == 'b' * 40 and new['root'] == next_root
    assert old['root'].joinpath('metadata/state.json').read_text() == '{"source":"PRIVATE AUX DATA"}'
    with pytest.raises(KnowledgeError, match='Git-managed'):
        catalog.bind('alpha', binding(template, root_path='next'))


def test_v4_binding_migrates_without_expansion(setup):
    app, workspace, template = setup
    registry = app.state.registry
    app.state.templates.bind('alpha', binding(template))
    with registry.store.transaction(write=True) as db:
        db.execute('ALTER TABLE library_templates DROP COLUMN workspace_json')
        db.execute('ALTER TABLE library_templates DROP COLUMN workspace_key')
        db.execute('PRAGMA user_version=4')
    reopened = Registry(str(registry.pool.root), str(registry.directory))
    from folder_kb.templates import TemplateCatalog
    catalog = TemplateCatalog(reopened)
    assert catalog.binding('alpha')['binding']['workspace'] is None
    assert catalog.snapshot_source('alpha')['root'] == workspace / 'knowledge'
