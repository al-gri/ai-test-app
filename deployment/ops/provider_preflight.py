"""Read-only provider capability check. Never prints key or response bodies."""
import json
from pathlib import Path
import httpx

key=Path('/credentials/deepseek.key').read_text().strip()
try:
    with httpx.Client(timeout=15,follow_redirects=False,trust_env=False) as client:
        result=client.get('https://api.deepseek.com/models',headers={'Authorization':'Bearer '+key})
    valid=result.status_code==200 and any(x.get('id')=='deepseek-flash' for x in result.json().get('data',[]))
    print(json.dumps({'http_status':result.status_code,'deepseek_flash_available':valid}))
    raise SystemExit(0 if valid else 1)
except httpx.HTTPError:
    print('Provider connectivity check failed; no generation requested.')
    raise SystemExit(1)
