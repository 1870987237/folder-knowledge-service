"""Preserve benchmark/platform source without copying local state or credentials."""
import hashlib
import json
from pathlib import Path
import subprocess


def freeze_source(output):
    root=Path(__file__).resolve().parents[1]
    destination=Path(output).resolve()/'source-snapshot'
    destination.mkdir()  # Never replace an earlier snapshot.
    tracked=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=root).decode('utf-8').split('\0')
    # Include new benchmark files as well, but never arbitrary untracked state.
    candidates=set(p for p in tracked if p.startswith(('benchmarks/','folder_kb/','tools/','examples/')))
    records={}
    for name in sorted(candidates):
        source=root/name
        if (not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(root)
                or any(part in ('__pycache__','node_modules','web','.git') for part in source.parts)
                or source.suffix not in ('.py','.ts','.json','.csv','.md')):
            continue
        content=source.read_bytes();target=destination/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(content);records[name]=hashlib.sha256(content).hexdigest()
    manifest={'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
        'dirty':bool(subprocess.check_output(['git','status','--porcelain','--untracked-files=normal'],cwd=root,text=True).strip()),
        'files':records,'sha256':hashlib.sha256(json.dumps(records,sort_keys=True).encode()).hexdigest()}
    (destination/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return {'directory':'source-snapshot','sha256':manifest['sha256'],'git_commit':manifest['git_commit'],'dirty':manifest['dirty']}
