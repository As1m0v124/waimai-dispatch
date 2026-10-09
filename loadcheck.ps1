# Sustained-load check: does the fleet keep up with the order arrival rate?
# Samples the pool size and rider utilisation over time.
# Run: powershell -ExecutionPolicy Bypass -File loadcheck.ps1
# (ASCII-only source: PowerShell 5.1 reads non-BOM .ps1 files as ANSI.)

param(
    [int]$Port = 8787,
    [int]$Samples = 24,
    [int]$EveryMs = 2500,
    [int]$Speed = 200
)

$ErrorActionPreference = 'Stop'
$base = "http://127.0.0.1:$Port"

# 认证 token（服务端默认给所有 /api/* 加了认证）。见 verify-api.ps1 里的同一段。
$script:token = $env:WAIMAI_TOKEN
if (-not $script:token) {
    $tokenFile = Join-Path $PSScriptRoot 'data\api-token'
    if (Test-Path $tokenFile) {
        $script:token = (Get-Content $tokenFile -Raw -Encoding UTF8).Trim()
    }
}

function AuthHeaders {
    $h = @{}
    if ($script:token) { $h['X-Auth-Token'] = $script:token }
    return $h
}

function Api($path, $body) {
    $json = $body | ConvertTo-Json -Compress
    Invoke-RestMethod -Uri "$base/api/$path" -Method Post `
        -ContentType 'application/json; charset=utf-8' -Headers (AuthHeaders) `
        -Body ([System.Text.Encoding]::UTF8.GetBytes($json))
}

Write-Host ""
Write-Host "== sustained load check ==" -ForegroundColor Cyan

Api 'control' @{ warmupOrders = 6; autoOrder = $true; speed = $Speed
                 dispatchIntervalSec = 120; autoOrderEverySec = 60; paused = $false } | Out-Null
Api 'reset' @{} | Out-Null

Write-Host ("  {0,-9} {1,6} {2,6} {3,6} {4,6} {5,8} {6,8} {7,7}" -f `
    'clock', 'pool', 'fly', 'done', 'total', 'load', 'avgTotal', 'onTime')
Write-Host ("  " + ("-" * 62))

$maxPool = 0
$poolSeries = @()
$lastStats = $null
for ($i = 0; $i -lt $Samples; $i++) {
    Start-Sleep -Milliseconds $EveryMs
    $s = Invoke-RestMethod -Uri "$base/api/state" -Method Get -Headers (AuthHeaders)
    $st = $s.stats
    $lastStats = $st
    $poolSeries += $st.pooled
    if ($st.pooled -gt $maxPool) { $maxPool = $st.pooled }
    Write-Host ("  {0,-9} {1,6} {2,6} {3,6} {4,6} {5,8} {6,8} {7,7}" -f `
        $s.sim.clock, $st.pooled, $st.inFlight, $st.delivered, $st.totalOrders,
        ("$($st.riderLoad)/$($st.riderCapacity)"),
        $(if ($null -eq $st.avgTotalMin) { '-' } else { $st.avgTotalMin }),
        $(if ($null -eq $st.onTimeRate) { '-' } else { "$($st.onTimeRate)%" }))
}

$st = $lastStats
$n = $poolSeries.Count
$tail = if ($n -ge 10) { $poolSeries[-5..-1] } else { $poolSeries }
$mid = if ($n -ge 10) { $poolSeries[-10..-6] } else { $poolSeries }
$tailAvg = ($tail | Measure-Object -Average).Average
$midAvg = ($mid | Measure-Object -Average).Average
$tailMax = ($tail | Measure-Object -Maximum).Maximum

Write-Host ""
Write-Host "  pool size series      : $($poolSeries -join ' ')"
Write-Host "  peak pool size        : $maxPool"
Write-Host "  pool avg (late / mid) : $([math]::Round($tailAvg, 1)) / $([math]::Round($midAvg, 1))"
Write-Host "  of which postponed    : $($st.pooledPostponed) waiting on purpose (cumulative $($st.postponedOrders) orders / $($st.postponedRounds) rounds)"
Write-Host "  delivered             : $($st.delivered) of $($st.totalOrders)"
Write-Host "  rider utilisation     : $($st.riderUtilization)%"
Write-Host "  avg total wait (min)  : $($st.avgTotalMin)"
Write-Host "  avg dispatch wait     : $($st.avgWaitDispatchMin) min"
Write-Host "  avg to-store          : $($st.avgToStoreMin) min"
Write-Host "  avg pre-trip wait     : $($st.avgPrepWaitMin) min"
Write-Host "  avg on-road           : $($st.avgOnRoadMin) min"
Write-Host "  on-time rate          : $($st.onTimeRate)%  (SLA $($st.slaMinutes) min)"
Write-Host "  on-route share        : $($st.onRouteShare)%  (tier1 $($st.tier1OnRoute) / tier2 $($st.tier2Idle) / tier3 $($st.tier3Fallback))"
Write-Host "  riders free           : $($s.intake.ridersFree)"
Write-Host "  rider km              : $($st.riderTotalKm) km"
Write-Host "  sim minutes elapsed   : $([math]::Round(($s.sim.seconds - 36000) / 60, 1))"

# The question is whether the pool is *draining or stable*, not whether it ever spiked.
# A spike right after the warmup burst is expected, because the first dispatch round is
# still up to dispatchIntervalSec away.
$stable = $tailAvg -le ($midAvg + 1.0)

# A non-empty pool is NOT by itself a backlog any more. With postponement on (the
# default) a pooled order is often one that is *deliberately* waiting for a better rider,
# so an absolute threshold like "pool <= 6" would flag the intended behaviour as a
# problem. What must hold instead is that the queue never outgrows the riders who could
# clear it right now, and that service quality holds up.
$grace = [Math]::Max(6, [int]$s.intake.ridersFree)
$insideCapacity = $tailMax -le $grace
$healthySla = ($null -ne $st.onTimeRate) -and ($st.onTimeRate -ge 90)
$headroom = $st.riderUtilization -lt 85 * 1
$delivering = $st.delivered -gt 0

Write-Host ""
if ($stable -and $insideCapacity -and $healthySla -and $headroom -and $delivering) {
    Write-Host "LOAD OK - the fleet keeps up with the order rate" -ForegroundColor Green
    exit 0
}
Write-Host "LOAD PROBLEM:" -ForegroundColor Red
if (-not $stable) { Write-Host "  - the pool is still growing late in the run" -ForegroundColor Red }
if (-not $insideCapacity) {
    Write-Host "  - the pool ($tailMax) outgrew the riders who could clear it ($($s.intake.ridersFree) free)" -ForegroundColor Red }
if (-not $healthySla) { Write-Host "  - on-time rate is unhealthy ($($st.onTimeRate)%)" -ForegroundColor Red }
if (-not $headroom) { Write-Host "  - riders are saturated ($($st.riderUtilization)%)" -ForegroundColor Red }
if (-not $delivering) { Write-Host "  - nothing was delivered" -ForegroundColor Red }
Write-Host "  Try lowering autoOrderEverySec or raising defaultMaxOrders / rider count." -ForegroundColor Yellow
exit 1
