"""File protocol validation for a trusted, local reference template.

No sandbox is provided by this module. Production scheduling must use an
isolated worker/container before accepting third-party programs.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .core import is_link
from .inbox import Material


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TemplateRef(Contract):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Record(Contract):
    id: str = Field(min_length=1, max_length=128)
    material: Material


class JobRequest(Contract):
    protocol_version: Literal["1.0"] = "1.0"
    job_id: str
    operation: Literal["initialize", "update"]
    library_id: str
    base_version: str
    template: TemplateRef
    config: dict = Field(default_factory=dict)


class MaterialResult(Contract):
    record_id: str
    outcome: Literal["accepted", "partially_accepted", "rejected"]
    summary: str = Field(min_length=1, max_length=4000)
    references: list[str] = Field(default_factory=list, max_length=32)


class JobResult(Contract):
    protocol_version: Literal["1.0"]
    job_id: str
    library_id: str
    base_version: str
    template: TemplateRef
    candidate_path: Literal["knowledge"]
    summary: str = Field(min_length=1, max_length=4000)
    materials: list[MaterialResult] = Field(max_length=100)


def tree_digest(root: Path) -> str:
    """Hash an ordinary bounded tree, rejecting links/junctions and special files."""
    if is_link(root.lstat()) or not root.is_dir():
        raise ValueError("Expected a regular directory")
    entries, size = [], 0
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in sorted(folders + files):
            path = Path(directory) / name
            st = path.lstat()
            if is_link(st):
                raise ValueError("Links and junctions are not allowed")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError("Special files are not allowed")
            size += st.st_size
            if size > 32 * 1024 * 1024 or len(entries) >= 2000:
                raise ValueError("Template demo tree limit exceeded")
            entries.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(sorted(entries), ensure_ascii=False).encode()).hexdigest()


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_job(job: Path, request: JobRequest, records: list[Record], knowledge: Path):
    if len(records) > 100 or len({r.id for r in records}) != len(records):
        raise ValueError("A job requires at most 100 uniquely identified records")
    if any(r.material.library_id != request.library_id for r in records):
        raise ValueError("Cross-library material is not allowed")
    if request.operation == "initialize" and records:
        raise ValueError("Initialization does not consume materials")
    if tree_digest(knowledge) != request.base_version:
        raise ValueError("Base version changed before snapshot")
    job.mkdir(parents=True, exist_ok=False)
    inputs = job / "input"
    inputs.mkdir()
    (job / "work").mkdir()
    (job / "output").mkdir()
    shutil.copytree(knowledge, inputs / "knowledge")
    if tree_digest(inputs / "knowledge") != request.base_version:
        raise ValueError("Knowledge changed while snapshotting")
    write_json(inputs / "request.json", request.model_dump())
    (inputs / "materials.jsonl").write_text(
        "".join(record.model_dump_json() + "\n" for record in records), encoding="utf-8")


def validate_result(job: Path, request: JobRequest, records: list[Record]) -> JobResult:
    output = job / "output"
    tree_digest(output)  # Also checks result.json and all output paths for unsafe files.
    result_file = output / "result.json"
    if result_file.stat().st_size > 1024 * 1024:
        raise ValueError("Result exceeds limit")
    result = JobResult.model_validate_json(result_file.read_text(encoding="utf-8"))
    for field in ("job_id", "library_id", "base_version", "template"):
        if getattr(result, field) != getattr(request, field):
            raise ValueError("Result identity/base version does not match request")
    ids = [r.record_id for r in result.materials]
    if len(ids) != len(set(ids)) or set(ids) != {r.id for r in records}:
        raise ValueError("Result must account for each input record exactly once")
    knowledge = output / "knowledge"
    tree_digest(knowledge)
    if not (knowledge / "INDEX.md").is_file():
        raise ValueError("Candidate requires INDEX.md")
    for item in result.materials:
        for reference in item.references:
            relative = PurePosixPath(reference)
            if not reference or relative.is_absolute() or ".." in relative.parts or "\\" in reference or ":" in reference:
                raise ValueError("Invalid output reference")
            path = (knowledge / reference).resolve()
            if not path.is_relative_to(knowledge.resolve()) or not path.is_file():
                raise ValueError("Output reference does not exist within candidate")
    return result


def run_reference(job: Path, request: JobRequest, records: list[Record], package: Path,
                  timeout_seconds: float = 30) -> JobResult:
    """Developer smoke runner. Run only the reviewed reference package, never untrusted code."""
    if tree_digest(package) != request.template.package_sha256:
        raise ValueError("Installed template package changed")
    inputs = job / "input"
    before = tree_digest(inputs)
    # Avoid passing credentials from the host environment to the reference program.
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH") if key in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    command = [sys.executable, "-I", "-B", str((package / "program.py").resolve()), request.operation,
               "--input", str(inputs.resolve()), "--work", str((job / "work").resolve()),
               "--output", str((job / "output").resolve())]
    subprocess.run(command, cwd=job / "work", env=env, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=timeout_seconds, check=True, shell=False)
    if tree_digest(inputs) != before or tree_digest(package) != request.template.package_sha256:
        raise ValueError("Reference template modified its input or package")
    return validate_result(job, request, records)
