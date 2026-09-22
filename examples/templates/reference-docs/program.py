"""A deterministic protocol fixture, not a knowledge extraction model."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["initialize", "update"])
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads((args.input / "request.json").read_text(encoding="utf-8"))
    if request["operation"] != args.operation:
        raise ValueError("Operation does not match request")
    args.work.mkdir(exist_ok=True)
    # The platform supplies a new, empty output directory for every attempt.
    destination = args.output / "knowledge"
    shutil.copytree(args.input / "knowledge", destination)
    records = [json.loads(line) for line in
               (args.input / "materials.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    notes = destination / "demo-inbox"
    notes.mkdir(exist_ok=True)
    results = []
    for record in records:
        material = record["material"]
        name = hashlib.sha256(record["id"].encode()).hexdigest() + ".md"
        relative = "demo-inbox/" + name
        # No model or fact validation: even new content is kept as review material.
        text = ("# " + (material.get("title") or "待审核材料") + "\n\n"
                "测试模板生成；仅整理格式，未经事实验证。\n\n"
                + "提交记录：" + record["id"] + "\n\n"
                + "用途：" + material["kind"] + "\n\n"
                + "来源：" + json.dumps(material.get("source"), ensure_ascii=False) + "\n\n"
                + "场景：" + json.dumps(material.get("context", {}), ensure_ascii=False) + "\n\n"
                + material["content"]["text"] + "\n")
        (destination / relative).write_text(text, encoding="utf-8")
        results.append({"record_id": record["id"], "outcome": "partially_accepted",
                        "summary": "已整理为候选审核材料，未验证事实或替换已有知识。",
                        "references": [relative]})
    (notes / "INDEX.md").write_text(
        "# 测试材料索引\n\n" + "".join("- [" + p.stem + "](" + p.name + ")\n"
           for p in sorted(notes.glob("*.md")) if p.name != "INDEX.md"), encoding="utf-8")
    if not (destination / "INDEX.md").exists():
        (destination / "INDEX.md").write_text("# 知识目录\n\n- [待审核材料](demo-inbox/INDEX.md)\n", encoding="utf-8")
    result = {"protocol_version": "1.0", "job_id": request["job_id"],
              "library_id": request["library_id"], "base_version": request["base_version"],
              "template": request["template"], "candidate_path": "knowledge",
              "summary": "测试模板已生成候选目录；尚未审核或发布。", "materials": results}
    (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
