"""Isolated synthetic browser fixture. Never targets existing knowledge or state."""
import json
import uuid
from pathlib import Path

from folder_kb.management import Registry
from folder_kb.collaboration import Collaboration


def create_fixture():
    base = Path('data').resolve() / ('human-ui-' + uuid.uuid4().hex[:8])
    root = base / 'knowledge'
    root.mkdir(parents=True)
    registry = Registry(str(root), str(base / 'state'))
    collaboration = Collaboration(registry)
    password = 'Workspace-Demo-2026!'
    for name in ('demo.editor', 'demo.reviewer'):
        collaboration.register(name, password, 'fixture')
    with registry.store.transaction(write=True) as db:
        db.execute("UPDATE users SET status='active'")
        users = {r['username']: r['id'] for r in db.execute('SELECT * FROM users')}
        db.execute("INSERT INTO organizations VALUES ('demo-team','演示团队')")
        db.execute("INSERT INTO organization_members VALUES ('demo-team',?,'admin')", (users['demo.reviewer'],))
        db.execute("INSERT INTO organization_members VALUES ('demo-team',?,'member')", (users['demo.editor'],))
    reviewer = {'id': users['demo.reviewer']}
    lid = collaboration.create_library(reviewer, '团队使用手册', 'demo-team')['id']
    with registry.store.transaction(write=True) as db:
        db.execute("INSERT INTO library_members VALUES (?,?,'editor')", (lid, users['demo.editor']))
    library = registry.libraries()[0]
    documents = root / library['path']
    (documents / '指南' / '进阶').mkdir(parents=True)
    (documents / '指南' / '进阶' / '操作.md').write_text('# 操作指南\n\n中文内容。\n\n```python\nprint("hello")\n```\n', encoding='utf-8')
    (documents / '元数据.md').write_text('---\ntitle: 保留元数据\n---\n\n# 原始标题\n\n<div>保留原文</div>\n', encoding='utf-8')
    (root / library['path'] / 'README.md').write_text('# 团队使用手册\n\n## 使用步骤\n\n1. 阅读知识库。\n2. 保存草稿，交给发布者审核。\n\n| 角色 | 权限 |\n|---|---|\n| 编辑者 | 编写草稿 |\n| 发布者 | 审核发布 |\n', encoding='utf-8')
    config = {'root': str(root), 'state': str(base / 'state'), 'port': 8798,
              'admin_token': 'isolated-human-workspace-test-admin',
              'editor': 'demo.editor', 'reviewer': 'demo.reviewer', 'password': password}
    (base / 'fixture.json').write_text(json.dumps(config), encoding='utf-8')
    print(base)


if __name__ == '__main__':
    create_fixture()
