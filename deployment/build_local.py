"""Stream an explicit offline build context; no Docker directory traversal."""
import io
import os
import subprocess
import tarfile
from pathlib import Path

root = Path(__file__).parent
docker = Path(os.environ['LOCALAPPDATA']) / 'Programs/DockerDesktop/resources/bin/docker.exe'
context = io.BytesIO()
with tarfile.open(fileobj=context, mode='w:gz', format=tarfile.GNU_FORMAT) as archive:
    for folder in ('ops', 'wheels'):
        for path in sorted((root / folder).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts:
                archive.add(path, arcname='Dockerfile' if path.name == 'Dockerfile' else path.relative_to(root).as_posix(), recursive=False)
result = subprocess.run([str(docker), '--config', str(root / 'ops/docker-config'), '--host', 'tcp://localhost:2375', 'build',
    '--network', 'none', '--pull=false', '-t', 'ai-test-app-control:1.1.1',
    '-'], input=context.getvalue(), check=False)
raise SystemExit(result.returncode)
