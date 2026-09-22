"""Run the bundled, trusted template against disposable platform data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import uuid

from .inbox import Completion, Inbox, Material, Release
from .management import Registry
from .storage import now
from .template_protocol import JobRequest, Record, TemplateRef, prepare_job, run_reference, tree_digest, write_json
from .template_package import build_package
from .templates import TemplateCatalog, TemplateRegistration, TemplateBinding
from .workspaces import WorkspaceSpec, workspace_digest




def run_demo(output: Path) -> dict:
    # Every invocation gets a fresh instance; never opens the live server's database.
    demo = output.resolve() / ("run-" + uuid.uuid4().hex[:12])
    demo.mkdir(parents=True, exist_ok=False)
    root, state = demo / "knowledge", demo / "platform-data"
    workspace = root / "demo-library"
    library = workspace / "knowledge"
    library.mkdir(parents=True)
    (library / "INDEX.md").write_text("# Existing knowledge\n\nDo not replace published content.\n", encoding="utf-8")
    registry = Registry(str(root), str(state))
    (workspace / "metadata").mkdir()
    (workspace / "metadata/sources.json").write_text('{"source":"keep this auxiliary data"}', encoding="utf-8")
    (workspace / ".env").write_text("EXAMPLE_SECRET=excluded-demo-placeholder", encoding="utf-8")
    registry.save_library("demo-library", "Protocol demo", "demo-library/knowledge", True, create=True)
    inbox = Inbox(registry)

    examples = Path(__file__).resolve().parents[1] / "examples"
    definition = json.loads((examples / "template-platform-config.json").read_text(encoding="utf-8"))
    source = examples / "templates/reference-docs"
    package = state / "templates" / definition["id"] / definition["version"] / "package"
    archive = build_package(source)
    definition["source"] = {"type": "uploaded-zip", "archive_sha256": hashlib.sha256(archive).hexdigest()}
    catalog = TemplateCatalog(registry)
    catalog.register(TemplateRegistration.model_validate(definition))
    installed = catalog.install(definition["id"], definition["version"], archive)
    template = TemplateRef(id=definition["id"], version=definition["version"],
                           package_sha256=installed["package_sha256"])
    catalog.bind("demo-library", TemplateBinding(template_id=template.id, template_version=template.version,
                 workspace=WorkspaceSpec(type="local", root_path="demo-library", knowledge_subpath="knowledge")))
    scope = catalog.snapshot_source("demo-library")

    empty = demo / "empty-base"
    empty.mkdir()
    initialize = JobRequest(job_id=uuid.uuid4().hex[:12], operation="initialize", library_id="demo-library",
                            base_version=workspace_digest(empty), template=template, knowledge_subpath="knowledge", workspace_key=scope["workspace_key"])
    init_job = state / "jobs" / initialize.job_id
    prepare_job(init_job, initialize, [], empty)
    run_reference(init_job, initialize, [], package)

    key = registry.create_key("reference-demo", ["demo-library"], None)
    inbox.grants(key["key"]["id"], ["demo-library"])
    records = []
    for index, kind in enumerate(("new_content", "correction", "observation")):
        material = Material(submission_id="demo-" + str(index), library_id="demo-library", kind=kind,
                            title={"new_content": "新增说明", "correction": "纠错建议", "observation": "使用观察"}[kind],
                            content={"text": "这是一份测试材料，用于验证平台与模板之间的文件接口。"},
                            source={"reference": "demo:protocol-check"},
                            context={"scenario": "接口测试", "verified": "false"})
        receipt = inbox.submit(key["token"], material)
        records.append(Record(id=receipt["id"], material=material))

    before = workspace_digest(scope["root"], scope["spec"].exclude_paths)
    request = JobRequest(job_id=uuid.uuid4().hex[:12], operation="update", library_id="demo-library",
                         base_version=before, template=template, knowledge_subpath=scope["spec"].knowledge_subpath,
                         workspace_key=scope["workspace_key"], source_version=scope["source_version"], exclude_paths=scope["spec"].exclude_paths)
    job = state / "jobs" / request.job_id
    with registry.store.transaction(write=True) as db:
        db.execute("""INSERT INTO template_jobs
            (id,library_id,template_id,template_version,base_version,status,created_at,updated_at)
            VALUES (?,?,?,?,?,'running',?,?)""",
            (request.job_id, request.library_id, template.id, template.version, before, now(), now()))
    claims = {}
    try:
        for record in records:
            claims[record.id] = inbox.claim(record.id, 900)["claim_token"]
        prepare_job(job, request, records, scope["root"])
        result = run_reference(job, request, records, package)
        if workspace_digest(scope["root"], request.exclude_paths) != before:
            raise ValueError("Published base changed; candidate requires a new run")
        for item in result.materials:
            inbox.finish(item.record_id, Completion(claim_token=claims[item.record_id], outcome=item.outcome,
                         summary=item.summary, references=item.references, version="candidate:" + request.job_id))
            del claims[item.record_id]
        with registry.store.transaction(write=True) as db:
            db.execute("UPDATE template_jobs SET status='candidate_ready',updated_at=?,result_json=? WHERE id=?",
                       (now(), result.model_dump_json(), request.job_id))
    except Exception as exc:
        for record_id, claim in claims.items():
            try:
                inbox.finish(record_id, Release(claim_token=claim))
            except Exception:
                pass  # The lease remains recoverable if release cannot complete.
        with registry.store.transaction(write=True) as db:
            db.execute("UPDATE template_jobs SET status='failed',updated_at=?,error_code=? WHERE id=?",
                       (now(), type(exc).__name__, request.job_id))
        raise
    summary = {"demo_directory": str(demo), "database": str(registry.store.path),
               "template": template.model_dump(), "initialize_job": initialize.job_id, "update_job": request.job_id,
               "candidate_directory": str(job / "output/workspace/knowledge"),
               "candidate_workspace": str(job / "output/workspace"), "result": str(job / "output/result.json"),
               "status": "candidate_ready", "published": False, "original_knowledge_unchanged": workspace_digest(workspace, request.exclude_paths) == before,
               "auxiliary_preserved": (job / "output/workspace/metadata/sources.json").read_bytes() == (workspace / "metadata/sources.json").read_bytes(),
               "credentials_excluded": not (job / "input/workspace/.env").exists(), "material_count": len(records)}
    write_json(demo / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/template-demo"))
    args = parser.parse_args()
    print(json.dumps(run_demo(args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
