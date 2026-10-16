"""Standalone reference plugin; requires only Python stdlib + SQLite FTS5."""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import unicodedata

def tokens(text):
    """English identifiers + Chinese adjacent character pairs; no external model."""
    parts = re.findall(r'[a-z0-9_]+|[\u3400-\u9fff]+', unicodedata.normalize('NFKC', text).lower())
    result = []
    for part in parts:
        if '\u3400' <= part[0] <= '\u9fff' and len(part) > 1:
            result.extend(part[i:i + 2] for i in range(len(part) - 1))
        else:
            result.append(part)
    return result


def chunks(text, splitter):
    """Source-preserving blocks with line citations, bounded to 1200 characters."""
    block, size, start, title, fence = [], 0, 1, '', False
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        heading = re.match(r'^#{1,6}\s+(.+)', line) if not fence and splitter == 'markdown_sections' else None
        if block and (heading or size + len(line) > 1200):
            yield start, number - 1, title, ''.join(block)
            block, size = [], 0
        if heading:
            title = heading.group(1).strip()
        if line.lstrip().startswith(('```', '~~~')):
            fence = not fence
        # Long single lines still fit the result budget; each fragment cites that line.
        for offset in range(0, len(line), 1200):
            part = line[offset:offset + 1200]
            if not block:
                start = number
            block.append(part)
            size += len(part)
            if size >= 1200:
                yield start, number, title, ''.join(block)
                block, size = [], 0
    if block:
        yield start, number, title, ''.join(block)


def build(request, output):
    folder = output / 'index'
    folder.mkdir()
    with sqlite3.connect(folder / 'search.sqlite3') as db:
        db.execute('CREATE TABLE chunks(id INTEGER PRIMARY KEY,path TEXT,start_line INTEGER,end_line INTEGER,title TEXT,text TEXT)')
        db.execute('CREATE VIRTUAL TABLE terms USING fts5(title,body)')
        count = 0
        for document in request['documents']:
            for first, last, title, body in chunks(document['text'], 'markdown_sections'):
                if not body.strip():
                    continue
                count += 1
                if count > 16000:
                    raise ValueError('Reference plugin supports at most 16000 chunks')
                db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?)', (count, document['path'], first, last, title[:300], body))
                db.execute('INSERT INTO terms(rowid,title,body) VALUES(?,?,?)', (count, ' '.join(tokens(title)), ' '.join(tokens(body))))
    return {'protocol_version': '1.0', 'engine': 'bm25-markdown-v1', 'chunks': count}


def search(request):
    terms = list(dict.fromkeys(tokens(request['query'])))
    if not terms:
        return {'protocol_version': '1.0', 'results': [], 'has_more': False}
    path, limit = request['path'], request['limit']
    db = sqlite3.connect((Path(request['index_dir']) / 'search.sqlite3').as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT c.*,bm25(terms,2.0,1.0) AS rank FROM terms JOIN chunks c ON c.id=terms.rowid "
            "WHERE terms MATCH ? AND (?='.' OR substr(c.path,1,length(?)+1)=?||'/') ORDER BY rank,c.id LIMIT ?",
            (' OR '.join('"'+term+'"' for term in terms), path, path, path, limit+1)).fetchall()
        return {'protocol_version': '1.0', 'has_more': len(rows)>limit,
                'results': [dict(path=r['path'], start_line=r['start_line'], end_line=r['end_line'],
                    title=r['title'], text=r['text'], score=-r['rank']) for r in rows[:limit]]}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.input.read_text(encoding='utf-8'))
    if request['protocol_version'] != '1.0':
        raise ValueError('Unsupported protocol')
    if request['operation'] == 'build':
        result = build(request, args.output)
    elif request['operation'] == 'search':
        result = search(request)
    else:
        raise ValueError('Unsupported operation')
    (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
