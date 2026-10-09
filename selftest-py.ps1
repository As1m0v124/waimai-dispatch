# Run the Python port's self-test.
# Usage:  powershell -ExecutionPolicy Bypass -File selftest-py.ps1
# (Kept ASCII-only on purpose: PowerShell 5.1 reads non-BOM .ps1 files as ANSI.)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

. "$root\tools\python.ps1"

& $PYTHON -X utf8 "$root\py\waimai\selftest.py"
exit $LASTEXITCODE
