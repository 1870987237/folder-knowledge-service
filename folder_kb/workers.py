"""Read-only operational view. Does not own queues or launch processes."""
from .worker_runtime import HEARTBEATS, liveness


def worker_status(registry):
    with registry.store.transaction() as db:
        heartbeats = dict(db.execute('SELECT name,value FROM metadata WHERE name IN (?,?)', tuple(HEARTBEATS.values())))
        workers = []
        for kind, name, table, waiting, running in (
            ('git', 'Git 同步', 'repository_jobs', ('queued',), ('running',)),
            ('updates', '知识更新', 'update_jobs', ('queued', 'publish_queued'), ('running', 'publishing')),
        ):
            counts = dict(db.execute(f'SELECT status,count(*) FROM {table} GROUP BY status'))
            if kind == 'git':
                rows = db.execute('''SELECT j.id,j.repository_id AS resource_id,r.name AS resource_name,j.error,
                    j.finished_at AS failed_at FROM repository_jobs j JOIN repositories r ON r.id=j.repository_id
                    WHERE j.status='failed' ORDER BY j.finished_at DESC,j.id DESC LIMIT 5''').fetchall()
            else:
                rows = db.execute('''SELECT j.id,j.library_id AS resource_id,l.name AS resource_name,
                    j.error_code AS error,j.updated_at AS failed_at FROM update_jobs j
                    JOIN libraries l ON l.id=j.library_id WHERE j.status='failed'
                    ORDER BY j.updated_at DESC,j.id DESC LIMIT 5''').fetchall()
            workers.append(dict(id=kind, name=name, **liveness(heartbeats.get(HEARTBEATS[kind])),
                queued=sum(counts.get(s, 0) for s in waiting), running=sum(counts.get(s, 0) for s in running),
                awaiting_review=counts.get('candidate_ready', 0), failed=counts.get('failed', 0),
                recent_failures=[dict(row) for row in rows]))
    return {'workers': workers}


def add_worker_routes(app, registry):
    @app.get('/admin/api/workers')
    def status():
        return worker_status(registry)
