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

$runnerPath = Join-Path $repositoryRoot "scripts\run_tests_sharded.py"

Push-Location $repositoryRoot
try {
    & $PythonPath $runnerPath --python $PythonPath
    if ($LASTEXITCODE -ne 0) {
        Write-Output "pytest failed with exit code $LASTEXITCODE."
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}

exit 0
