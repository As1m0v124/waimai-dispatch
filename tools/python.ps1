# Locate a usable Python 3.10+ interpreter.
# Mirrors tools/jdk.ps1: try the PATH first, then the common install locations,
# then any Windows Store alias that actually works.
# (Kept ASCII-only on purpose: PowerShell 5.1 reads non-BOM .ps1 files as ANSI.)

function Test-Python($exe) {
    if (-not $exe) { return $false }
    if (-not (Test-Path $exe)) { return $false }
    # The Microsoft Store stub exists on PATH but fails when run -- filter it out.
    if ($exe -like '*\WindowsApps\python*.exe') { return $false }
    try {
        $null = & $exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-Python {
    $candidates = @()

    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    $onPath3 = Get-Command python3 -ErrorAction SilentlyContinue
    if ($onPath3) { $candidates += $onPath3.Source }

    $local = Join-Path $env:LOCALAPPDATA 'Programs\Python'
    if (Test-Path $local) {
        Get-ChildItem $local -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { $candidates += (Join-Path $_.FullName 'python.exe') }
    }

    $candidates += @(
        'C:\Python313\python.exe', 'C:\Python312\python.exe', 'C:\Python311\python.exe',
        'C:\Program Files\Python313\python.exe',
        'C:\Program Files\Python312\python.exe',
        'C:\Program Files\Python311\python.exe',
        'C:\ProgramData\anaconda3\python.exe',
        (Join-Path $env:USERPROFILE 'anaconda3\python.exe'),
        (Join-Path $env:USERPROFILE 'miniconda3\python.exe')
    )

    foreach ($c in $candidates) {
        if (Test-Python $c) { return $c }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "No Python 3.10+ found." -ForegroundColor Red
    Write-Host "Install one with:  winget install --id Python.Python.3.13 --exact --scope user" -ForegroundColor Yellow
    Write-Host "Note: the python.exe in WindowsApps is a Microsoft Store stub and does not work." -ForegroundColor Yellow
    exit 1
}

$global:PYTHON = $py
