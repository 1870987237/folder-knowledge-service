"""Download pinned official shards and materialize ALL questions and corpus documents.

Raw data stays in ignored local directories. Download checkpoints are verified
against Hugging Face LFS SHA256; corpus import can resume without sampling.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import json
from pathlib import Path
import shutil
import time

import httpx

from benchmarks.agent.tasks import TASK_COLUMNS, fingerprint, tasks
from benchmarks.browsecomp.prepare import REPOSITORY, REVISION, decode, docid

CORPUS_REPOSITORY = 'Tevatron/browsecomp-plus-corpus'
CORPUS_REVISION = 'b27b02bc3e45511b8b82a13e6f90ce761df726f6'
QUESTION_COUNT = 830
DOCUMENT_COUNT = 100195


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def inventory(cache, repository, revision):
    folder = Path(cache) / repository.split('/')[-1] / revision
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / 'shards.json'
    if manifest.exists():
        return folder, json.loads(manifest.read_text(encoding='utf-8'))
    with httpx.Client(follow_redirects=True, timeout=90) as client:
        response = client.get(f'https://huggingface.co/api/datasets/{repository}/tree/{revision}/data')
        response.raise_for_status()
        shards = [dict(path=x['path'], bytes=x['size'], sha256=x['lfs']['oid'])
                  for x in response.json() if x['path'].endswith('.parquet')]
    if not shards:
        raise ValueError('No pinned parquet shards found')
    shards.sort(key=lambda x: x['path'])
    atomic_json(manifest, shards)
    return folder, shards


def download(folder, item, repository, revision, reuse=None):
    target = folder / Path(item['path']).name
    if not target.exists() and reuse and Path(reuse).is_file():
        if Path(reuse).stat().st_size == item['bytes'] and sha256(reuse) == item['sha256']:
            shutil.copyfile(reuse, target)
    if target.exists():
        if target.stat().st_size != item['bytes'] or sha256(target) != item['sha256']:
            raise ValueError('Cached shard does not match the pinned LFS digest: ' + target.name)
        return target
    part = target.with_suffix('.part')
    url = f'https://huggingface.co/datasets/{repository}/resolve/{revision}/{item["path"]}'
    for attempt in range(5):
        offset = part.stat().st_size if part.exists() else 0
        if offset > item['bytes']:
            raise ValueError('Partial download exceeds expected size')
        if offset == item['bytes']:
            break
        try:
            with httpx.Client(follow_redirects=True, timeout=120) as client:
                # Avoid stale signed redirects when restarting a long download.
                with client.stream('GET', url, params={'download': 'true', 'resume': str(offset)},
                                   headers={'Range': f'bytes={offset}-'} if offset else {}) as response:
                    response.raise_for_status()
                    if offset and response.status_code == 206:
                        if not response.headers.get('content-range', '').startswith(f'bytes {offset}-'):
                            raise ValueError('Download resumed at the wrong offset')
                    else:
                        offset = 0
                    print(json.dumps({'download': target.name, 'resume_bytes': offset}), flush=True)
                    with part.open('ab' if offset else 'wb') as stream:
                        for chunk in response.iter_bytes(1024 * 1024):
                            stream.write(chunk)
            break
        except (httpx.HTTPError, OSError):
            if attempt == 4:
                raise
            time.sleep(min(2 ** attempt, 10))
    if part.stat().st_size != item['bytes'] or sha256(part) != item['sha256']:
        raise ValueError('Downloaded shard failed size/SHA256 verification: ' + target.name)
    part.replace(target)
    print(json.dumps({'verified_shard': target.name, 'bytes': item['bytes']}), flush=True)
    return target


def download_all(cache):
    sources = []
    for repository, revision, count in ((REPOSITORY, REVISION, 6), (CORPUS_REPOSITORY, CORPUS_REVISION, 7)):
        folder, shards = inventory(cache, repository, revision)
        if len(shards) != count:
            raise ValueError('Unexpected number of shards in pinned revision')
        sources.append(dict(repository=repository, revision=revision, shards=shards, folder=str(folder.resolve())))
    jobs = [(source, item) for source in sources for item in source['shards']]
    def fetch(job):
        source, item = job
        reused = Path('data/browsecomp-upstream') / Path(item['path']).name
        return download(Path(source['folder']), item, source['repository'], source['revision'], reused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(fetch, jobs))
    return sources


def question_rows(source):
    import pyarrow.parquet as pq
    columns = ['query_id', 'query', 'answer', 'evidence_docs', 'gold_docs']
    for item in source['shards']:
        for batch in pq.ParquetFile(Path(source['folder']) / Path(item['path']).name).iter_batches(batch_size=1, columns=columns):
            yield decode(batch.to_pylist()[0])


def corpus_rows(source):
    import pyarrow.parquet as pq
    for item in source['shards']:
        for batch in pq.ParquetFile(Path(source['folder']) / Path(item['path']).name).iter_batches(batch_size=32):
            yield from batch.to_pylist()


def materialize(output, questions, documents, provenance, *, expected_questions=QUESTION_COUNT, expected_documents=DOCUMENT_COUNT):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / '.gitignore').write_text('*\n!.gitignore\n', encoding='utf-8')
    checkpoint = output / 'import.json'
    identity = dict(provenance=provenance, expected_questions=expected_questions, expected_documents=expected_documents)
    if checkpoint.exists() and json.loads(checkpoint.read_text(encoding='utf-8')) != identity:
        raise ValueError('Partial import belongs to another source')
    if not checkpoint.exists() and any(p.name not in ('.gitignore',) for p in output.iterdir()):
        raise ValueError('Import requires a new output or matching import checkpoint')
    atomic_json(checkpoint, identity)
    public, expectations = [], {}
    for row in questions:
        qid = docid(row['query_id']); identifier = 'RBCP_' + qid
        if identifier in expectations or not row['query'].strip() or not row['answer'].strip():
            raise ValueError('Duplicate/empty question')
        public.append(dict(id=identifier, kind='browsecomp_plus', prompt=row['query'], write_policy='readonly'))
        expectations[identifier] = {'reference': dict(benchmark='browsecomp-plus', query_id=qid, answer=row['answer'],
            evidence_docids=sorted({docid(d['docid']) for d in row['evidence_docs']}),
            gold_docids=sorted({docid(d['docid']) for d in row['gold_docs']}))}
    if len(public) != expected_questions:
        raise ValueError('Question count mismatch; full means no exclusions')
    root = output / 'knowledge' / 'documents'; root.mkdir(parents=True, exist_ok=True)
    entries = {}; total = 0
    for row in documents:
        did = docid(row['docid']); name = f'documents/{did}.md'
        if name in entries:
            raise ValueError('Duplicate corpus docid')
        text = row['text']
        if not isinstance(text, str) or '\x00' in text:
            raise ValueError('Corpus document is not supported UTF-8 text')
        raw = text.encode('utf-8'); digest = hashlib.sha256(raw).hexdigest()
        target = root / (did + '.md')
        if target.exists():
            if target.stat().st_size != len(raw) or sha256(target) != digest:
                raise ValueError('Existing document differs from pinned corpus')
        else:
            tmp = target.with_suffix('.part'); tmp.write_bytes(raw); tmp.replace(target)
        entries[name] = dict(path=name, bytes=len(raw), sha256=digest)
        total += len(raw)
        if len(entries) % 10000 == 0:
            print(json.dumps({'imported_documents': len(entries), 'bytes': total}), flush=True)
    if len(entries) != expected_documents:
        raise ValueError('Corpus count mismatch; do not publish partial corpus')
    if {p.name for p in root.iterdir()} != {Path(n).name for n in entries}:
        raise ValueError('Corpus directory has unexpected files')
    for ref in (v['reference'] for v in expectations.values()):
        if not ref['evidence_docids'] or not ref['gold_docids'] or any(f'documents/{did}.md' not in entries for did in ref['evidence_docids'] + ref['gold_docids']):
            raise ValueError('Missing official evidence/gold document')
    with (output / 'tasks.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=TASK_COLUMNS); writer.writeheader(); writer.writerows(public)
    atomic_json(output / 'expectations.json', expectations)
    # A small path/hash catalogue enables lazy loading; do not JSON-encode 3+ GiB of text.
    catalog = output / 'corpus-manifest.jsonl'
    with catalog.open('w', encoding='utf-8', newline='\n') as stream:
        for name in sorted(entries):
            stream.write(json.dumps(entries[name], separators=(',', ':')) + '\n')
    full = expected_questions == QUESTION_COUNT and expected_documents == DOCUMENT_COUNT
    manifest = dict(benchmark='browsecomp-plus', scope='full-corpus-platform-adaptation' if full else 'synthetic-full-import-fixture',
        official_leaderboard_comparable=False, questions=len(public), documents=len(entries), bytes=total,
        selection='ALL official questions and ALL documents; no negative sampling or development exclusions',
        corpus_format='path-sha256-v1', corpus_sha256=sha256(catalog), document_paths='documents/<original-docid>.md',
        source=provenance, tasks_file_sha256=sha256(output/'tasks.csv'), expectations_file_sha256=sha256(output/'expectations.json'))
    tasks(output)  # Validate task schema before publishing the manifest.
    atomic_json(output / 'manifest.json', manifest)
    return manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cache', type=Path, default=Path('data/browsecomp-full-downloads'))
    p.add_argument('--output', type=Path, default=Path('data/browsecomp-plus-full'))
    p.add_argument('--download-only', action='store_true')
    args = p.parse_args()
    sources = download_all(args.cache)
    if args.download_only:
        print(json.dumps({'state':'downloaded', 'shards':13}), flush=True); return
    manifest = materialize(args.output, question_rows(sources[0]), corpus_rows(sources[1]), sources)
    print(json.dumps({k:manifest[k] for k in ('scope','questions','documents','bytes','corpus_sha256')}), flush=True)


if __name__ == '__main__':
    main()
