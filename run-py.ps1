# Run the Python port of the dispatch MVP.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File run-py.ps1
#   powershell -ExecutionPolicy Bypass -File run-py.ps1 -Port 8788 -Speed 60
#   powershell -ExecutionPolicy Bypass -File run-py.ps1 -Osm realistic
#   powershell -ExecutionPolicy Bypass -File run-py.ps1 -Osm hangzhou.osm -Bbox 30.24,120.14,30.29,120.19
#   powershell -ExecutionPolicy Bypass -File run-py.ps1 -ListNetworks
#
# Params:
#   -Port          listen port, default 8787
#   -Speed         simulation speed (1 real second = N simulated seconds), default 20
#   -Seed          random seed, default 20260927
#   -Osm           enable a road network: "realistic" (built-in simulated city),
#                  "synthetic" (built-in grid), or a file name from data/osm/
#   -Bbox          clip extent minLat,minLon,maxLat,maxLon (useful for huge .osm files)
#   -ListNetworks  list available road networks and exit
# (Kept ASCII-only on purpose: PowerShell 5.1 reads non-BOM .ps1 files as ANSI.)

param(
    [int]$Port = 8787,
    [double]$Speed = 20,
    [long]$Seed = 20260927,
    [string]$Osm = '',
    [string]$Bbox = '',
    [switch]$ListNetworks
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

. "$root\tools\python.ps1"      # provides $PYTHON

# NOTE: do not use $args here -- it is a PowerShell automatic variable.
# These flags used to be documented in the README but were missing from this
# script, so the documented commands failed with "no parameter matches -Osm".
$pyArgs = @('-X', 'utf8', "$root\py\waimai\main.py",
            '--port', $Port, '--speed', $Speed, '--seed', $Seed)
if ($Osm) { $pyArgs += @('--osm', $Osm) }
if ($Bbox) { $pyArgs += @('--bbox', $Bbox) }
if ($ListNetworks) { $pyArgs += '--list-networks' }

Write-Host "Using Python: $PYTHON" -ForegroundColor DarkGray
& $PYTHON @pyArgs
exit $LASTEXITCODE
