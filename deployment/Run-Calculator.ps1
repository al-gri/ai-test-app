$ErrorActionPreference = 'Stop'
$env:DOCKER_HOST = 'tcp://localhost:2375'
$composeExe = Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker-compose.exe'
Get-Content -Raw (Join-Path $PSScriptRoot 'compose.yaml') | & $composeExe -p ai-test-app -f - run --rm runner
if ($LASTEXITCODE -ne 0) { throw 'Run stopped. Inspect dashboard; do not reset state or retry unknown calls.' }
