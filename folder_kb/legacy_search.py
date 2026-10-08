"""Compatibility backend for declarative v1 plugins, not platform search policy."""
import re
import unicodedata
from .errors import KnowledgeError

ENGINE = 'sqlite-fts5-bm25-v1'

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


def build(db, files, index, max_chunks):
    db.execute('CREATE TABLE chunks(id INTEGER PRIMARY KEY,path TEXT,start_line INTEGER,end_line INTEGER,title TEXT,text TEXT,sha256 TEXT,version TEXT)')
    db.execute('CREATE VIRTUAL TABLE terms USING fts5(title,body)')
    count = 0
    for path, text, sha, version in files:
        for first, last, title, body in chunks(text, index['splitter']):
            if not body.strip():
                continue
            count += 1
            if count > max_chunks:
                raise KnowledgeError('index_limit', 'Too many chunks; narrow the mount', 413)
            db.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)', (count,path,first,last,title,body,sha,version))
            db.execute('INSERT INTO terms(rowid,title,body) VALUES(?,?,?)', (count, ' '.join(tokens(title)), ' '.join(tokens(body))))
    return count


def search(db, query, limit, path):
    terms = list(dict.fromkeys(tokens(query)))
    if not terms:
        raise KnowledgeError('invalid_search', 'Query must contain Chinese characters or English words/numbers', 422)
    rows = db.execute("SELECT c.*,bm25(terms,2.0,1.0) AS rank FROM terms JOIN chunks c ON c.id=terms.rowid "
        "WHERE terms MATCH ? AND (?='.' OR substr(c.path,1,length(?)+1)=?||'/') ORDER BY rank,c.id LIMIT ?",
        (' OR '.join('"' + term + '"' for term in terms), path, path, path, limit+1)).fetchall()
    return rows[:limit], len(rows)>limit
