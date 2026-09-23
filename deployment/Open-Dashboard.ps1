$ErrorActionPreference = 'Stop'
$dockerExe = Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker.exe'
# Keep the login token out of command arguments, URLs, console and shell history.
$loginToken = & $dockerExe --host tcp://localhost:2375 exec ai-test-app-dashboard-1 python -c "import json; print(json.load(open('/credentials/dashboard.json'))['login_token'])"
if ($LASTEXITCODE -ne 0) { throw 'Dashboard is not running' }
Set-Clipboard -Value $loginToken
Remove-Variable loginToken
Start-Process 'http://127.0.0.1:8765'
Write-Host 'Login token copied to clipboard. Paste it into the dashboard login field.'
