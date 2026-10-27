function ConvertFrom-FlukeUtf8Hex {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Hex
    )

    if ([string]::IsNullOrWhiteSpace($Hex) -or $Hex -notmatch '^(?:[0-9A-Fa-f]{2})+$') {
        throw "Python returned an invalid UTF-8 encoded path."
    }

    $bytes = New-Object byte[] ($Hex.Length / 2)
    for ($index = 0; $index -lt $Hex.Length; $index += 2) {
        $bytes[[int]($index / 2)] = [Convert]::ToByte($Hex.Substring($index, 2), 16)
    }
    return [System.Text.Encoding]::UTF8.GetString($bytes)
}

function Assert-FlukePathHasNoReparsePoints {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $currentPath = [System.IO.Path]::GetFullPath($Path)
    while (-not [string]::IsNullOrWhiteSpace($currentPath)) {
        if (Test-Path -LiteralPath $currentPath) {
            $item = Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing to use a path containing a reparse point: $currentPath"
            }
        }

        $parentPath = [System.IO.Path]::GetDirectoryName($currentPath)
        if ([string]::IsNullOrWhiteSpace($parentPath) -or $parentPath -ieq $currentPath) {
            break
        }
        $currentPath = $parentPath
    }
}

function New-FlukeSmokeConfigDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SmokeConfigParent
    )

    $parentPath = [System.IO.Path]::GetFullPath($SmokeConfigParent)
    Assert-FlukePathHasNoReparsePoints -Path $parentPath
    if (-not (Test-Path -LiteralPath $parentPath)) {
        New-Item -ItemType Directory -Path $parentPath -ErrorAction Stop | Out-Null
    }
    $parentItem = Get-Item -LiteralPath $parentPath -Force -ErrorAction Stop
    if (-not $parentItem.PSIsContainer) {
        throw "Smoke config parent must be a directory: $parentPath"
    }
    if (($parentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Smoke config parent cannot be a reparse point: $parentPath"
    }
    Assert-FlukePathHasNoReparsePoints -Path $parentPath

    $directoryName = "Fluke8845A-smoke-config-$([guid]::NewGuid().ToString('N'))"
    $directoryPath = Join-Path $parentPath $directoryName
    if (Test-Path -LiteralPath $directoryPath) {
        throw "Refusing to reuse an existing smoke config directory: $directoryPath"
    }
    New-Item -ItemType Directory -Path $directoryPath -ErrorAction Stop | Out-Null
    return [System.IO.Path]::GetFullPath($directoryPath)
}

function Remove-FlukeSmokeConfigDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$SmokeConfigParent
    )

    $parentPath = [System.IO.Path]::GetFullPath($SmokeConfigParent)
    $directoryPath = [System.IO.Path]::GetFullPath($Path)
    Assert-FlukePathHasNoReparsePoints -Path $parentPath
    Assert-FlukePathHasNoReparsePoints -Path $directoryPath
    $directoryName = [System.IO.Path]::GetFileName($directoryPath)
    $expectedParent = [System.IO.Path]::GetDirectoryName($directoryPath)
    if ($expectedParent -ine $parentPath -or $directoryName -notmatch '^Fluke8845A-smoke-config-[0-9a-fA-F]{32}$') {
        throw "Refusing to remove an unowned smoke config path: $directoryPath"
    }
    if (-not (Test-Path -LiteralPath $directoryPath)) {
        return
    }

    $directoryItem = Get-Item -LiteralPath $directoryPath -Force -ErrorAction Stop
    if (-not $directoryItem.PSIsContainer) {
        throw "Refusing to remove a non-directory smoke config path: $directoryPath"
    }
    if (($directoryItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to remove a reparse point smoke config path: $directoryPath"
    }
    $reparsePoint = Get-ChildItem -LiteralPath $directoryPath -Force -Recurse -ErrorAction Stop |
        Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 } |
        Select-Object -First 1
    if ($null -ne $reparsePoint) {
        throw "Refusing to recursively remove smoke config containing a reparse point: $($reparsePoint.FullName)"
    }

    Remove-Item -LiteralPath $directoryPath -Recurse -Force -ErrorAction Stop
}
