param(
    [switch]$SideBySide,
    [switch]$DesktopOnly,
    [string]$BuildName,
    [string]$PythonPath,
    [string]$PortablePythonPath
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "scripts\build_path_helpers.ps1")
$previousLocation = Get-Location
$previousPythonPath = $env:PYTHONPATH
$previousNuitkaCache = $env:NUITKA_CACHE_DIR
$previousConfigDir = $env:FLUKE8845A_CONFIG_DIR
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
$smokeConfigDirectory = $null

function Wait-SoftwareSmokeProcess {
    param(
        [Parameter(Mandatory)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory)][string]$Description
    )

    try {
        Wait-Process -Id $Process.Id -Timeout 300 -ErrorAction Stop | Out-Null
    } catch {
        if (Get-Process -Id $Process.Id -ErrorAction SilentlyContinue) {
            Stop-Process -Id $Process.Id -Force
        }
        throw "$Description exceeded the 5-minute software smoke watchdog."
    }

    $Process.Refresh()
    if ($Process.ExitCode -ne 0) {
        throw "$Description failed with exit code $($Process.ExitCode)."
    }
}

try {
    Set-Location -LiteralPath $PSScriptRoot
    if ([string]::IsNullOrWhiteSpace($previousNuitkaCache)) {
        $nuitkaCacheDirectory = Join-Path $PSScriptRoot "build\nuitka-cache"
    } else {
        $nuitkaCacheDirectory = [System.IO.Path]::GetFullPath($previousNuitkaCache)
    }
    $distRoot = Join-Path $PSScriptRoot "dist"
    $standardExe = Join-Path $distRoot "Fluke8845A.dist\Fluke8845A.exe"
    if ($SideBySide) {
        if ([string]::IsNullOrWhiteSpace($BuildName)) {
            $BuildName = "Fluke8845A_SideBySide_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
        }
        if ($BuildName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$' -or $BuildName.Contains("..")) {
            throw "BuildName must be a short Windows-safe name without path separators."
        }
        $candidateBuildName = $BuildName
        $outputDirectory = Join-Path $distRoot $BuildName
        $portableArchive = Join-Path $distRoot ($BuildName + "_Windows_x64.zip")
        $sourceArchive = Join-Path $distRoot ($BuildName + "_Source_Project.zip")
        if (Test-Path -LiteralPath $outputDirectory) {
            throw "Side-by-side build directory already exists; choose a new BuildName: $outputDirectory"
        }
        if (Test-Path -LiteralPath $portableArchive) {
            throw "Refusing to overwrite an existing portable archive: $portableArchive"
        }
        if (Test-Path -LiteralPath $sourceArchive) {
            throw "Refusing to overwrite an existing source archive: $sourceArchive"
        }
    } else {
        if (-not [string]::IsNullOrWhiteSpace($BuildName)) {
            throw "Use -BuildName together with -SideBySide."
        }
        $activeStandardBuild = Get-Process -Name "Fluke8845A" -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -and $_.Path -ieq $standardExe }
        if ($activeStandardBuild) {
            throw "The standard EXE is running and locked. Save the experiment, close it, or rebuild safely with -SideBySide."
        }
        $outputDirectory = $distRoot
        $candidateBuildName = "Fluke8845A"
        $portableArchive = Join-Path $distRoot "Fluke8845A_Windows_x64.zip"
        $sourceArchive = Join-Path $distRoot "Fluke8845A_Source_Project.zip"
    }
    $exeDist = Join-Path $outputDirectory "Fluke8845A.dist"
    $env:TEMP = Join-Path $PSScriptRoot "build\windows-temp"
    $env:TMP = $env:TEMP
    New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null

    if ([string]::IsNullOrWhiteSpace($PythonPath)) {
        $python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    } else {
        $python = (Resolve-Path -LiteralPath $PythonPath).Path
    }
    if (-not (Test-Path -LiteralPath $python)) {
        throw "The selected Python environment was not found: $python"
    }
    & $python (Join-Path $PSScriptRoot "scripts\validate_build_environment.py") `
        --lock (Join-Path $PSScriptRoot "requirements-lock.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "The selected Python environment does not match requirements-lock.txt; install the locked dependencies before packaging."
    }
    $pythonSitePackagesHex = (& $python -c "import sysconfig; print(sysconfig.get_paths()['purelib'].encode('utf-8').hex())").Trim()
    $pythonSitePackages = ConvertFrom-FlukeUtf8Hex -Hex $pythonSitePackagesHex
    $env:PYTHONPATH = (Join-Path $PSScriptRoot "src") + ";" + $pythonSitePackages

    & $python (Join-Path $PSScriptRoot "scripts\create_app_icon_ico.py")
    if ($LASTEXITCODE -ne 0) { throw "Application icon generation failed." }

    $provenanceRunId = [guid]::NewGuid().ToString("N")
    $sourceDigestPath = Join-Path $PSScriptRoot ("build\source-tree-" + $provenanceRunId + ".sha256")
    $provenancePath = Join-Path $PSScriptRoot ("build\BUILD-PROVENANCE-" + $provenanceRunId + ".json")
    & $python (Join-Path $PSScriptRoot "scripts\build_provenance.py") digest `
        --root $PSScriptRoot `
        --output $sourceDigestPath
    if ($LASTEXITCODE -ne 0) { throw "Build provenance source-tree capture failed." }

    $pytestRunId = [guid]::NewGuid().ToString("N")
    $pytestArtifactRoot = Join-Path ([System.IO.Path]::GetTempPath()) "Fluke8845A_Test_Artifacts"
    $pytestRunDirectory = Join-Path $pytestArtifactRoot ("pytest-build-" + $pytestRunId)
    New-Item -ItemType Directory -Path $pytestRunDirectory -Force | Out-Null
    $pytestRunner = Join-Path $PSScriptRoot "scripts\run_tests_sharded.py"
    & $python $pytestRunner --python $python --timeout-seconds 600 --output-root (Join-Path $pytestRunDirectory "pytest-shards")
    if ($LASTEXITCODE -ne 0) { throw "pytest failed with exit code $LASTEXITCODE; EXE packaging stopped." }
    if (-not $DesktopOnly) {
        & $python (Join-Path $PSScriptRoot "scripts\run_web_tests.py")
        if ($LASTEXITCODE -ne 0) { throw "Web frontend JavaScript tests failed with exit code $LASTEXITCODE; EXE packaging stopped." }
    }
    $env:NUITKA_CACHE_DIR = $nuitkaCacheDirectory
    & $python -m nuitka --version
    if ($LASTEXITCODE -ne 0) { throw 'Install build dependencies with: python -m pip install -e ".[dev,build]"' }

    $pythonBaseHex = (& $python -c "import sys; print(sys.base_prefix.encode('utf-8').hex())").Trim()
    if ([string]::IsNullOrWhiteSpace($pythonBaseHex) -or $pythonBaseHex -notmatch '^(?:[0-9A-Fa-f]{2})+$') {
        throw "Python returned an invalid UTF-8 encoded base runtime path."
    }
    $pythonBaseBytes = New-Object byte[] ($pythonBaseHex.Length / 2)
    for ($index = 0; $index -lt $pythonBaseHex.Length; $index += 2) {
        $pythonBaseBytes[[int]($index / 2)] = [Convert]::ToByte($pythonBaseHex.Substring($index, 2), 16)
    }
    $pythonBase = [System.Text.Encoding]::UTF8.GetString($pythonBaseBytes)
    $pythonVersion = (& $python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])").Trim()
    if ([string]::IsNullOrWhiteSpace($PortablePythonPath)) {
        $portablePython = Join-Path $PSScriptRoot "build\portable-python-$pythonVersion"
    } else {
        $portablePython = (Resolve-Path -LiteralPath $PortablePythonPath).Path
    }
    $runtimeMarkers = @("python.exe", "python3.dll", "Lib\os.py", "DLLs\_ssl.pyd")
    $hasRuntime = $true
    foreach ($marker in $runtimeMarkers) {
        if (-not (Test-Path -LiteralPath (Join-Path $portablePython $marker))) { $hasRuntime = $false }
    }
    if (-not $hasRuntime -and (Test-Path -LiteralPath $portablePython)) {
        throw "Portable Python runtime is incomplete. Remove the generated directory and retry: $portablePython"
    }
    if (-not $hasRuntime) {
        if (-not (Test-Path -LiteralPath $pythonBase)) {
            throw "Python base runtime is missing: $pythonBase. Supply -PortablePythonPath to an existing matching runtime."
        }
        New-Item -ItemType Directory -Path $portablePython -Force | Out-Null
        Get-ChildItem -LiteralPath $pythonBase -Force | Copy-Item -Destination $portablePython -Recurse -Force
    }

    $portableExecutable = Join-Path $portablePython "python.exe"
    if (-not (Test-Path -LiteralPath $portableExecutable)) {
        throw "Portable Python runtime copy is incomplete: $portableExecutable"
    }
    $portableVersion = (& $portableExecutable -c "import sys; print('%d.%d.%d' % sys.version_info[:3])").Trim()
    if ($portableVersion -ne $pythonVersion) {
        throw "Portable Python version $portableVersion does not match the build environment $pythonVersion."
    }

    $env:PYTHONPATH = (Join-Path $PSScriptRoot "src") + ";" + $pythonSitePackages
    $env:NUITKA_CACHE_DIR = $nuitkaCacheDirectory
    $smokeConfigDirectory = New-FlukeSmokeConfigDirectory -SmokeConfigParent $env:TEMP
    $env:FLUKE8845A_CONFIG_DIR = $smokeConfigDirectory
    $entryPoint = Join-Path $PSScriptRoot "Fluke8845A.py"

    & $portableExecutable -m nuitka $entryPoint `
        --standalone `
        --enable-plugin=pyside6 `
        --nofollow-import-to=PySide6.QtTest `
        --assume-yes-for-downloads `
        --remove-output `
        "--output-dir=$outputDirectory" `
        --output-filename=Fluke8845A.exe `
        --windows-console-mode=disable `
        "--windows-icon-from-ico=$PSScriptRoot\assets\fluke8845a_app_icon_v3.ico" `
        --include-data-dir=docs=docs `
        --include-data-dir=assets=assets `
        --include-package=serial `
        --include-module=PySide6.QtOpenGL `
        --include-module=PySide6.QtOpenGLWidgets
    if ($LASTEXITCODE -ne 0) { throw "Nuitka build failed." }

    if (-not $DesktopOnly) {
    $webEntryPoint = Join-Path $PSScriptRoot "Fluke8845A_Web.py"
    $webAssetsDirectory = Join-Path $PSScriptRoot "src\fluke8845a\web"
    & $portableExecutable -m nuitka $webEntryPoint `
        --standalone `
        --enable-plugin=pyside6 `
        --nofollow-import-to=PySide6.QtTest `
        --noinclude-qt-plugins=imageformats `
        --assume-yes-for-downloads `
        --remove-output `
        "--output-dir=$outputDirectory" `
        --output-filename=Fluke8845A-Web.exe `
        --windows-console-mode=disable `
        "--windows-icon-from-ico=$PSScriptRoot\assets\fluke8845a_app_icon_v3.ico" `
        "--include-data-dir=$webAssetsDirectory=fluke8845a\web" `
        --include-package=serial `
        --lto=no
    if ($LASTEXITCODE -ne 0) { throw "Nuitka localhost web build failed." }

    $webDist = Join-Path $outputDirectory "Fluke8845A_Web.dist"
    $webExecutable = Join-Path $webDist "Fluke8845A-Web.exe"
    if (-not (Test-Path -LiteralPath $webExecutable)) { throw "Nuitka did not create $webExecutable" }
    $webReleaseDist = Join-Path $exeDist "Fluke8845A-Web.dist"
    Copy-Item -LiteralPath $webDist -Destination $webReleaseDist -Recurse -Force
    }

    # The desktop app never reads PDF files; Qt's default plugin set includes this unused plugin.
    $unusedQtPdfPlugin = Join-Path $exeDist "PySide6\qt-plugins\imageformats\qpdf.dll"
    if (Test-Path -LiteralPath $unusedQtPdfPlugin) {
        Remove-Item -LiteralPath $unusedQtPdfPlugin -Force
    }

    & $python (Join-Path $PSScriptRoot "scripts\package_windows_release.py") `
        --app-dist $exeDist `
        --prune-unused-qt-pdf
    if ($LASTEXITCODE -ne 0) { throw "Unused Qt PDF components cleanup failed." }

    & $python (Join-Path $PSScriptRoot "scripts\collect_windows_runtime_licenses.py") `
        --python-root $portablePython `
        --site-packages $pythonSitePackages `
        --project-root $PSScriptRoot `
        --output-root (Join-Path $exeDist "docs\licenses") `
        --binary-root $exeDist `
        --python-version $pythonVersion
    if ($LASTEXITCODE -ne 0) { throw "Windows runtime license collection failed." }

    $exe = Join-Path $exeDist "Fluke8845A.exe"
    if (-not (Test-Path -LiteralPath $exe)) { throw "Nuitka did not create $exe" }

    $buildPythonPath = $env:PYTHONPATH
    Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
    $smoke = Start-Process -FilePath $exe -ArgumentList @("--mock", "--smoke") -PassThru -WindowStyle Hidden
    Wait-SoftwareSmokeProcess -Process $smoke -Description "Frozen Mock GUI smoke check"
    $serialSmoke = Start-Process -FilePath $exe -ArgumentList @("--serial-smoke") -PassThru -WindowStyle Hidden
    Wait-SoftwareSmokeProcess -Process $serialSmoke -Description "Frozen pyserial import/port enumeration check"
    $env:PYTHONPATH = $buildPythonPath

    $provenanceScopeArguments = @()
    if ($DesktopOnly) {
        $provenanceScopeArguments = @("--desktop-only")
    } else {
        $provenanceScopeArguments = @("--web-exe", $webExecutable)
    }
    & $python (Join-Path $PSScriptRoot "scripts\build_provenance.py") create `
        --root $PSScriptRoot `
        --candidate-build-name $candidateBuildName `
        --expected-source-digest $sourceDigestPath `
        --desktop-exe $exe `
        @provenanceScopeArguments `
        --output $provenancePath
    if ($LASTEXITCODE -ne 0) { throw "Build provenance creation failed; sources may have changed during compilation." }
    Copy-Item -LiteralPath $provenancePath -Destination (Join-Path $exeDist "BUILD-IDENTITY.json") -Force

    # Generate the report after the identity marker exists so its recorded window title names this build.
    $buildPythonPath = $env:PYTHONPATH
    Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
    $mockE2EReport = Join-Path $PSScriptRoot ("build\mock-e2e-" + [guid]::NewGuid().ToString("N") + ".json")
    $mockE2E = Start-Process -FilePath $exe -ArgumentList @("--mock-e2e", "--report", $mockE2EReport) -PassThru -WindowStyle Hidden
    Wait-SoftwareSmokeProcess -Process $mockE2E -Description "Frozen six-mode Mock GUI verification"
    if (-not (Test-Path -LiteralPath $mockE2EReport)) { throw "Frozen six-mode Mock GUI verification did not create its report: $mockE2EReport" }
    & $python (Join-Path $PSScriptRoot "scripts\update_mock_report.py") `
        --report $mockE2EReport `
        --executable $exe
    if ($LASTEXITCODE -ne 0) { throw "Frozen six-mode Mock GUI report identity update failed." }
    $mockE2EData = Get-Content -LiteralPath $mockE2EReport -Raw | ConvertFrom-Json
    if ($mockE2EData.status -ne "ok" -or $mockE2EData.simulation -ne $true -or $mockE2EData.mode_count -ne 6) {
        throw "Frozen six-mode Mock GUI verification returned an invalid report: $mockE2EReport"
    }
    $mockValidationDirectory = Join-Path $PSScriptRoot "output\mock_validation"
    New-Item -ItemType Directory -Path $mockValidationDirectory -Force | Out-Null
    if ($SideBySide) {
        $releaseMockE2EReport = Join-Path $mockValidationDirectory ($BuildName + "_six_mode_e2e.json")
        if (Test-Path -LiteralPath $releaseMockE2EReport) {
            throw "Refusing to overwrite an existing side-by-side Mock report: $releaseMockE2EReport"
        }
    } else {
        $releaseMockE2EReport = Join-Path $mockValidationDirectory "latest_frozen_exe_six_mode_e2e.json"
    }
    Copy-Item -LiteralPath $mockE2EReport -Destination $releaseMockE2EReport -Force
    $env:PYTHONPATH = $buildPythonPath

    $packageScopeArguments = @()
    if ($DesktopOnly) { $packageScopeArguments = @("--desktop-only") }
    & $python (Join-Path $PSScriptRoot "scripts\package_windows_release.py") `
        --app-dist $exeDist `
        --archive $portableArchive `
        --mock-report $releaseMockE2EReport `
        --provenance $provenancePath `
        @packageScopeArguments
    if ($LASTEXITCODE -ne 0) { throw "Portable Windows package creation or verification failed." }

    & $python (Join-Path $PSScriptRoot "scripts\package_source.py") `
        --output $sourceArchive `
        --provenance $provenancePath
    if ($LASTEXITCODE -ne 0) { throw "Source project archive creation or verification failed." }

    if ($SideBySide) {
        & $python (Join-Path $PSScriptRoot "scripts\latest_verified_launcher.py") `
            --dist-root $distRoot `
            --executable $exe
        if ($LASTEXITCODE -ne 0) { throw "Latest verified Windows launcher update failed." }
        Write-Host "Updated latest verified launcher $(Join-Path $distRoot 'Start-Latest-Verified-Fluke8845A.bat')"
    } else {
        Write-Host "Stable launcher unchanged; use -SideBySide to promote an immutable verified build."
    }

    Write-Host "Created and smoke-checked $exe"
    Write-Host "Created portable package $portableArchive"
    Write-Host "Created source project package $sourceArchive"
}
finally {
    if ($null -ne $smokeConfigDirectory) {
        try {
            Remove-FlukeSmokeConfigDirectory -Path $smokeConfigDirectory -SmokeConfigParent $env:TEMP
        } catch {
            Write-Warning "Could not remove this build's temporary smoke config directory: $smokeConfigDirectory. $($_.Exception.Message)"
        }
    }
    if ($null -eq $previousPythonPath) { Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue }
    else { $env:PYTHONPATH = $previousPythonPath }
    if ($null -eq $previousNuitkaCache) { Remove-Item Env:\NUITKA_CACHE_DIR -ErrorAction SilentlyContinue }
    else { $env:NUITKA_CACHE_DIR = $previousNuitkaCache }
    if ($null -eq $previousConfigDir) { Remove-Item Env:\FLUKE8845A_CONFIG_DIR -ErrorAction SilentlyContinue }
    else { $env:FLUKE8845A_CONFIG_DIR = $previousConfigDir }
    if ($null -eq $previousTemp) { Remove-Item Env:\TEMP -ErrorAction SilentlyContinue }
    else { $env:TEMP = $previousTemp }
    if ($null -eq $previousTmp) { Remove-Item Env:\TMP -ErrorAction SilentlyContinue }
    else { $env:TMP = $previousTmp }
    Set-Location -LiteralPath $previousLocation
}
