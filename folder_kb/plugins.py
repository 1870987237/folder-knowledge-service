"""Plugin-facing API over the existing immutable package catalog.

Installation and binding never execute code. Fulltext indexing has a built-in
consumer; semantic/custom indexing and instructions remain declarations.
"""
import anyio
from fastapi import Query, Request
from pydantic import Field

from .errors import KnowledgeError
from .inbox import StrictModel
from . import templates
from .templates import ID_PATTERN, VERSION_PATTERN, TemplateBinding, TemplateRegistration
from .workspaces import WorkspaceSpec


class PluginBinding(StrictModel):
    plugin_id: str = Field(pattern=ID_PATTERN)
    plugin_version: str = Field(pattern=VERSION_PATTERN)
    config: dict = Field(default_factory=dict)
    workspace: WorkspaceSpec | None = None

    def legacy(self):
        return TemplateBinding(template_id=self.plugin_id, template_version=self.plugin_version,
                               config=self.config, workspace=self.workspace)


def describe_plugin(catalog, item):
    definition = item['definition']
    features = definition.get('plugin') or {}
    capabilities = {name: {'integration': 'available'} for name in definition['capabilities']}
    if features.get('indexing'):
        from .retrieval import supported
        capabilities['indexing'] = {'integration': 'available' if supported(features['indexing']) else 'definition_only'}
    if features.get('instructions', '').strip():
        capabilities['instructions'] = {'integration': 'definition_only'}
    with catalog.store.transaction() as db:
        bindings = [dict(row) for row in db.execute(
            'SELECT l.id,l.name FROM library_templates b JOIN libraries l ON l.id=b.library_id '
            'WHERE b.template_id=? AND b.template_version=? ORDER BY l.id',
            (definition['id'], definition['version']))]
    return dict(item, capabilities=capabilities, libraries=bindings, legacy_template=not bool(features))


def add_plugin_routes(app):
    catalog = app.state.templates

    def describe(item):
        return describe_plugin(catalog, item)

    def binding(library_id):
        result = catalog.binding(library_id)
        old = result['binding']
        if old is None:
            return result
        plugin = describe(catalog.get(old['template_id'], old['template_version']))
        return {'library_id': library_id, 'binding': {
            'plugin_id': old['template_id'], 'plugin_version': old['template_version'],
            'config': old['config'], 'workspace': old['workspace'], 'workspace_key': old['workspace_key']},
            'plugin': plugin}

    @app.get('/admin/api/plugins/schema')
    def schema():
        return TemplateRegistration.model_json_schema()

    @app.post('/admin/api/plugins', status_code=201)
    def register(value: TemplateRegistration):
        return describe(catalog.register(value))

    @app.get('/admin/api/plugins')
    def listing(offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)):
        result = catalog.listing(offset, limit)
        return {'plugins': [describe(item) for item in result['templates']], 'next_offset': result['next_offset']}

    @app.get('/admin/api/plugins/{plugin_id}/versions/{version}')
    def detail(plugin_id: str, version: str):
        return describe(catalog.get(plugin_id, version))

    @app.put('/admin/api/plugins/{plugin_id}/versions/{version}/package')
    async def install(request: Request, plugin_id: str, version: str):
        await anyio.to_thread.run_sync(catalog.get, plugin_id, version)
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > templates.MAX_PACKAGE_BYTES:
                raise KnowledgeError('template_package_too_large', 'Package exceeds 32 MiB', 413)
            data.extend(chunk)
        item = await anyio.to_thread.run_sync(catalog.install, plugin_id, version, bytes(data))
        return await anyio.to_thread.run_sync(describe, item)

    @app.get('/admin/api/libraries/{library_id}/plugin')
    def get_binding(library_id: str):
        return binding(library_id)

    @app.put('/admin/api/libraries/{library_id}/plugin')
    def bind(library_id: str, value: PluginBinding):
        catalog.bind(library_id, value.legacy())
        return binding(library_id)

    @app.delete('/admin/api/libraries/{library_id}/plugin')
    def unbind(library_id: str):
        return catalog.unbind(library_id)
