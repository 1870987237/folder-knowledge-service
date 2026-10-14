"""Admin-only template registration, bounded ZIP installation and library binding."""
from __future__ import annotations

import hashlib
import io
import json
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
import shutil
import stat
import uuid
import zipfile
import zlib
from typing import Literal

import anyio
from fastapi import Query, Request
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import Field, field_validator, model_validator

from .core import is_link
from .errors import KnowledgeError
from .inbox import StrictModel
from .storage import now
from .template_protocol import tree_digest, write_json
from .workspaces import WorkspaceSpec, resolve_workspace

MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 2000
ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


def relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (not value or len(value) > 240 or path.is_absolute() or
            any(part in ('', '.', '..') for part in value.split('/')) or
            any(char in value for char in '\\:<>"|?*') or any(ord(char) < 32 for char in value) or
            any(part.endswith((' ', '.')) for part in path.parts)):
        raise ValueError("Expected a portable relative path")
    for part in path.parts:
        if part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
            raise ValueError("Reserved path name")
    return path


def validate_config_schema(schema: dict):
    # No resolver is allowed to retrieve remote schemas.
    def walk(node, depth=0):
        if depth > 24:
            raise ValueError("Configuration schema is too deep")
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ('$ref', '$dynamicRef') and (not isinstance(value, str) or not value.startswith('#')):
                    raise ValueError("Only local schema references are supported")
                if key == '$id':
                    raise ValueError("Schema IDs are not supported; use local references")
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)
    if schema.get('type') != 'object':
        raise ValueError("Configuration schema must describe an object")
    if schema.get('$schema', 'https://json-schema.org/draft/2020-12/schema') != 'https://json-schema.org/draft/2020-12/schema':
        raise ValueError("Use JSON Schema draft 2020-12")
    if len(json.dumps(schema).encode()) > 32768:
        raise ValueError("Configuration schema exceeds 32 KiB")
    walk(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ValueError("Invalid JSON Schema") from None


class PackageSource(StrictModel):
    type: Literal['uploaded-zip'] = 'uploaded-zip'
    archive_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class Runtime(StrictModel):
    type: Literal['container', 'trusted-python-demo', 'declarative']
    image: str | None = Field(default=None, max_length=256)
    timeout_seconds: int = Field(default=900, ge=1, le=3600)
    memory_mb: int = Field(default=512, ge=64, le=8192)
    cpus: float = Field(default=1, gt=0, le=8)

    @model_validator(mode='after')
    def image_required(self):
        import re
        if self.type == 'container' and (not self.image or not re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', self.image)):
            raise ValueError("Container runtime requires an image pinned by digest")
        return self


class OutputSpec(StrictModel):
    directory: Literal['knowledge', 'workspace'] = 'knowledge'
    required_files: list[str] = Field(default_factory=lambda: ['INDEX.md'], max_length=32)

    @field_validator('required_files')
    @classmethod
    def check_files(cls, values):
        for value in values:
            relative_path(value)
        return values


class IndexDefinition(StrictModel):
    mode: Literal['fulltext', 'semantic', 'hybrid'] = 'hybrid'
    splitter: Literal['document', 'markdown_sections', 'custom'] = 'markdown_sections'
    include: list[str] = Field(default_factory=lambda: ['**/*.md', '**/*.txt'], min_length=1, max_length=32)
    exclude: list[str] = Field(default_factory=list, max_length=32)
    metadata_fields: list[str] = Field(default_factory=list, max_length=32)
    entrypoint: list[str] | None = None
    search_protocol: Literal['1.0', '1.1'] | None = None
    supports_path_filter: bool = False

    @model_validator(mode='after')
    def contract(self):
        for pattern in self.include + self.exclude:
            if (not pattern or len(pattern) > 240 or PurePosixPath(pattern).is_absolute()
                    or any(p in ('', '.', '..') for p in pattern.split('/'))
                    or any(c in pattern for c in '\\:\x00') or any(ord(c) < 32 for c in pattern)):
                raise ValueError('Index patterns must stay relative to the knowledge directory')
        if (self.splitter == 'custom') != (self.entrypoint is not None):
            raise ValueError('Only custom indexing requires an entrypoint')
        if self.search_protocol and not self.entrypoint:
            raise ValueError('Search protocol requires a plugin entrypoint')
        if any(not name.isidentifier() or len(name) > 64 for name in self.metadata_fields):
            raise ValueError('Invalid metadata field name')
        return self


class PluginFeatures(StrictModel):
    manifest_version: Literal['1.0'] = '1.0'
    instructions: str = Field(default='', max_length=8000)
    indexing: IndexDefinition | None = None


class TemplateRegistration(StrictModel):
    id: str = Field(pattern=ID_PATTERN)
    version: str = Field(pattern=VERSION_PATTERN)
    protocol_version: Literal['1.0', '1.1'] = '1.0'
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=2000)
    maintainer: str = Field(min_length=1, max_length=100)
    source: PackageSource
    capabilities: list[Literal['initialize', 'update']] = Field(max_length=2)
    entrypoints: dict[str, list[str]]
    runtime: Runtime
    config_schema: dict = Field(default_factory=lambda: {'type': 'object', 'additionalProperties': False})
    required_secrets: list[str] = Field(default_factory=list, max_length=16)
    output: OutputSpec = Field(default_factory=OutputSpec)
    plugin: PluginFeatures | None = None

    @model_validator(mode='after')
    def contract(self):
        relative_path(self.id)
        relative_path(self.version)
        if self.output.directory != ('workspace' if self.protocol_version == '1.1' else 'knowledge'):
            raise ValueError('Output directory must match the protocol version')
        if len(set(self.capabilities)) != len(self.capabilities) or set(self.entrypoints) != set(self.capabilities):
            raise ValueError("Capabilities and entrypoints must match exactly")
        if not self.capabilities and not (self.plugin and (self.plugin.instructions.strip() or self.plugin.indexing)):
            raise ValueError('Declare at least one plugin capability')
        commands = list(self.entrypoints.values())
        if self.plugin and self.plugin.indexing and self.plugin.indexing.entrypoint is not None:
            commands.append(self.plugin.indexing.entrypoint)
        if commands and self.runtime.type == 'declarative':
            raise ValueError('Executable capabilities require a program runtime')
        for command in commands:
            # v1 Python package convention; no shell expressions or arbitrary interpreter path.
            if len(command) < 2 or len(command) > 16 or command[0] != 'python':
                raise ValueError("Entrypoints must be argv arrays beginning with python and a package-relative script")
            relative_path(command[1])
            if not command[1].endswith('.py'):
                raise ValueError("The entrypoint must reference a Python script")
            if any(not arg or len(arg) > 256 or '\x00' in arg for arg in command):
                raise ValueError("Invalid entrypoint arguments")
        if len(set(self.required_secrets)) != len(self.required_secrets):
            raise ValueError("Duplicate secret declaration")
        import re
        if any(not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', key) for key in self.required_secrets):
            raise ValueError("Secret declarations must be uppercase names")
        validate_config_schema(self.config_schema)
        return self


class TemplateBinding(StrictModel):
    template_id: str = Field(pattern=ID_PATTERN)
    template_version: str = Field(pattern=VERSION_PATTERN)
    config: dict = Field(default_factory=dict)
    workspace: WorkspaceSpec | None = None

    @model_validator(mode='after')
    def size_limit(self):
        if len(self.model_dump_json().encode()) > 32768:
            raise ValueError("Instance configuration exceeds 32 KiB")
        return self


class TemplateCatalog:
    def __init__(self, registry):
        self.registry, self.store = registry, registry.store
        self.root = registry.directory / 'templates'
        self.root.mkdir(exist_ok=True)
        if is_link(self.root.lstat()):
            raise ValueError("Template storage must not be a link")

    @staticmethod
    def public(row):
        return {'definition': json.loads(row['metadata_json']),
                'package_sha256': row['package_sha256'] or None, 'created_at': row['created_at'],
                'status': 'installed' if row['package_sha256'] else 'registered'}

    @staticmethod
    def require(db, template_id, version):
        row = db.execute('SELECT * FROM template_versions WHERE id=? AND version=?', (template_id, version)).fetchone()
        if row is None:
            raise KnowledgeError('unknown_template', 'Template version is not registered', 404)
        return row

    def version_path(self, template_id, version):
        import re
        if not re.fullmatch(ID_PATTERN, template_id) or not re.fullmatch(VERSION_PATTERN, version):
            raise KnowledgeError('invalid_template_id', 'Invalid template ID or version', 400)
        try:
            relative_path(template_id)
            relative_path(version)
        except ValueError:
            raise KnowledgeError('invalid_template_id', 'Template ID or version is not portable', 400) from None
        path = self.root / template_id / version
        for ancestor in (self.root, path.parent, path):
            if ancestor.exists() and is_link(ancestor.lstat()):
                raise KnowledgeError('unsafe_template_path', 'Template storage contains a link', 409)
        return path

    def register(self, value: TemplateRegistration):
        metadata = value.model_dump()
        if value.plugin is None:
            metadata.pop('plugin')  # Preserve legacy registration equality and worker contracts.
        payload = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        with self.store.transaction(write=True) as db:
            row = db.execute('SELECT * FROM template_versions WHERE id=? AND version=?', (value.id, value.version)).fetchone()
            if row is not None:
                if row['metadata_json'] != payload:
                    raise KnowledgeError('template_version_conflict', 'A template version is immutable; register a new version', 409)
                return dict(self.public(row), duplicate=True)
            db.execute('INSERT INTO template_versions VALUES (?,?,?,?,?)', (value.id, value.version, payload, '', now()))
            self.event(db, 'template_register', value.id, value.version)
            return dict(self.public(self.require(db, value.id, value.version)), duplicate=False)

    def listing(self, offset=0, limit=50):
        with self.store.transaction() as db:
            rows = db.execute('SELECT * FROM template_versions ORDER BY id,version LIMIT ? OFFSET ?', (limit + 1, offset)).fetchall()
            return {'templates': [self.public(row) for row in rows[:limit]],
                    'next_offset': offset + limit if len(rows) > limit else None}

    def get(self, template_id, version):
        with self.store.transaction() as db:
            return self.public(self.require(db, template_id, version))

    @staticmethod
    def extract(data: bytes, destination: Path):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if not entries or len(entries) > MAX_ENTRIES:
                    raise ValueError("Invalid package entry count")
                seen, directory_spellings, total = set(), {}, 0
                for entry in entries:
                    if entry.orig_filename != entry.filename:
                        raise ValueError("Archive filename was normalized or truncated")
                    name = entry.filename.rstrip('/') if entry.is_dir() else entry.filename
                    relative_path(name)
                    folded = name.casefold()
                    if folded in seen:
                        raise ValueError("Duplicate archive path")
                    seen.add(folded)
                    parts = name.split('/')
                    for count in range(1, len(parts) + (1 if entry.is_dir() else 0)):
                        prefix = '/'.join(parts[:count])
                        previous = directory_spellings.setdefault(prefix.casefold(), prefix)
                        if previous != prefix:
                            raise ValueError("Case-colliding directory paths")
                    mode = entry.external_attr >> 16
                    kind = stat.S_IFMT(mode)
                    if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (entry.flag_bits & 1):
                        raise ValueError("Links, special files and encrypted files are unsupported")
                    if kind == stat.S_IFDIR and not entry.is_dir():
                        raise ValueError("Directory metadata mismatch")
                    target = destination / name
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    total += entry.file_size
                    if total > MAX_PACKAGE_BYTES:
                        raise ValueError("Expanded package exceeds limit")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, target.open('xb') as output:
                        remaining = entry.file_size
                        while True:
                            block = source.read(min(65536, remaining + 1))
                            if not block:
                                break
                            remaining -= len(block)
                            if remaining < 0:
                                raise ValueError("Archive length mismatch")
                            output.write(block)
                        if remaining != 0:
                            raise ValueError("Truncated package")
        except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error, EOFError):
            raise KnowledgeError('invalid_template_package', 'Package must be a bounded ZIP of regular files with portable paths', 400) from None

    def install(self, template_id, version, archive: bytes):
        if len(archive) > MAX_PACKAGE_BYTES:
            raise KnowledgeError('template_package_too_large', 'Package exceeds 32 MiB', 413)
        definition = self.get(template_id, version)['definition']
        if definition.get('source', {}).get('type') != 'uploaded-zip':
            raise KnowledgeError('unsupported_template_source', 'This template does not use uploaded ZIP installation', 409)
        if hashlib.sha256(archive).hexdigest() != definition['source']['archive_sha256']:
            raise KnowledgeError('template_digest_mismatch', 'Archive SHA-256 does not match registration', 409)
        destination = self.version_path(template_id, version)
        staging_root = self.root / '.staging'
        staging_root.mkdir(exist_ok=True)
        if is_link(staging_root.lstat()):
            raise KnowledgeError('unsafe_template_path', 'Staging storage contains a link', 409)
        stage = staging_root / uuid.uuid4().hex
        stage.mkdir()
        try:
            package = stage / 'package'
            package.mkdir()
            self.extract(archive, package)
            commands = list(definition['entrypoints'].values())
            index = (definition.get('plugin') or {}).get('indexing')
            if index and index.get('entrypoint'):
                commands.append(index['entrypoint'])
            for command in commands:
                if not (package / command[1]).is_file():
                    raise KnowledgeError('missing_template_entrypoint', 'An entrypoint script is absent from the package', 400)
            digest = tree_digest(package)
            write_json(stage / 'manifest.json', {'id': template_id, 'version': version,
                       'archive_sha256': definition['source']['archive_sha256'], 'package_sha256': digest})
            with self.store.transaction(write=True) as db:
                row = self.require(db, template_id, version)
                # Serialize installation with bind/install mutations across processes.
                destination = self.version_path(template_id, version)
                if destination.exists():
                    try:
                        actual = tree_digest(destination / 'package')
                    except (OSError, ValueError):
                        actual = None
                    if actual != digest:
                        raise KnowledgeError('template_install_conflict', 'Installed content differs; versions cannot be replaced', 409)
                else:
                    destination.parent.mkdir(exist_ok=True)
                    stage.rename(destination)
                if row['package_sha256'] and row['package_sha256'] != digest:
                    raise KnowledgeError('template_install_conflict', 'Registered package digest differs', 409)
                db.execute('UPDATE template_versions SET package_sha256=? WHERE id=? AND version=?', (digest, template_id, version))
                self.event(db, 'template_install', template_id, version)
                return self.public(self.require(db, template_id, version))
        finally:
            # Only remove this generated staging directory, never an installed version.
            if stage.exists() and stage.resolve().is_relative_to(staging_root.resolve()) and stage != staging_root:
                shutil.rmtree(stage)

    def bind(self, library_id, value: TemplateBinding, db=None):
        with self.store.transaction(write=True) if db is None else nullcontext(db) as db:
            library = db.execute('SELECT * FROM libraries WHERE id=?', (library_id,)).fetchone()
            if library is None:
                raise KnowledgeError('unknown_library', 'Library does not exist', 404)
            row = self.require(db, value.template_id, value.template_version)
            if not row['package_sha256']:
                raise KnowledgeError('template_not_installed', 'Install the template before binding', 409)
            definition = json.loads(row['metadata_json'])
            updates = bool(set(definition['capabilities']) & {'initialize', 'update'})
            native = db.execute('SELECT 1 FROM library_ownership WHERE library_id=? AND native=1', (library_id,)).fetchone()
            if native and (updates or value.workspace is not None):
                raise KnowledgeError('native_library_managed', '原生文档库只允许绑定检索定义或使用指引，不能运行更新程序或扩大工作范围', 409)
            if 'update' not in definition['capabilities'] and not definition.get('plugin'):
                raise KnowledgeError('template_cannot_update', 'Knowledge binding requires update capability', 409)
            if value.workspace is not None and definition['protocol_version'] != '1.1':
                raise KnowledgeError('workspace_protocol_required', 'Explicit workspaces require template protocol 1.1', 409)
            scope = resolve_workspace(self.registry, db, library, value.workspace)
            scope_json = None if native else scope['spec'].model_dump_json()
            for other in db.execute('SELECT * FROM library_templates WHERE workspace_key=? AND library_id<>?',
                                    (scope['workspace_key'], library_id)):
                old_scope = WorkspaceSpec.model_validate_json(other['workspace_json'])
                if (other['template_id'] != value.template_id or other['template_version'] != value.template_version or
                        json.loads(other['config_json']) != value.config or
                        old_scope.exclude_paths != scope['spec'].exclude_paths):
                    raise KnowledgeError('workspace_binding_conflict',
                                         'Views sharing a workspace must use the same template, configuration and exclusions', 409)
            try:
                Draft202012Validator(definition['config_schema']).validate(value.config)
            except ValidationError:
                # Never echo the instance value (it may contain an accidental secret).
                raise KnowledgeError('invalid_template_config', 'Configuration does not satisfy the template schema', 422) from None
            except Exception:
                raise KnowledgeError('invalid_template_schema', 'Configuration schema cannot be resolved', 422) from None
            try:
                digest = tree_digest(self.version_path(value.template_id, value.template_version) / 'package')
            except (OSError, ValueError):
                raise KnowledgeError('template_package_unavailable', 'Installed package is missing or unsafe', 409) from None
            if digest != row['package_sha256']:
                raise KnowledgeError('template_package_changed', 'Installed package no longer matches its digest', 409)
            db.execute("""INSERT INTO library_templates
                (library_id,template_id,template_version,config_json,workspace_json,workspace_key) VALUES (?,?,?,?,?,?)
                ON CONFLICT(library_id) DO UPDATE SET template_id=excluded.template_id,
                template_version=excluded.template_version,config_json=excluded.config_json,
                workspace_json=excluded.workspace_json,workspace_key=excluded.workspace_key""",
                (library_id, value.template_id, value.template_version, json.dumps(value.config, ensure_ascii=False),
                 scope_json, None if native else scope['workspace_key']))
            self.event(db, 'template_bind', value.template_id, value.template_version, library_id)
            return self.binding(library_id, db)

    def binding(self, library_id, db=None):
        with self.store.transaction() if db is None else nullcontext(db) as db:
            if db.execute('SELECT 1 FROM libraries WHERE id=?', (library_id,)).fetchone() is None:
                raise KnowledgeError('unknown_library', 'Library does not exist', 404)
            row = db.execute('SELECT * FROM library_templates WHERE library_id=?', (library_id,)).fetchone()
            return {'library_id': library_id, 'binding': None if row is None else {
                'template_id': row['template_id'], 'template_version': row['template_version'],
                'config': json.loads(row['config_json']),
                'workspace': json.loads(row['workspace_json']) if row['workspace_json'] else None,
                'workspace_key': row['workspace_key']}}

    def snapshot_source(self, library_id):
        """Resolve the source and visible view together in one DB snapshot before each job."""
        with self.store.transaction() as db:
            library = db.execute('SELECT * FROM libraries WHERE id=? AND enabled=1', (library_id,)).fetchone()
            binding = db.execute('SELECT * FROM library_templates WHERE library_id=?', (library_id,)).fetchone()
            if not library or not binding:
                raise KnowledgeError('workspace_unavailable', 'Enabled library and template binding required', 409)
            specification = WorkspaceSpec.model_validate_json(binding['workspace_json']) if binding['workspace_json'] else None
            return resolve_workspace(self.registry, db, library, specification)

    def unbind(self, library_id):
        self.binding(library_id)
        with self.store.transaction(write=True) as db:
            db.execute('DELETE FROM library_templates WHERE library_id=?', (library_id,))
            self.store.event(db, {'kind': 'admin', 'action': 'template_unbind', 'library_id': library_id, 'status': 'ok'})
        return {'library_id': library_id, 'binding': None}

    def event(self, db, action, template_id, version, library_id=None):
        self.store.event(db, {'kind': 'admin', 'action': action, 'path': template_id + '/' + version,
                              'library_id': library_id, 'status': 'ok'})


def add_template_routes(app, registry):
    catalog = TemplateCatalog(registry)
    app.state.templates = catalog

    @app.post('/admin/api/templates', status_code=201)
    def register(value: TemplateRegistration):
        return catalog.register(value)

    @app.get('/admin/api/templates')
    def listing(offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100)):
        return catalog.listing(offset, limit)

    @app.get('/admin/api/templates/{template_id}/versions/{version}')
    def detail(template_id: str, version: str):
        return catalog.get(template_id, version)

    @app.put('/admin/api/templates/{template_id}/versions/{version}/package')
    async def install(request: Request, template_id: str, version: str):
        # Stop unknown uploads before consuming their bodies.
        await anyio.to_thread.run_sync(catalog.get, template_id, version)
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_PACKAGE_BYTES:
                raise KnowledgeError('template_package_too_large', 'Package exceeds 32 MiB', 413)
            data.extend(chunk)
        return await anyio.to_thread.run_sync(catalog.install, template_id, version, bytes(data))

    @app.get('/admin/api/libraries/{library_id}/template')
    def get_binding(library_id: str):
        return catalog.binding(library_id)

    @app.put('/admin/api/libraries/{library_id}/template')
    def bind(library_id: str, value: TemplateBinding):
        return catalog.bind(library_id, value)

    @app.delete('/admin/api/libraries/{library_id}/template')
    def unbind(library_id: str):
        return catalog.unbind(library_id)
