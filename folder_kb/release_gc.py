"""Conservative, opt-in cleanup of old Agent publication snapshots."""
import argparse
import json
import os
from pathlib import PurePosixPath
import re
import time

from .core import is_link
from .file_writes import remove_release
from .management import Registry


def collect(registry, keep=20, min_age_days=7, apply=False):
    if not 1 <= keep <= 1000 or min_age_days < 1:
        raise ValueError('keep must be 1..1000; min_age_days must be at least 1')
    removed, candidates, skipped = [], [], []
    # Hold the same write lock as publication while deleting, so references cannot move.
    with registry.store.transaction(write=apply) as db:
        records = list(db.execute('SELECT rowid,* FROM file_write_receipts ORDER BY rowid DESC'))
        retained, counts, known = set(), {}, set()
        for r in records:
            known.update((r['previous_root'], r['published_root']))
            count = counts.get(r['library_id'], 0)
            if count < keep:
                retained.add(r['published_root'])
            counts[r['library_id']] = count + 1
        # Preserve every explicit reference outside historical write receipts: library roots,
        # drafts, update tasks, Git releases, template workspace JSON and future tables too.
        references = []
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            name = row[0]
            if name == 'file_write_receipts' or name.startswith('sqlite_'):
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            for record in db.execute('SELECT * FROM ' + quoted):
                references.extend(str(v).casefold() for v in record if v is not None)
        roots = [r[0] for r in db.execute('SELECT path FROM libraries')]
        cutoff = time.time() - min_age_days * 86400
        for relative in sorted(known - retained):
            pure = PurePosixPath(relative)
            if len(pure.parts) != 2 or pure.parts[0] != 'document-releases' or not re.fullmatch('[0-9a-f]{32}', pure.name):
                continue
            # Parent mounts (e.g. '.') also protect every release beneath them.
            if any(root == '.' or relative == root or relative.startswith(root.rstrip('/') + '/') or
                   root.startswith(relative + '/') for root in roots) or any(pure.name in value for value in references):
                skipped.append({'path': relative, 'reason': 'referenced'})
                continue
            target = registry.pool.root / pure
            if not target.exists():
                continue
            if is_link(target.lstat()) or not target.is_dir():
                skipped.append({'path': relative, 'reason': 'unsafe'})
                continue
            if target.stat().st_mtime > cutoff:
                skipped.append({'path': relative, 'reason': 'too_recent'})
                continue
            candidates.append(relative)
            if apply:
                remove_release(registry.pool.root, target)
                removed.append(relative)
        if apply:
            registry.store.event(db, {'kind': 'admin', 'action': 'release_gc', 'status': 'ok'})
    return {'dry_run': not apply, 'keep': keep, 'min_age_days': min_age_days,
            'candidates': candidates, 'removed': removed, 'skipped': skipped}


def main():
    parser = argparse.ArgumentParser(description='Preview or remove unreferenced old Agent snapshots; never cleans arbitrary folders')
    parser.add_argument('--root', default=os.getenv('KB_ROOT'), required=not os.getenv('KB_ROOT'))
    parser.add_argument('--state-dir', default=os.getenv('KB_STATE_DIR'), required=not os.getenv('KB_STATE_DIR'))
    parser.add_argument('--keep', type=int, default=20)
    parser.add_argument('--min-age-days', type=int, default=7)
    parser.add_argument('--apply', action='store_true', help='Delete candidates; default only previews')
    args = parser.parse_args()
    print(json.dumps(collect(Registry(args.root, args.state_dir), args.keep, args.min_age_days, args.apply), indent=2))


if __name__ == '__main__':
    main()
