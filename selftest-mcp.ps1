# Run the MCP integration test (spawns a real stdio server and does JSON-RPC round trips).
# Usage:  powershell -ExecutionPolicy Bypass -File selftest-mcp.ps1
# (Kept ASCII-only on purpose: PowerShell 5.1 reads non-BOM .ps1 files as ANSI.)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

. "$root\tools\python.ps1"

& $PYTHON -X utf8 "$root\py\mcp_selftest.py"
exit $LASTEXITCODE
