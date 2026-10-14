param(
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $environmentPython = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $environmentPython) {
        $PythonPath = $environmentPython
    } else {
        $pythonCommand = Get-Command python -ErrorAction Stop
        $PythonPath = $pythonCommand.Source
    }
} else {
    $PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python executable was not found: $PythonPath"
}

& $PythonPath -m pytest --version
if ($LASTEXITCODE -ne 0) {
    throw "pytest is missing from $PythonPath; install the project developer dependencies first."
}

$buildDirectory = Join-Path $repositoryRoot "build"
New-Item -ItemType Directory -Path $buildDirectory -Force | Out-Null
$runId = [guid]::NewGuid().ToString("N")
$baseTemp = Join-Path $buildDirectory ("pytest-bounded-" + $runId)
$stdoutPath = Join-Path $buildDirectory ("pytest-bounded-" + $runId + ".stdout.log")
$stderrPath = Join-Path $buildDirectory ("pytest-bounded-" + $runId + ".stderr.log")

$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $PythonPath
$startInfo.Arguments = '-m pytest -p no:cacheprovider --basetemp "{0}"' -f $baseTemp
$startInfo.WorkingDirectory = $repositoryRoot
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$process = New-Object System.Diagnostics.Process
$process.StartInfo = $startInfo
[void]$process.Start()
$stdoutTask = $process.StandardOutput.ReadToEndAsync()
$stderrTask = $process.StandardError.ReadToEndAsync()

$timedOut = -not $process.WaitForExit(50000)
if ($timedOut) {
    & taskkill.exe /PID $process.Id /T /F *> $null
    if (-not $process.WaitForExit(3000)) {
        try { $process.Kill() } catch { }
        [void]$process.WaitForExit(1000)
    }
}
$stdout = $stdoutTask.GetAwaiter().GetResult()
$stderr = $stderrTask.GetAwaiter().GetResult()
Set-Content -LiteralPath $stdoutPath -Value $stdout -Encoding utf8
Set-Content -LiteralPath $stderrPath -Value $stderr -Encoding utf8
Write-Output $stdout
Write-Output $stderr

if ($timedOut) {
    Write-Output "pytest exceeded the 55-second limit; test process tree was stopped."
    exit 124
}
if (-not $process.HasExited) {
    Write-Output "pytest process did not exit after the bounded stop attempt."
    exit 125
}
if ($process.ExitCode -ne 0) {
    Write-Output "pytest failed with exit code $($process.ExitCode)."
    exit $process.ExitCode
}

exit 0
