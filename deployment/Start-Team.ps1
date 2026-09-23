$ErrorActionPreference = 'Stop'
$env:DOCKER_HOST = 'tcp://localhost:2375'
$composeExe = Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker-compose.exe'
Get-Content -Raw (Join-Path $PSScriptRoot 'compose.yaml') | & $composeExe -p ai-test-app -f - up -d dashboard watchdog
if ($LASTEXITCODE -ne 0) { throw 'Service startup failed' }
