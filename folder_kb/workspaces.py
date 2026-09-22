"""Explicit update workspaces, separate from consumer-facing library roots."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
from typing import Literal

from pydantic import Field, model_validator

from .core import is_link
from .errors import KnowledgeError
from .inbox import StrictModel


def scoped_path(value):
    if value == '.':
        return value
    if (not value or len(value) > 512 or any(c in value for c in ('\\', ':', '\x00')) or
            any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError('Expected a relative workspace path')
    return value


class WorkspaceSpec(StrictModel):
    type: Literal['local', 'git']
    root_path: str | None = None
    repository_id: str | None = None
    workspace_subpath: str = '.'
    knowledge_subpath: str = '.'
    exclude_paths: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode='after')
    def source(self):
        if self.type == 'local':
            if self.root_path is None or self.repository_id is not None or self.workspace_subpath != '.':
                raise ValueError('Local workspaces require root_path and no repository/subpath')
            scoped_path(self.root_path)
        elif not self.repository_id or self.root_path is not None:
            raise ValueError('Git workspaces require repository_id and no root_path')
        scoped_path(self.workspace_subpath)
        scoped_path(self.knowledge_subpath)
        for path in self.exclude_paths:
            scoped_path(path)
            if path == '.':
                raise ValueError('Cannot exclude the workspace root')
        self.exclude_paths = sorted(set(self.exclude_paths))
        if excluded(self.knowledge_subpath, self.exclude_paths):
            raise ValueError('The retrieval directory is excluded')
        return self


def excluded(relative: str, exclusions=()):
    parts = PurePosixPath(relative).parts
    if any(p in ('.git', '.env', '.venv', '__pycache__', '.cache', '.secrets', 'credentials')
           or p.startswith('.env.') or p.endswith(('.pem', '.key')) for p in parts):
        return True
    return any(relative == path or relative.startswith(path + '/') for path in exclusions)


def workspace_files(root: Path, exclusions=(), include_dirs=False):
    if is_link(root.lstat()) or not root.is_dir():
        raise ValueError('Expected a regular workspace')
    files, total, entries = [], 0, 0
    for directory, folders, names in os.walk(root, followlinks=False):
        for name in list(folders) + names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if excluded(relative, exclusions):
                if name in folders:
                    folders.remove(name)
                continue
            entries += 1
            if entries > 2000:
                raise ValueError('Workspace entry limit exceeded')
            if is_link(path.lstat()):
                raise ValueError('Workspace contains a link or junction')
            if path.is_dir():
                if include_dirs:
                    files.append(path)
                continue
            if not path.is_file():
                raise ValueError('Workspace contains a special file')
            total += path.stat().st_size
            if total > 32 * 1024 * 1024:
                raise ValueError('Workspace byte limit exceeded')
            files.append(path)
    return sorted(files)


def workspace_digest(root: Path, exclusions=()):
    entries = [(p.relative_to(root).as_posix(), "directory" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest())
               for p in workspace_files(root, exclusions, include_dirs=True)]
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()


def copy_workspace(source: Path, target: Path, exclusions=()):
    files = workspace_files(source, exclusions, include_dirs=True)
    target.mkdir(parents=True, exist_ok=False)
    for path in files:
        output = target / path.relative_to(source)
        if path.is_dir():
            output.mkdir(parents=True, exist_ok=True)
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, output)


def resolve_workspace(registry, db, library, specification: WorkspaceSpec | None):
    binding = db.execute('SELECT * FROM repository_libraries WHERE library_id=?', (library['id'],)).fetchone()
    if specification is None:
        # Existing bindings do not silently gain access to a parent directory.
        if binding:
            specification = WorkspaceSpec(type='git', repository_id=binding['repository_id'],
                                          workspace_subpath=binding['subpath'])
        else:
            specification = WorkspaceSpec(type='local', root_path=library['path'])
    commit = None
    if specification.type == 'git':
        if not binding or binding['repository_id'] != specification.repository_id:
            raise KnowledgeError('workspace_repository_mismatch', 'Library must belong to the selected repository', 409)
        repo = db.execute('SELECT * FROM repositories WHERE id=?', (specification.repository_id,)).fetchone()
        if not repo or not repo['enabled'] or not repo['release_path'] or not repo['current_commit']:
            raise KnowledgeError('workspace_unavailable', 'Repository has no enabled published version', 409)
        root_relative = (PurePosixPath(repo['release_path']) / specification.workspace_subpath).as_posix()
        commit = repo['current_commit']
        identity = ['git', specification.repository_id, specification.workspace_subpath]
    else:
        if binding:
            raise KnowledgeError('workspace_source_mismatch', 'Git-managed libraries require a Git workspace', 409)
        root_relative = specification.root_path
        identity = ['local', str(registry.pool.root), root_relative]
    root = registry.folder_for_path(root_relative).root
    retrieval = registry.folder_for_path((PurePosixPath(root_relative) / specification.knowledge_subpath).as_posix()).root
    current = registry.folder_for_path(library['path']).root
    if not retrieval.is_relative_to(root) or retrieval != current:
        raise KnowledgeError('workspace_view_mismatch', 'Workspace retrieval subdirectory must match the library mount', 409)
    if specification.type == 'local':
        identity = ['local', os.path.normcase(str(root))]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return {'spec': specification, 'root': root, 'workspace_key': key, 'source_version': commit}
