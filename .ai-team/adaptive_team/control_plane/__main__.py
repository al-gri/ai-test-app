"""Run with python -m adaptive_team.control_plane; never enable reload/workers."""
import argparse
import json
import os
import secrets
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    keys=sub.add_parser('credentials');keys.add_argument('path',type=Path)
    serve=sub.add_parser('serve');serve.add_argument('--database',required=True,type=Path)
    serve.add_argument('--credentials',required=True,type=Path);serve.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    if args.command=='credentials':
        # O_EXCL prevents destroying credentials on a repeat invocation. On
        # Windows, inherit a private user-directory ACL; chmod is not an ACL tool.
        with os.fdopen(os.open(args.path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as out:
            json.dump({'actor':'local-owner','login_token':secrets.token_urlsafe(32),
                       'signing_key':secrets.token_hex(32)},out)
        print('Credentials written. Keep outside repositories and worker mounts; use login_token in the browser.')
        return
    value=json.loads(args.credentials.read_text(encoding='utf-8'))
    from .app import create_app
    import uvicorn
    app=create_app(args.database,login_token=value['login_token'],
        signing_key=bytes.fromhex(value['signing_key']),actor=value['actor'],port=args.port)
    telemetry=None
    if os.environ.get('AI_TEAM_OTEL')=='1':
        from ..observability.otel import Telemetry,configure
        telemetry=Telemetry.from_environment();configure(telemetry)
    try:
        uvicorn.run(app,host='127.0.0.1',port=args.port,workers=1,
                    proxy_headers=False,access_log=False,log_level='critical',limit_concurrency=64,
                    timeout_keep_alive=5)
    finally:
        if telemetry is not None:configure(None);telemetry.close()


if __name__=='__main__':main()
