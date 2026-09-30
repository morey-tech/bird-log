import argparse
import json
import logging
import urllib.request

from .config import Config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['run', 'health', 'seed'], nargs='?', default='run')
    parser.add_argument('--directory', default='./data/web-demo')
    args = parser.parse_args()
    config = Config.from_env()
    if args.command == 'health':
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{config.port}/healthz', timeout=3) as response:
                raise SystemExit(0 if json.load(response).get('alive') else 1)
        except (OSError, ValueError):
            raise SystemExit(1)
    if args.command == 'seed':
        from .seed import seed
        seed(args.directory)
        return
    logging.basicConfig(level=config.log_level, format='%(message)s')
    import uvicorn
    from .app import create_app
    uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1, access_log=False, proxy_headers=False, limit_concurrency=64)


if __name__ == '__main__':
    main()
