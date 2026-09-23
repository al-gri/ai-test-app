"""Copy only an accepted Git export to this workspace, rejecting special paths."""
import io
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile

root=Path(__file__).resolve().parent
destination=root/'repository'
docker=str(Path(os.environ['LOCALAPPDATA'])/'Programs/DockerDesktop/resources/bin/docker.exe')
result=subprocess.run([docker,'--config',str(root/'ops/docker-config'),'--host','tcp://localhost:2375',
    'run','--rm','--network','none','--mount','type=volume,source=ai-test-app_work,target=/work,readonly',
    'ai-test-app-control:1.1.1','cat','/work/verified-project.tar'],capture_output=True,check=True)
if len(result.stdout)>20_000_000: raise ValueError('Oversized export')
with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
    members=archive.getmembers()
    if len(members)>2000: raise ValueError('Too many export entries')
    for entry in members:
        path=PurePosixPath(entry.name)
        if path.is_absolute() or '..' in path.parts or '.git' in path.parts or '\\' in entry.name:
            raise ValueError('Unsafe export path')
        if not (entry.isfile() or entry.isdir()): raise ValueError('Links/special files refused')
        if not (destination/entry.name).resolve().is_relative_to(destination.resolve()):
            raise ValueError('Export escapes destination')
    destination.mkdir(exist_ok=False)
    archive.extractall(destination,filter='data')
print(f'Exported {sum(m.isfile() for m in members)} verified files to {destination}')
