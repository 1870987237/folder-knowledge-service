"""Document BM25 baseline using disk-backed SQLite FTS5, independent of the dataset."""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import unicodedata

ENGINE = 'sqlite-corpus-fts5-porter-bm25-v1'
STOP = set('a an and are as at be been being but by can could did do does for from had has have how i if in into is it its may more most of on or our should than that the their them then there these they this those to was we were what when where which who whom why will with would you your'.split())


def terms(query):
    words = re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', query).lower(), re.UNICODE)
    # This policy is part of this plugin, not platform behavior or answer labels.
    return list(dict.fromkeys(w for w in words if w not in STOP))[:256]


def build(request, output):
    folder=output/'index'; folder.mkdir()
    with sqlite3.connect(folder/'search.sqlite3') as db:
        db.execute('PRAGMA journal_mode=OFF')
        db.execute('PRAGMA synchronous=OFF')
        db.execute('PRAGMA cache_size=-65536')
        db.execute('CREATE TABLE documents(id INTEGER PRIMARY KEY,path TEXT UNIQUE,body TEXT)')
        db.execute("CREATE VIRTUAL TABLE terms USING fts5(body,content='documents',content_rowid='id',tokenize='porter unicode61')")
        count=0
        with Path(request['documents_path']).open(encoding='utf-8') as stream:
            for line in stream:
                item=json.loads(line); count+=1
                db.execute('INSERT INTO documents VALUES(?,?,?)',(count,item['path'],item['text']))
                db.execute('INSERT INTO terms(rowid,body) VALUES(?,?)',(count,item['text']))
                if count%1000==0:
                    db.commit()
        db.commit()
        db.execute("INSERT INTO terms(terms) VALUES('optimize')")
        db.commit()
    return dict(protocol_version='1.1',engine=ENGINE,chunks=count)


def ranking(db, query, limit, path='.'):
    words=terms(query)
    if not words:
        return []
    expression=' OR '.join('"'+w.replace('"','""')+'"' for w in words)
    return db.execute('SELECT d.id,d.path,terms.rank AS score FROM terms JOIN documents d ON d.id=terms.rowid '
        "WHERE terms MATCH ? AND (?='.' OR substr(d.path,1,length(?)+1)=?||'/') ORDER BY terms.rank LIMIT ?",
        (expression,path,path,path,limit)).fetchall()


def excerpt(body, query):
    """Pick a verbatim, bounded window near a matching term, preserving line numbers."""
    words=terms(query)
    pattern=re.compile('|'.join(re.escape(w) for w in words),re.IGNORECASE) if words else None
    # Prefer lines covering several distinct query terms; no task annotations.
    best=None; offset=0
    for number,line in enumerate(body.splitlines(keepends=True),1):
        hits=list(pattern.finditer(line)) if pattern else []
        if hits:
            score=len({m.group().lower() for m in hits})
            if best is None or score>best[0]:
                best=(score,offset,number,hits[0].start())
        offset+=len(line)
    begin=max(0,best[1]+best[3]-200) if best else 0
    piece=body[begin:begin+1200]
    # Count CR, LF and Unicode line separators the same way as platform validation.
    first=len(body[:begin].splitlines()) or 1
    if begin and body[:begin].endswith(('\n','\r','\x85','\u2028','\u2029')):
        first+=1
    last=first+max(0,len(piece.splitlines())-1)
    return first,last,piece


def search(request):
    path=Path(request['index_dir'])/'search.sqlite3'
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        limit=request['limit']
        if type(limit) is not int or not 1<=limit<=1000:
            raise ValueError('Limit must be in 1..1000')
        rows=ranking(db,request['query'],limit+1,request.get('path','.'))
        if request['operation']=='rank':
            return dict(protocol_version='1.1',engine=ENGINE,results=[dict(path=r['path'],score=-r['score']) for r in rows[:limit]])
        hits=[]
        for row in rows[:limit]:
            body=db.execute('SELECT body FROM documents WHERE id=?',(row['id'],)).fetchone()[0]
            first,last,text=excerpt(body,request['query'])
            if text:
                hits.append(dict(path=row['path'],start_line=first,end_line=last,text=text,title='',score=-row['score']))
        return dict(protocol_version='1.1',results=hits,has_more=len(rows)>limit)


def main():
    p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();request=json.loads(a.input.read_text(encoding='utf-8'))
    if request['protocol_version']!='1.1' or request['operation'] not in ('build','search','rank'):
        raise ValueError('Unsupported operation/protocol')
    result=build(request,a.output) if request['operation']=='build' else search(request)
    (a.output/'result.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')


if __name__=='__main__':
    main()
