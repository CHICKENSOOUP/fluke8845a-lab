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

$runId = [guid]::NewGuid().ToString("N")
$testArtifactsDirectory = Join-Path ([System.IO.Path]::GetTempPath()) "Fluke8845A_Test_Artifacts"
$runDirectory = Join-Path $testArtifactsDirectory ("pytest-bounded-" + $runId)
New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null
$stdoutPath = Join-Path $runDirectory "runner.stdout.log"
$stderrPath = Join-Path $runDirectory "runner.stderr.log"

$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $PythonPath
$runnerPath = Join-Path $repositoryRoot "scripts\run_tests_sharded.py"
$shardLogRoot = Join-Path $runDirectory "pytest-shards"
$startInfo.Arguments = '"{0}" --python "{1}" --timeout-seconds 45 --output-root "{2}"' -f $runnerPath, $PythonPath, $shardLogRoot
$startInfo.WorkingDirectory = $repositoryRoot
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$pythonPathEntries = @((Join-Path $repositoryRoot "src"))
if (-not [string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $pythonPathEntries += $env:PYTHONPATH
}
$startInfo.EnvironmentVariables["PYTHONPATH"] = $pythonPathEntries -join [System.IO.Path]::PathSeparator
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
    Write-Output "The complete pytest shard suite exceeded the 50-second wrapper limit; test process tree was stopped."
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
