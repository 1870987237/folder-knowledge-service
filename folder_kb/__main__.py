import argparse
import os

import uvicorn

from .server import Settings, create_app


def main():
    parser = argparse.ArgumentParser(description='Read-only knowledge folder via REST and MCP')
    parser.add_argument('--root', default=os.getenv('KB_ROOT'))
    parser.add_argument('--host', default=os.getenv('KB_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('KB_PORT', '8790')))
    args = parser.parse_args()
    if not args.root:
        parser.error('Provide --root or KB_ROOT')
    os.environ['KB_ROOT'] = args.root
    settings = Settings.from_env()
    if args.host not in ('127.0.0.1', 'localhost', '::1'):
        if not settings.token or not os.getenv('KB_ALLOWED_HOSTS'):
            parser.error('Remote binding requires KB_TOKEN and explicit KB_ALLOWED_HOSTS')
    uvicorn.run(create_app(settings), host=args.host, port=args.port,
                access_log=False, limit_concurrency=32, timeout_keep_alive=10)


if __name__ == '__main__':
    main()
