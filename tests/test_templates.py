import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from folder_kb.management import Registry
from folder_kb.template_demo import run_demo
from folder_kb.workspaces import workspace_digest
from folder_kb.template_protocol import (JobRequest, Record, TemplateRef, prepare_job,
                                         run_reference, tree_digest, validate_result, write_json)


@pytest.fixture
def job(tmp_path):
    source = Path(__file__).resolve().parents[1] / "examples/templates/reference-docs"
    package = tmp_path / "package"
    shutil.copytree(source, package)
    knowledge = tmp_path / "base"
    knowledge.mkdir()
    (knowledge / "INDEX.md").write_text("# Existing\n", encoding="utf-8")
    template = TemplateRef(id="reference-docs", version="0.1.0", package_sha256=tree_digest(package))
    request = JobRequest(job_id="job1", operation="update", library_id="alpha",
                         base_version=workspace_digest(knowledge), template=template)
    record = Record(id="record1", material={"submission_id": "one", "library_id": "alpha",
                                          "kind": "new_content", "content": {"text": "A new fact"},
                                          "context": {"condition": "example"}})
    target = tmp_path / "job"
    return package, knowledge, request, record, target


def test_roundtrip(tmp_path):
    summary = run_demo(tmp_path)
    assert summary["published"] is False and summary["original_knowledge_unchanged"]
    assert summary["auxiliary_preserved"] and summary["credentials_excluded"]
    result = json.loads(Path(summary["result"]).read_text(encoding="utf-8"))
    assert len(result["materials"]) == 3
    assert all(x["outcome"] == "partially_accepted" for x in result["materials"])
    with sqlite3.connect(summary["database"]) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 10
        assert db.execute("SELECT count(*) FROM template_versions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM library_templates").fetchone()[0] == 1
        assert db.execute("SELECT status FROM template_jobs").fetchone()[0] == "candidate_ready"
        assert db.execute("SELECT count(*) FROM inbox_items WHERE status='completed'").fetchone()[0] == 3
    assert (Path(summary["candidate_directory"]) / "INDEX.md").read_text().startswith("# Existing knowledge")


def test_repeat_and_cross_library(job):
    package, knowledge, request, record, target = job
    prepare_job(target, request, [record], knowledge)
    result = run_reference(target, request, [record], package)
    candidate = target / "output/workspace"
    assert result.materials[0].references
    assert (knowledge / "INDEX.md").read_text() == "# Existing\n"
    new_request = request.model_copy(update={"job_id": "job2", "base_version": workspace_digest(candidate)})
    second = target.parent / "job2"
    prepare_job(second, new_request, [record], candidate)
    run_reference(second, new_request, [record], package)
    assert len(list((second / "output/workspace/demo-inbox").glob("*.md"))) == 2
    other = record.model_copy(update={"material": record.material.model_copy(update={"library_id": "beta"})})
    with pytest.raises(ValueError, match="Cross-library"):
        prepare_job(target.parent / "invalid", request, [other], knowledge)


@pytest.mark.parametrize("mutation", ["identity", "missing", "duplicate", "traversal"])
def test_bad_results(job, mutation):
    package, knowledge, request, record, target = job
    prepare_job(target, request, [record], knowledge)
    run_reference(target, request, [record], package)
    path = target / "output/result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "identity":
        result["base_version"] = "different"
    elif mutation == "missing":
        result["materials"] = []
    elif mutation == "duplicate":
        result["materials"] *= 2
    else:
        result["materials"][0]["references"] = ["../result.json"]
    write_json(path, result)
    with pytest.raises(ValueError):
        validate_result(target, request, [record])


def test_source_change_and_timeout(job, monkeypatch):
    package, knowledge, request, record, target = job
    prepare_job(target, request, [record], knowledge)
    original = (package / "program.py").read_bytes()
    (package / "program.py").write_text("raise RuntimeError()", encoding="utf-8")
    with pytest.raises(ValueError, match="package changed"):
        run_reference(target, request, [record], package)
    (package / "program.py").write_bytes(original)
    def timeout(*args, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 1
        assert "KB_ADMIN_TOKEN" not in kwargs["env"]
        raise subprocess.TimeoutExpired(args[0], 1)
    monkeypatch.setenv("KB_ADMIN_TOKEN", "must-not-leak")
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        run_reference(target, request, [record], package, timeout_seconds=1)


def test_input_version_and_duplicate_records(job):
    _, knowledge, request, record, target = job
    with pytest.raises(ValueError, match="uniquely"):
        prepare_job(target, request, [record, record], knowledge)
    (knowledge / "INDEX.md").write_text("changed")
    with pytest.raises(ValueError, match="Base version"):
        prepare_job(target, request, [record], knowledge)


def test_v3_migration(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    registry = Registry(str(root), str(tmp_path / "state"))
    registry.save_library("alpha", "alpha", ".", True, create=True)
    key = registry.create_key("agent", ["alpha"], None)
    with registry.store.transaction(write=True) as db:
        for table in ("template_jobs", "library_templates", "template_versions"):
            db.execute("DROP TABLE " + table)
        db.execute("PRAGMA user_version=3")
    reopened = Registry(str(root), str(tmp_path / "state"))
    assert reopened.authenticate(key["token"])["library_ids"] == ["alpha"]
    with reopened.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM template_versions").fetchone()[0] == 0
        assert db.execute("PRAGMA user_version").fetchone()[0] == 10
