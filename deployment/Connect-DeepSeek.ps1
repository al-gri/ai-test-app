$ErrorActionPreference = 'Stop'
$dockerExe = Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin/docker.exe'
$secretValue = Read-Host 'Enter DeepSeek API key (hidden; never sent to chat or GitHub)' -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secretValue)
try {
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $dockerExe
    $start.Arguments = '--host tcp://localhost:2375 run --rm -i --network none --mount type=volume,source=ai-test-app_credentials,target=/credentials ai-test-app-control:1.1.1 python /opt/deployment/deployment.py set-key'
    $start.UseShellExecute = $false
    $start.RedirectStandardInput = $true
    $start.CreateNoWindow = $true
    $process = [Diagnostics.Process]::Start($start)
    $process.StandardInput.WriteLine([Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer))
    $process.StandardInput.Close()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw 'Key provisioning failed' }
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    $secretValue.Dispose()
}
