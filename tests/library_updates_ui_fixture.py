"""Disposable, synthetic UI fixture. Never opens an existing state directory."""
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from folder_kb.server import Settings, create_app
from folder_kb.template_package import build_package
from tests.test_collaboration import ADMIN, account, headers, organization


def main():
    base = Path('data/library-updates-ui-20261013').resolve()
    if base.exists():
        raise SystemExit('Fixture already exists; refusing to overwrite it')
    (base / 'root').mkdir(parents=True)
    app = create_app(Settings(str(base / 'root'), state_dir=str(base / 'state'), admin_token=ADMIN,
                              allowed_hosts=('testserver', '127.0.0.1', 'localhost')))
    with TestClient(app) as client:
        uid, _ = account(client, 'owner')
        oid = organization(client, 'owner', '模板更新演示组织')
        definition = json.loads(Path('examples/template-platform-config.json').read_text(encoding='utf-8'))
        archive = build_package(Path('examples/templates/reference-docs'))
        definition['source'] = {'archive_sha256': hashlib.sha256(archive).hexdigest()}
        assert client.post('/admin/api/templates', headers=headers(ADMIN), json=definition).status_code == 201
        assert client.put('/admin/api/templates/reference-docs/versions/0.2.0/package', headers=headers(ADMIN), content=archive).status_code == 200
        assert client.put('/admin/api/resource-access/plugin/reference-docs@0.2.0', headers=headers(ADMIN),
                          json={'approved': True, 'organization_ids': [oid], 'user_ids': [uid]}).status_code == 200
    print('Created synthetic UI fixture under data/library-updates-ui-20261013')


if __name__ == '__main__':
    main()
