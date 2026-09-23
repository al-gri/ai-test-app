$ErrorActionPreference = 'Stop'
$env:DOCKER_HOST = 'tcp://localhost:2375'
$composeExe = Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker-compose.exe'
# Stop only the persistent UI/watchdog services. Preserve volumes and run history.
# Use after the runner has stopped; watchdog must remain alive during sandbox work.
Get-Content -Raw (Join-Path $PSScriptRoot 'compose.yaml') | & $composeExe -p ai-test-app -f - stop dashboard watchdog
