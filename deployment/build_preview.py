"""Build an app-only image from the accepted exported tree, never a worktree."""
import io
import os
from pathlib import Path
import subprocess
import tarfile

root=Path(__file__).resolve().parent
docker=str(Path(os.environ['LOCALAPPDATA'])/'Programs/DockerDesktop/resources/bin/docker.exe')
dockerfile=b'''FROM sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
COPY app/ /app/
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
CMD ["python", "/app/server.py", "--host", "0.0.0.0", "--port", "8080"]
'''
stream=io.BytesIO()
with tarfile.open(fileobj=stream,mode='w:gz',format=tarfile.GNU_FORMAT) as archive:
    item=tarfile.TarInfo('Dockerfile');item.size=len(dockerfile)
    archive.addfile(item,io.BytesIO(dockerfile))
    for path in sorted((root/'repository/app').iterdir()):
        if path.is_symlink() or not path.is_file(): raise ValueError('Unexpected app entry')
        archive.add(path,arcname='app/'+path.name,recursive=False)
subprocess.run([docker,'--config',str(root/'ops/docker-config'),'--host','tcp://localhost:2375',
    'build','--network','none','--pull=false','-t','ai-test-app-calculator:verified','-'],
    input=stream.getvalue(),check=True)
