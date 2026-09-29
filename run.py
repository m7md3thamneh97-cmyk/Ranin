"""Local-only launcher. Access tokens are printed once; no tokens are committed."""
import argparse
import os
from pathlib import Path
import uvicorn
from studio.app import create_app


def main():
    parser = argparse.ArgumentParser(description='Raneen Teaching Studio — local prototype')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', default=os.environ.get('RANEEN_DATA_DIR', './data'))
    parser.add_argument('--new-admin', metavar='NAME', help='Create an admin token and exit (local recovery).')
    args = parser.parse_args()
    app = create_app(Path(args.data_dir))
    store = app.state.store
    if args.new_admin:
        user = store.create_user(args.new_admin, 'admin')
        print(f"New admin token (store privately): {user['token']}")
        return
    if not store.one("SELECT id FROM users WHERE role='admin' LIMIT 1"):
        user = store.create_user('Workspace owner', 'admin')
        print(f"\nFIRST-RUN ADMIN TOKEN (shown once):\n{user['token']}\n", flush=True)
    print(f'Open http://localhost:{args.port}\nLocal prototype only. No external AI services are connected.', flush=True)
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False, proxy_headers=False)

if __name__ == '__main__':
    main()
