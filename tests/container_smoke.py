"""Linux Docker smoke test with disposable demo data; no production directories."""
import errno
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def main():
    suffix = uuid.uuid4().hex[:12]
    name, volume, image = 'kb-smoke-'+suffix, 'kb-state-'+suffix, 'folder-kb-smoke:'+suffix
    token = secrets.token_urlsafe(32)
    try:
        docker('build', '-t', image, '.')
        docker('volume', 'create', volume)
        with tempfile.TemporaryDirectory(prefix='kb-smoke-') as directory:
            root = Path(directory)/'knowledge'
            (root/'term').mkdir(parents=True)
            (root/'term/README.md').write_text('container smoke knowledge\n', encoding='utf-8')
            docker('run', '-d', '--name', name, '--read-only', '--cap-drop=ALL',
                   '--security-opt=no-new-privileges:true', '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
                   '-p', '127.0.0.1::8790', '--mount', f'type=bind,source={root},target=/knowledge,readonly',
                   '--mount', f'type=volume,source={volume},target=/service-data',
                   '-e', 'KB_ADMIN_TOKEN='+token, '-e', 'KB_STATE_DIR=/service-data',
                   '-e', 'KB_ALLOWED_HOSTS=127.0.0.1,127.0.0.1:*', image)
            port = docker('port', name, '8790').rsplit(':', 1)[1]
            base = 'http://127.0.0.1:'+port
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def request(path, method='GET', body=None, credential=token):
                req = urllib.request.Request(base+path, method=method,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={'Authorization':'Bearer '+credential, 'Content-Type':'application/json'})
                with opener.open(req, timeout=5) as response:
                    return json.load(response)

            def wait_ready():
                for _ in range(60):
                    try:
                        request('/healthz');return
                    except (urllib.error.URLError, OSError):
                        time.sleep(.5)
                raise RuntimeError('Container did not become healthy')

            wait_ready()
            assert docker('exec', name, 'id', '-u') == '10001'
            assert docker('inspect', name, '--format', '{{.HostConfig.ReadonlyRootfs}}') == 'true'
            request('/admin/api/libraries', 'POST', {'id':'term','name':'Term','path':'term'})
            key = request('/admin/api/keys', 'POST', {'agent_name':'smoke-agent','library_ids':['term']})
            assert request('/api/v1/read?library_id=term&path=README.md', credential=key['token'])['lines'][0]['text']=='container smoke knowledge'
            docker('exec', name, 'python', '-c',
                   "from pathlib import Path\nimport errno\ntry:\n Path('/knowledge/write-must-fail.txt').write_text('x')\nexcept OSError as e:\n assert e.errno == errno.EROFS\nelse:\n raise AssertionError('Knowledge mount is writable')")
            assert not (root/'write-must-fail.txt').exists()
            docker('restart', name)
            wait_ready()
            assert request('/api/v1/read?path=README.md', credential=key['token'])['library_id']=='term'
            request('/admin/api/keys/'+key['key']['id']+'/revoke', 'POST')
            try:
                request('/api/v1/info', credential=key['token'])
            except urllib.error.HTTPError as exc:
                assert exc.code==401
            else:
                raise AssertionError('Revoked key still works')
            print('PASS: non-root container, read-only knowledge mount, admin/key isolation, persistent restart, revocation')
    finally:
        # Only identifiers created by this smoke test are removed.
        subprocess.run(['docker','rm','-f',name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker','volume','rm',volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker','image','rm',image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
