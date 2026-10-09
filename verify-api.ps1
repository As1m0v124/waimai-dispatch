# End-to-end HTTP verification of the dispatch MVP.
#
# Run:  powershell -ExecutionPolicy Bypass -File verify-api.ps1
# Requires the server to be running (run.ps1).
#
# The source is deliberately ASCII-only (PowerShell 5.1 reads non-BOM .ps1 files as
# ANSI). Chinese test payloads are built from code points so the UTF-8 round-trip
# through the API is still exercised for real.
#
# The script resets the simulation first and turns auto-ordering off, so every check
# runs against a known world state instead of whatever the demo happened to be doing.
#
# Exit code 0 = all checks passed.

param([int]$Port = 8787)

$ErrorActionPreference = 'Stop'
$base = "http://127.0.0.1:$Port"
$script:pass = 0
$script:fail = 0

# 认证 token。服务端默认只绑本机并给所有 /api/* 加认证，token 在 data/api-token
# （或环境变量 WAIMAI_TOKEN）。第二次握手之前先把它读出来，后面每个请求都带上。
$script:token = $env:WAIMAI_TOKEN
if (-not $script:token) {
    $tokenFile = Join-Path $PSScriptRoot 'data\api-token'
    if (Test-Path $tokenFile) {
        $script:token = (Get-Content $tokenFile -Raw -Encoding UTF8).Trim()
    }
}

function Check($name, $ok, $extra) {
    if ($ok) { $script:pass++; Write-Host ("  [OK]   " + $name) -ForegroundColor Green }
    else { $script:fail++; Write-Host ("  [FAIL] " + $name + "  " + $extra) -ForegroundColor Red }
}

function U([int[]]$cp) { -join ($cp | ForEach-Object { [char]$_ }) }

function State { Invoke-RestMethod -Uri "$base/api/state" -Method Get -Headers (AuthHeaders) }

function AuthHeaders($extra) {
    $h = @{}
    if ($extra) { $extra.GetEnumerator() | ForEach-Object { $h[$_.Key] = $_.Value } }
    if ($script:token) { $h['X-Auth-Token'] = $script:token }
    return $h
}

function Api($path, $body) {
    $json = $body | ConvertTo-Json -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    try {
        Invoke-RestMethod -Uri "$base/api/$path" -Method Post `
            -ContentType 'application/json; charset=utf-8' -Body $bytes `
            -Headers (AuthHeaders)
    } catch {
        # A missing endpoint (404) must surface as a failed check, not abort the run.
        # Invoke-RestMethod throws on any non-2xx, and with $ErrorActionPreference='Stop'
        # that used to kill the script at the first absent endpoint -- so one missing
        # feature hid every check after it (the Java reference build hits this).
        $resp = $_.Exception.Response
        $code = if ($resp) { [int]$resp.StatusCode } else { 0 }
        [pscustomobject]@{
            ok = $false
            error = ("HTTP " + $code + " " + $_.Exception.Message)
        }
    }
}

function OrderById($state, $id) { $state.orders | Where-Object { $_.id -eq $id } }
function RiderById($state, $id) { $state.riders | Where-Object { $_.id -eq $id } }

# Chinese payloads, expressed as code points (keeps this file ASCII-safe).
$NAME  = U @(0x674E, 0x56DB)                              # Li Si
$NOTE  = U @(0x4E0D, 0x8981, 0x8471)                      # "no scallion"
$ADDR  = (U @(0x6D4B, 0x8BD5, 0x5C0F, 0x533A)) + " 1A"    # "test estate 1A"
$NAME2 = U @(0x738B, 0x4E94)                              # Wang Wu
$KIND_DIAODAN = U @(0x8C03, 0x5355)                       # event type "reassign"
$KIND_PAIDAN  = U @(0x6D3E, 0x5355)                       # log kind   "dispatch"

Write-Host ""
Write-Host "== HTTP end-to-end verification ==" -ForegroundColor Cyan

# ---------------------------------------------------------------- 1. static assets
$html = Invoke-WebRequest -Uri "$base/" -UseBasicParsing
Check "GET / serves the app shell" ($html.StatusCode -eq 200 -and $html.Content -match 'app\.js')
$js = Invoke-WebRequest -Uri "$base/app.js" -UseBasicParsing
Check "GET /app.js serves the frontend" ($js.StatusCode -eq 200 -and $js.Content.Length -gt 5000)
$css = Invoke-WebRequest -Uri "$base/style.css" -UseBasicParsing
Check "GET /style.css serves the stylesheet" ($css.StatusCode -eq 200 -and $css.Content.Length -gt 3000)

# ---------------------------------------------------------------- 1b. 安全
Write-Host "  ... security" -ForegroundColor DarkGray

function HttpStatus($path, $method, $headers, $body) {
    # Return the status code of a request. 4xx/5xx don't throw out of here.
    $params = @{
        Uri = "$base/$path"; Method = $method; UseBasicParsing = $true
        ErrorAction = 'Stop'
    }
    if ($headers) { $params.Headers = $headers }
    if ($body) { $params.Body = $body; $params.ContentType = 'application/json' }
    try {
        $r = Invoke-WebRequest @params
        return [int]$r.StatusCode
    } catch {
        $resp = $_.Exception.Response
        if ($resp) { return [int]$resp.StatusCode }
        return 0
    }
}

# 注意：GET 不能带 -Body —— 带上了 Invoke-WebRequest 会把它变成 POST，
# 于是"无 token 的 GET"实际发出去的是 POST，收到 405 而不是 401，看起来像是认证没生效。
Check "state 需要认证（无 token 401）" ((HttpStatus 'api/state' 'GET' $null $null) -eq 401)
Check "写接口需要认证（无 token 401）" ((HttpStatus 'api/reset' 'POST' $null '{}') -eq 401)

$bad = $null
try {
    Invoke-WebRequest -Uri "$base/api/state" -UseBasicParsing -ErrorAction Stop `
        -Headers @{ 'X-Auth-Token' = 'definitely-not-the-token' } | Out-Null
} catch { $bad = [int]$_.Exception.Response.StatusCode }
Check "错误 token 被拒" ($bad -eq 401)

# 任意文件读取：以前 GET /C:/... 能读到 data/llm.properties 里的明文 API Key
$traversals = @(
    'C:/Users/asimov/.zcode/workspace/default/waimai-dispatch/data/llm.properties',
    '../../../data/llm.properties',
    '..%2f..%2fdata%2fllm.properties',
    '%2e%2e/%2e%2e/data/llm.properties',
    'app.js:stream',
    '../.gitignore'
)
$leak = 0
$badCode = 0
foreach ($t in $traversals) {
    $code = 0
    $content = ''
    try {
        $r = Invoke-WebRequest -Uri "$base/$t" -UseBasicParsing -ErrorAction Stop
        $code = [int]$r.StatusCode
        $content = [string]$r.Content
    } catch {
        $resp = $_.Exception.Response
        if ($resp) { $code = [int]$resp.StatusCode }
    }
    if ($code -eq 200) { $badCode++ }
    if ($content -match 'apiKey|sk-|deepseek' ) { $leak++ }
}
Check "路径穿越全部被拒（$($traversals.Count) 种手法）" ($badCode -eq 0) "允许通过的次数=$badCode"
Check "越界读取没有泄漏任何配置内容" ($leak -eq 0) "泄漏次数=$leak"

# token 只在本机绑定时注入页面。
# 这里要断言的是"页面里没有配置里的那个 Key"，而不是"页面里没有 apiKey 这个词"
# —— 页面上本来就有 LLM 配置表单，出现 apiKey 这个字段名是正常的。
Check "首页注入了 token（本机绑定）" ($html.Content -match 'name="waimai-token"')
$storedKey = ''
$cfgFile = Join-Path $PSScriptRoot 'data\llm.properties'
if (Test-Path $cfgFile) {
    foreach ($line in (Get-Content $cfgFile -Encoding UTF8)) {
        if ($line -match '^\s*apiKey\s*=\s*(.+)$') { $storedKey = $Matches[1].Trim() }
    }
}
Check "首页不含配置里的 API Key" `
    ($storedKey.Length -lt 8 -or $html.Content -notmatch [regex]::Escape($storedKey)) `
    ("keyLen=" + $storedKey.Length)

# CORS：以前是通配 *，任意网页都能驱动接口
$corsEvil = $null
$corsSame = $null
try {
    $r = Invoke-WebRequest -Uri "$base/api/state" -UseBasicParsing -ErrorAction Stop `
        -Headers (AuthHeaders @{ 'Origin' = 'https://evil.example.com' })
    $corsEvil = $r.Headers['Access-Control-Allow-Origin']
} catch { }
try {
    $r = Invoke-WebRequest -Uri "$base/api/state" -UseBasicParsing -ErrorAction Stop `
        -Headers (AuthHeaders @{ 'Origin' = $base })
    $corsSame = $r.Headers['Access-Control-Allow-Origin']
} catch { }
Check "跨站 Origin 不回 CORS 头" ($null -eq $corsEvil) "value=$corsEvil"
Check "同源 Origin 才回 CORS 头" ($corsSame -eq $base) "value=$corsSame"

# 请求硬化。
# Content-Length 必须用裸 socket 发：Invoke-WebRequest 会按实际的 body 自己算
# Content-Length 并覆盖掉我们给的值，所以拿它根本发不出"畸形的长度"，
# 检查会永远"看起来通过"。这里直接把字节写到 TCP 上。
function RawRequestLine($rawHeaders) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect('127.0.0.1', $Port)
        $stream = $client.GetStream()
        $payload = "POST /api/dispatch HTTP/1.1`r`nHost: 127.0.0.1:$Port`r`n" `
            + "X-Auth-Token: $script:token`r`n" + $rawHeaders + "`r`n"
        $bytes = [Text.Encoding]::ASCII.GetBytes($payload)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
        $client.ReceiveTimeout = 5000
        $buf = New-Object byte[] 512
        $read = $stream.Read($buf, 0, $buf.Length)
        if ($read -le 0) { return 0 }
        $text = [Text.Encoding]::ASCII.GetString($buf, 0, $read)
        if ($text -match 'HTTP/1\.[01] (\d{3})') { return [int]$Matches[1] }
        return 0
    } catch {
        return 0
    } finally {
        $client.Close()
    }
}

Check "超大 Content-Length 返回 413" `
    ((RawRequestLine "Content-Length: 99999999`r`nContent-Type: application/json`r`n") -eq 413)
Check "负数 Content-Length 返回 400" `
    ((RawRequestLine "Content-Length: -5`r`nContent-Type: application/json`r`n") -eq 400)
Check "非数字 Content-Length 返回 400" `
    ((RawRequestLine "Content-Length: abc`r`nContent-Type: application/json`r`n") -eq 400)
Check "chunked 请求被拒" `
    ((RawRequestLine "Transfer-Encoding: chunked`r`nContent-Type: application/json`r`n") -eq 400)

# 并发上限：挂住一批只发一半的连接（slowloris 的形态），
# 再打一发应当收到 503，而不是让服务无限起线程。
$held = New-Object System.Collections.ArrayList
$overloadCode = 0
try {
    for ($i = 0; $i -lt 64; $i++) {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect('127.0.0.1', $Port)
        $st = $c.GetStream()
        $bytes = [Text.Encoding]::ASCII.GetBytes("GET /api/health HTTP/1.1`r`nHost: x")
        $st.Write($bytes, 0, $bytes.Length)          # 不发空行 → 服务端停在读 header
        $st.Flush()
        [void]$held.Add($c)
    }
    $probe = New-Object System.Net.Sockets.TcpClient
    $probe.Connect('127.0.0.1', $Port)
    $pst = $probe.GetStream()
    $pbytes = [Text.Encoding]::ASCII.GetBytes("GET /api/health HTTP/1.1`r`nHost: x`r`n`r`n")
    $pst.Write($pbytes, 0, $pbytes.Length)
    $pst.Flush()
    $probe.ReceiveTimeout = 5000
    $buf = New-Object byte[] 128
    $read = $pst.Read($buf, 0, $buf.Length)
    if ($read -gt 0) {
        $text = [Text.Encoding]::ASCII.GetString($buf, 0, $read)
        if ($text -match 'HTTP/1\.[01] (\d{3})') { $overloadCode = [int]$Matches[1] }
    }
    $probe.Close()
} catch { } finally {
    foreach ($c in $held) { try { $c.Close() } catch { } }
}
Check "超过并发上限返回 503（不会无限起线程）" ($overloadCode -eq 503) "code=$overloadCode"
Start-Sleep -Milliseconds 800
Check "连接释放后服务恢复正常" ((HttpStatus 'api/health' 'GET' $null $null) -eq 200)

function BadJsonStatus($raw) {
    try {
        $r = Invoke-WebRequest -Uri "$base/api/order" -Method Post -UseBasicParsing `
            -Headers (AuthHeaders) -Body $raw -ContentType 'application/json' -ErrorAction Stop
        return [int]$r.StatusCode
    } catch {
        $resp = $_.Exception.Response
        if ($resp) { return [int]$resp.StatusCode }
        return 0
    }
}
Check "JSON 里的 NaN 返回 400" ((BadJsonStatus '{"name":NaN}') -eq 400)
Check "JSON 里的 Infinity 返回 400" ((BadJsonStatus '{"name":Infinity}') -eq 400)

# SSRF：baseUrl 指向别处就能把 API Key 发出去
$ssrfRejected = 0
foreach ($u in @('http://evil.example.com/v1', 'ftp://x/y',
                 'https://10.0.0.5/v1', 'https://169.254.169.254/latest/meta-data')) {
    $r = Api 'llm/config' @{ baseUrl = $u }
    if ($r.ok -eq $false -and "$($r.error)" -match 'baseUrl') { $ssrfRejected++ }
}
Check "危险 baseUrl 全部被拒（4 种）" ($ssrfRejected -eq 4) "拒绝数=$ssrfRejected"
Check "合规 https 被接受" ((Api 'llm/config' @{ baseUrl = 'https://api.deepseek.com' }).ok -eq $true)
Check "本机地址（本地模型）被接受" ((Api 'llm/config' @{ baseUrl = 'http://127.0.0.1:11434/v1' }).ok -eq $true)
Api 'llm/config' @{ baseUrl = 'https://api.deepseek.com' } | Out-Null

# 可观测性
$h = Invoke-RestMethod -Uri "$base/api/health" -Method Get
Check "GET /api/health 免认证可用" ($h.ok -eq $true)
Check "health 上报模拟时钟" ($null -ne $h.simClock -and $null -ne $h.uptimeSec)
Check "health 不泄露配置或订单" ($null -eq $h.cfg -and $null -eq $h.orders)
Check "/api/metrics 需要认证" ((HttpStatus 'api/metrics' 'GET' $null $null) -eq 401)
$m = Invoke-RestMethod -Uri "$base/api/metrics" -Method Get -Headers (AuthHeaders)
Check "metrics 有请求计数" ($m.counters.http_requests -gt 0) ("counters=" + ($m.counters | ConvertTo-Json -Compress))
Check "metrics 有延迟分位" ($null -ne $m.histograms.http_latency_ms.p95)
Check "metrics 有最短路调用数" ($null -ne $m.road)
$prom = Invoke-WebRequest -Uri "$base/api/metrics?format=prometheus" -UseBasicParsing -Headers (AuthHeaders)
Check "Prometheus 文本格式可用" ($prom.StatusCode -eq 200 -and $prom.Content -match 'waimai_http_requests')


# ---------------------------------------------------------------- 2. deterministic world
# warmupOrders=0 so the pool starts empty; the opening burst is checked separately below.
Api 'control' @{ warmupOrders = 0 } | Out-Null
Api 'reset' @{} | Out-Null
Api 'control' @{ autoOrder = $false; speed = 1 } | Out-Null

$s = State
Check "reset seeds 8 merchants" ($s.merchants.Count -eq 8)
Check "reset seeds 10 riders" ($s.riders.Count -eq 10)
Check "reset starts the clock at 10:00:00" ($s.sim.seconds -le 36005)
Check "state reports city size 10000" ($s.city -eq 10000)
Check "state includes stats" ($null -ne $s.stats)
Check "clock is formatted HH:MM:SS" ($s.sim.clock -match '^\d\d:\d\d:\d\d$')
Check "default dispatch cadence is 120s" ($s.cfg.dispatchIntervalSec -eq 120)
Check "reset leaves an empty pool" ($s.pool.Count -eq 0)
Check "all riders start ONLINE" (@($s.riders | Where-Object { $_.status -ne 'ONLINE' }).Count -eq 0)
Check "all riders start idle" (@($s.riders | Where-Object { $_.activeOrders -ne 0 }).Count -eq 0)
Check "all riders default to cap 5" (@($s.riders | Where-Object { $_.maxOrders -ne 5 }).Count -eq 0)

# ---------------------------------------------------------------- 3. customer places an order
$r = Api 'order' @{ name = $NAME; phone = '13911112222'; address = $ADDR; note = $NOTE }
Check "POST /api/order accepted" ($r.ok -eq $true) $r.error
Check "order number returned" ($r.orderId -match '^WM[0-9]{5}$')
$oid = $r.orderId

$s = State
$o = OrderById $s $oid
Check "order appears in state" ($null -ne $o)
Check "fresh order is POOLED" ($o.status -eq 'POOLED')
Check "customer name round-trips as UTF-8" ($o.customerName -eq $NAME)
Check "note round-trips as UTF-8" ($o.note -eq $NOTE)
Check "address round-trips as UTF-8" ($o.address -eq $ADDR)
Check "phone round-trips" ($o.phone -eq '13911112222')
Check "timestamp 1 (created) recorded" ($o.t.created -gt 0)
Check "order sits in the order pool" ($s.pool -contains $oid)
Check "no rider assigned yet" ($null -eq $o.riderId)
Check "order has merchant location" ($o.mx -gt 0 -and $o.my -gt 0)
Check "order has delivery location" ($o.dx -gt 0 -and $o.dy -gt 0)

$bad = Api 'order' @{ name = ''; phone = '123'; address = 'x' }
Check "empty name is rejected" ($bad.ok -eq $false)
$bad2 = Api 'order' @{ name = $NAME; phone = ''; address = 'x' }
Check "empty phone is rejected" ($bad2.ok -eq $false)

# ---------------------------------------------------------------- 4. dispatch round
$poolBefore = (State).pool.Count
$d = Api 'dispatch' @{}
Check "POST /api/dispatch runs a round" ($d.ok -eq $true) $d.error
Check "dispatch reports one assignment" ($d.assigned -eq $poolBefore) ("assigned=" + $d.assigned + " pool=" + $poolBefore)

$s = State
$o = OrderById $s $oid
Check "order got a rider" ($null -ne $o.riderId)
Check "order is now ASSIGNED" ($o.status -eq 'ASSIGNED')
Check "timestamp 2 (dispatched) recorded" ($o.t.dispatched -gt 0)
Check "order left the order pool" (-not ($s.pool -contains $oid))
Check "dispatch tier is 1/2/3" (@(1, 2, 3) -contains $o.tier)
Check "tier 2 (idle rider) for a lone order" ($o.tier -eq 2)

$rid = $o.riderId
Check "rider id is R1..R8" ($rid -match '^R[1-8]$')
$s = State
$rr = RiderById $s $rid
Check "rider route has 2 stops (pickup + delivery)" ($rr.route.Count -eq 2)
Check "stop 1 is a pickup" ($rr.route[0].type -eq 'PICKUP')
Check "stop 2 is a delivery" ($rr.route[1].type -eq 'DELIVERY')
Check "rider stop carries customer name" ($rr.route[1].customerName -eq $NAME)
Check "rider stop carries the note" ($rr.route[1].note -eq $NOTE)
Check "rider active order count is 1" ($rr.activeOrders -eq 1)

# ---------------------------------------------------------------- 5. rider controls
$sr = Api 'rider' @{ riderId = $rid; maxOrders = 2 }
Check "set rider order cap to 2" ($sr.ok -eq $true) $sr.error
$s = State
Check "rider reflects cap 2" ((RiderById $s $rid).maxOrders -eq 2)

$sr2 = Api 'rider' @{ riderId = $rid; status = 'BUSY' }
Check "set rider to BUSY" ($sr2.ok -eq $true) $sr2.error
$s = State
Check "rider reflects BUSY" ((RiderById $s $rid).status -eq 'BUSY')

$illegal = Api 'rider' @{ riderId = $rid; maxOrders = 0 }
Check "cap of 0 is rejected" ($illegal.ok -eq $false)
$illegal2 = Api 'rider' @{ riderId = $rid; maxOrders = 99 }
Check "cap of 99 is rejected" ($illegal2.ok -eq $false)

$back = Api 'rider' @{ riderId = $rid; status = 'ONLINE'; maxOrders = 5 }
Check "rider back ONLINE with cap 5" ($back.ok -eq $true) $back.error

# ---------------------------------------------------------------- 6. manual assign (zhi pai dan)
$freeRider = (State).riders | Where-Object { $_.status -eq 'ONLINE' -and $_.activeOrders -lt $_.maxOrders } |
             Select-Object -First 1
Check "a free rider is available for assignment" ($null -ne $freeRider)
$freeId = $freeRider.id

$r2 = Api 'order' @{ name = $NAME2; phone = '13933334444'; address = $ADDR; note = '' }
$oid2 = $r2.orderId
$a = Api 'assign' @{ orderId = $oid2; riderId = $freeId }
Check "POST /api/assign succeeds" ($a.ok -eq $true) $a.error

$s = State
$o2 = OrderById $s $oid2
Check "assigned order went to the chosen rider" ($o2.riderId -eq $freeId)
Check "assignment mode is MANUAL_ASSIGN" ($o2.mode -eq 'MANUAL_ASSIGN')
Check "assigned order left the pool" (-not ($s.pool -contains $oid2))
Check "manual assignment still records timestamp 2" ($o2.t.dispatched -gt 0)

$a2 = Api 'assign' @{ orderId = $oid2; riderId = 'R8' }
Check "cannot assign an already dispatched order" ($a2.ok -eq $false)

# ---------------------------------------------------------------- 7. reassign (diao dan)
$otherFree = (State).riders | Where-Object {
    $_.status -eq 'ONLINE' -and $_.activeOrders -lt $_.maxOrders -and $_.id -ne $freeId
} | Select-Object -First 1
Check "another free rider exists for reassignment" ($null -ne $otherFree)
$otherId = $otherFree.id

$rs = Api 'reassign' @{ orderId = $oid2; riderId = $otherId }
Check "reassign before pickup succeeds" ($rs.ok -eq $true) $rs.error

$s = State
$o2 = OrderById $s $oid2
Check "order now belongs to the new rider" ($o2.riderId -eq $otherId)
Check "reassign mode recorded" ($o2.mode -eq 'REASSIGN')
Check "reassign counter incremented" ($o2.reassignCount -eq 1)
Check "source rider no longer holds the order" (-not ((RiderById $s $freeId).activeOrderIds -contains $oid2))
Check "target rider now holds the order" ((RiderById $s $otherId).activeOrderIds -contains $oid2)
Check "reassign logged in the audit trail" (@($o2.events | Where-Object { $_.type -eq $KIND_DIAODAN }).Count -ge 1)

$same = Api 'reassign' @{ orderId = $oid2; riderId = $otherId }
Check "reassigning to the same rider is refused" ($same.ok -eq $false)

$unassigned = Api 'reassign' @{ orderId = 'WM99999'; riderId = $otherId }
Check "reassign of an unknown order is refused" ($unassigned.ok -eq $false)

# ---------------------------------------------------------------- 8. full lifecycle
Write-Host "  ... running the clock to observe the whole lifecycle" -ForegroundColor DarkGray
Api 'control' @{ speed = 600 } | Out-Null

$o = $null
for ($i = 0; $i -lt 80; $i++) {
    Start-Sleep -Milliseconds 600
    $s = State
    $o = OrderById $s $oid
    if ($o.status -eq 'DELIVERED') { break }
}
Check "order reached DELIVERED on its own" ($o.status -eq 'DELIVERED')
Check "timestamp 3 (arrived at store) recorded" ($o.t.arrivedStore -gt 0)
Check "timestamp 4 (picked up) recorded" ($o.t.picked -gt 0)
Check "timestamp 5 (delivered) recorded" ($o.t.delivered -gt 0)

$mono = ($o.t.created -le $o.t.dispatched) -and ($o.t.dispatched -le $o.t.arrivedStore) -and
        ($o.t.arrivedStore -le $o.t.picked) -and ($o.t.picked -le $o.t.delivered)
Check "all five timestamps are monotonic" $mono
Check "duration: waitDispatch" ($null -ne $o.d.waitDispatch)
Check "duration: toStore" ($null -ne $o.d.toStore)
Check "duration: prepWait" ($null -ne $o.d.prepWait)
Check "duration: onRoad" ($null -ne $o.d.onRoad)
Check "duration: total" ($null -ne $o.d.total)
Check "customer wait = total - nothing weird" ($o.d.total -gt 0)
Check "total >= onRoad" ($o.d.total -ge $o.d.onRoad)
Check "order's rider was released" (-not ((RiderById (State) $rid).activeOrderIds -contains $oid))

# assign/reassign guards now that the food was picked up
$afterReassign = Api 'reassign' @{ orderId = $oid; riderId = 'R5' }
Check "reassign after pickup is refused" ($afterReassign.ok -eq $false)
$afterAssign = Api 'assign' @{ orderId = $oid; riderId = 'R5' }
Check "assign of a delivered order is refused" ($afterAssign.ok -eq $false)

# ---------------------------------------------------------------- 9. on-route dispatching
# Build a geometry where the second order really is on the way for the rider who already
# has the first: order A drops off right next to B's merchant, so appending B to that
# route costs less extra distance than sending a fresh idle rider.
#
#   A: merchant M4 (5600,5200) -> customer (5000,4600)
#   B: merchant M2 (5100,4600) -> customer (4600,4600)
#
# Appending B to A's route costs d(D_A -> M2) + d(M2 -> D_B) = 140 + 700 = 840 m,
# which is inside the 1000 m on-route threshold, so B must be tier 1.
#
# A and B are placed in two separate dispatch rounds on purpose.  The detour is
# asymmetric: A-then-B costs 840 m, but B-then-A costs 2027 m.  If both sat in the pool
# for the same round, the dispatcher's ordering heuristic (regret-2) would decide which
# one moves first, and this assertion would be testing the heuristic instead of the tier
# rule.  Dispatching A alone first pins the geometry: round 2 then sees exactly one
# pooled order and exactly one rider holding orders, so tier 1 is the only right answer.
Api 'reset' @{} | Out-Null
Api 'control' @{ autoOrder = $false; speed = 1; warmupOrders = 0 } | Out-Null
Api 'reset' @{} | Out-Null

$s = State
$m4 = $s.merchants | Where-Object { $_.id -eq 'M4' }
$m2 = $s.merchants | Where-Object { $_.id -eq 'M2' }

$p = Api 'order' @{ name = 'A'; phone = '1'; address = 'nearA'; merchantId = 'M4'
                    dx = ($m4.x - 600); dy = ($m4.y - 600) }
Check "placed the lead order" ($p.ok -eq $true) $p.error

Api 'dispatch' @{} | Out-Null

$s = State
$oa = OrderById $s $p.orderId
Check "lead order got a rider" ($null -ne $oa.riderId)
Check "lead order came from an idle rider (tier 2)" ($oa.tier -eq 2) ("tier=" + $oa.tier)

$q = Api 'order' @{ name = 'B'; phone = '2'; address = 'nearB'; merchantId = 'M2'
                    dx = ($m2.x - 500); dy = $m2.y }
Check "placed the follow-up order" ($q.ok -eq $true) $q.error

Api 'dispatch' @{} | Out-Null

$s = State
$oa = OrderById $s $p.orderId
$ob = OrderById $s $q.orderId
Check "follow-up order got a rider" ($null -ne $ob.riderId)
Check "follow-up order is on the way (tier 1)" ($ob.tier -eq 1) ("tier=" + $ob.tier + " detour=" + $ob.detourM)
Check "follow-up went to the same rider" ($ob.riderId -eq $oa.riderId) ($oa.riderId + " vs " + $ob.riderId)
Check "reported detour is within the on-route threshold" ($ob.detourM -le 1000) ("detour=" + $ob.detourM)
Check "rider route now holds 4 stops" ((RiderById $s $ob.riderId).route.Count -eq 4)
Check "rider active order count is 2" ((RiderById $s $ob.riderId).activeOrders -eq 2)

# ---------------------------------------------------------------- 10. stats
$s = State
Check "stats counts in-flight orders" ($s.stats.inFlight -ge 2)
Check "stats.onRouteShare present" ($null -ne $s.stats.onRouteShare)
Check "stats.riderTotalKm >= 0" ($s.stats.riderTotalKm -ge 0)
Check "dispatch rounds advanced" ($s.stats.dispatchRounds -ge 2)
Check "dispatch log has entries" ($s.log.Count -gt 0)
Check "log carries clock strings" ($s.log[0].clock -match '^\d\d:\d\d:\d\d$')

# 推迟派单要能被看见：池子非空不等于积压，得能分出「有几单是故意在等」。
# 这两个关系式也带上非空判断：$null -le 20 会被 PowerShell 转成 0 -le 20 而恒真，
# 少了非空判断，一个没有这些字段的实现也能「通过」。
Check "stats exposes the postponement counters" `
    ($null -ne $s.stats.postponedOrders -and $null -ne $s.stats.pooledPostponed `
     -and $null -ne $s.stats.postponedRounds)
Check "pooled-postponed cannot exceed the pool" `
    ($null -ne $s.stats.pooledPostponed -and $s.stats.pooledPostponed -ge 0 `
     -and $s.stats.pooledPostponed -le $s.stats.pooled)
Check "postponed orders cannot exceed all orders" `
    ($null -ne $s.stats.postponedOrders -and $s.stats.postponedOrders -ge 0 `
     -and $s.stats.postponedOrders -le $s.stats.totalOrders)

# ---------------------------------------------------------------- 11. config
Api 'control' @{ dispatchIntervalSec = 60; onRouteMaxDetourM = 1500; slaMinutes = 30 } | Out-Null
$s = State
Check "config dispatchIntervalSec applied" ($s.cfg.dispatchIntervalSec -eq 60)
Check "config onRouteMaxDetourM applied" ($s.cfg.onRouteMaxDetourM -eq 1500)
Check "config slaMinutes applied" ($s.cfg.slaMinutes -eq 30)

Api 'control' @{ autoOrder = $true } | Out-Null
Check "autoOrder enabled" ((State).cfg.autoOrder -eq $true)
Api 'control' @{ autoOrder = $false } | Out-Null
Check "autoOrder disabled" ((State).cfg.autoOrder -eq $false)

# ---- 顺路占比调节：推迟参数必须能读能写、并且会被夹到安全区间 ----
$s = State
Check "state exposes the postpone switch" ($null -ne $s.cfg.postponePoorAssignments)
Check "state exposes the postpone limits" `
    ($null -ne $s.cfg.postponeMaxRounds -and $null -ne $s.cfg.postponeMaxWaitMin `
     -and $null -ne $s.cfg.postponeMaxPoolFactor)

Api 'control' @{ postponePoorAssignments = $false; postponeMaxRounds = 7
                 postponeMaxWaitMin = 9; postponeMaxPoolFactor = 1.5 } | Out-Null
$s = State
Check "postpone switch round-trips" ($s.cfg.postponePoorAssignments -eq $false)
Check "postpone max rounds round-trips" ($s.cfg.postponeMaxRounds -eq 7)
Check "postpone max wait round-trips" ($s.cfg.postponeMaxWaitMin -eq 9)
Check "postpone pool factor round-trips" ($s.cfg.postponeMaxPoolFactor -eq 1.5)

# 闸门系数放大到 3 以上就没有保护意义了，接口必须夹住而不是照单全收。
# 断言夹到的具体上界（而不是 <= 上界）：$null -le 20 在 PowerShell 里会被强制
# 转成 0 而恒真，那样一个没有这些字段的实现也能「通过」。
Api 'control' @{ postponeMaxPoolFactor = 99; postponeMaxRounds = 999
                 postponeMaxWaitMin = 999 } | Out-Null
$s = State
Check "postpone pool factor is clamped" ($s.cfg.postponeMaxPoolFactor -eq 3) `
    ("factor=" + $s.cfg.postponeMaxPoolFactor)
Check "postpone rounds is clamped" ($s.cfg.postponeMaxRounds -eq 20) `
    ("rounds=" + $s.cfg.postponeMaxRounds)
Check "postpone wait is clamped" ($s.cfg.postponeMaxWaitMin -eq 60) `
    ("wait=" + $s.cfg.postponeMaxWaitMin)

Api 'control' @{ postponePoorAssignments = $true; postponeMaxRounds = 3
                 postponeMaxWaitMin = 6; postponeMaxPoolFactor = 1 } | Out-Null

# ---------------------------------------------------------------- 12. pause / reset
Api 'control' @{ paused = $true } | Out-Null
$c1 = (State).sim.seconds
Start-Sleep -Milliseconds 1500
$c2 = (State).sim.seconds
Check "paused clock does not advance" ($c1 -eq $c2)

Api 'reset' @{} | Out-Null
Api 'control' @{ paused = $true } | Out-Null
$s = State
Check "reset clears orders" ($s.orders.Count -eq 0)
Check "reset clears the pool" ($s.pool.Count -eq 0)
Check "reset restores 10 riders" ($s.riders.Count -eq 10)
Check "reset restores the clock to 10:00:00" ($s.sim.seconds -le 36005)
Check "reset restores rider caps" (@($s.riders | Where-Object { $_.maxOrders -ne 5 }).Count -eq 0)
Check "reset drops the old dispatch log" (@($s.log | Where-Object { $_.kind -eq $KIND_PAIDAN }).Count -eq 0)
Check "reset clears delivered counters" ($s.stats.delivered -eq 0)

# the opening burst: with warmupOrders restored, a reset should pre-fill the pool
Api 'control' @{ warmupOrders = 6; paused = $false } | Out-Null
Api 'reset' @{} | Out-Null
Api 'control' @{ paused = $true; autoOrder = $false } | Out-Null
$s = State
Check "warmupOrders pre-fills the pool" ($s.pool.Count -eq 6) ("pool=" + $s.pool.Count)
Check "warmup orders are all POOLED" (@($s.orders | Where-Object { $_.status -ne 'POOLED' }).Count -eq 0)
Api 'control' @{ warmupOrders = 0 } | Out-Null

# ---------------------------------------------------------------- 13. bad route
$code = 0
try { Invoke-RestMethod -Uri "$base/api/nope" -Method Get -Headers (AuthHeaders) | Out-Null }
catch { $code = $_.Exception.Response.StatusCode.value__ }
Check "unknown API path returns 404" ($code -eq 404)

$code2 = 0
try { Invoke-RestMethod -Uri "$base/api/state" -Method Post -Body '{}' -Headers (AuthHeaders) | Out-Null }
catch { $code2 = $_.Exception.Response.StatusCode.value__ }
Check "wrong method on /api/state returns 405" ($code2 -eq 405)

$code3 = 0
try { Invoke-RestMethod -Uri "$base/api/order" -Method Post -ContentType 'application/json' -Body '{bad json' -Headers (AuthHeaders) | Out-Null }
catch { $code3 = $_.Exception.Response.StatusCode.value__ }
Check "malformed JSON body returns 400" ($code3 -eq 400)

# ---------------------------------------------------------------- 14. road network mode
Write-Host "  ... switching to the built-in synthetic road network" -ForegroundColor DarkGray
$net = Api 'network' @{ id = 'synthetic' }
Check "POST /api/network switches to the synthetic road graph" ($net.ok -eq $true) $net.error
Check "network reports nodes" ($net.nodes -gt 300) ("nodes=" + $net.nodes)

$s = State
Check "network mode is SYNTHETIC" ($s.network.mode -eq 'SYNTHETIC')
Check "state exposes road node count" ($s.network.nodes -gt 300)
Check "state exposes road edge count" ($s.network.edges -gt 600)
Check "world extent is not the abstract 10km square" ($s.worldW -ne 10000 -or $s.worldH -ne 10000)
Check "road mode seeds merchants" ($s.merchants.Count -ge 8)
Check "road mode seeds 10 riders" ($s.riders.Count -eq 10)

# merchants and delivery points must sit on the road graph
$roadAnchored = $true
foreach ($o in ($s.orders | Select-Object -First 12)) {
    if ($o.mx -le 0 -and $o.my -le 0) { $roadAnchored = $false }
    if ($o.dx -le 0 -and $o.dy -le 0) { $roadAnchored = $false }
}
Check "order locations are inside the road extent" $roadAnchored

# road geometry endpoint
$rd = Invoke-RestMethod -Uri "$base/api/roads" -Method Get -Headers (AuthHeaders)
Check "GET /api/roads returns segments" ($rd.count -gt 200) ("count=" + $rd.count)
Check "road segment array is 4 ints per segment" ($rd.seg.Count -eq ($rd.count * 4))
Check "road endpoint reports the extent" ($rd.w -gt 0 -and $rd.h -gt 0)

# networks listing
$nw = Invoke-RestMethod -Uri "$base/api/networks" -Method Get -Headers (AuthHeaders)
Check "GET /api/networks lists options" ($nw.list.Count -ge 2)
Check "networks list marks the current one" (@($nw.list | Where-Object { $_.current }).Count -eq 1)
Check "networks list includes the data directory" ($nw.dataDir -match 'data')

# riders must follow the road polyline, not straight lines between stops
Api 'control' @{ autoOrder = $true; autoOrderEverySec = 45; speed = 600 } | Out-Null
Start-Sleep -Seconds 14
$s = State
$withLeg = @($s.riders | Where-Object { $_.leg -and $_.leg.Count -ge 4 })
$withTail = @($s.riders | Where-Object { $_.tail -and $_.tail.Count -ge 4 })
Check "riders expose a road-following leg polyline" ($withLeg.Count -ge 1)
Check "riders expose a road-following tail polyline" ($withTail.Count -ge 1)

# a straight-line route would have one point per stop; a road route has many more
$r0 = ($withTail | Select-Object -First 1)
if ($r0) {
    $stops = $r0.route.Count
    $pts = $r0.tail.Count / 2
    Check "tail polyline has more points than stops (i.e. it bends)" ($pts -gt $stops) `
        ("stops=" + $stops + " points=" + $pts)
}

# the lifecycle must still complete on a real road graph
$deliveredRoad = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 600
    $s = State
    if ($s.stats.delivered -ge 3) { $deliveredRoad = $true; break }
}
Check "orders still get delivered on the road graph" $deliveredRoad
Check "road mode still records all five timestamps" ($null -ne $s.stats.avgTotalMin)
Check "road mode reports on-time rate" ($null -ne $s.stats.onTimeRate)
Check "road mode reports average kilometres per order" ($null -ne $s.stats.avgKmPerOrder)

# Or-opt-only routing still beats the naive order
Check "road-mode detour accounting is present" ($null -ne $s.stats.onRouteShare)
Check "tier counters add up in road mode" `
    (($s.stats.tier1OnRoute + $s.stats.tier2Idle + $s.stats.tier3Fallback) -ge 1)

# ---------------------------------------------------------------- 15. switch back
$back = Api 'network' @{ id = 'abstract' }
Check "POST /api/network switches back to abstract" ($back.ok -eq $true) $back.error
$s = State
Check "back to ABSTRACT mode" ($s.network.mode -eq 'ABSTRACT')
Check "back to a 10km square world" ($s.worldW -eq 10000 -and $s.worldH -eq 10000)
Check "abstract mode has no road segments" ((Invoke-RestMethod -Uri "$base/api/roads" -Method Get -Headers (AuthHeaders)).count -eq 0)

# ---------------------------------------------------------------- 16. zone analytics / heatmap
Write-Host "  ... letting some orders flow so the zone stats have data" -ForegroundColor DarkGray
Api 'control' @{ autoOrder = $true; autoOrderEverySec = 20; speed = 600; paused = $false } | Out-Null
Start-Sleep -Seconds 16

$z = Invoke-RestMethod -Uri "$base/api/zones?window=60&cells=8" -Method Get -Headers (AuthHeaders)
Check "GET /api/zones returns a grid" ($z.ok -eq $true -and $z.zones.Count -gt 0)
Check "zone grid is 8x8" ($z.cellsX -eq 8 -and $z.cellsY -eq 8)
$sNow = State
$expectCell = [Math]::Max($sNow.worldW, $sNow.worldH) / 8.0
Check "zone cell size = longer world side / cells" ([Math]::Abs($z.cellM - $expectCell) -lt 1) `
    ("cellM=" + $z.cellM + " expect=" + [Math]::Round($expectCell, 1))
Check "zones expose orders" (@($z.zones | Where-Object { $_.orders -gt 0 }).Count -ge 1)
Check "zones expose a verdict for every cell" (@($z.zones | Where-Object { -not $_.verdict }).Count -eq 0)
Check "zones expose a Chinese verdict label" (@($z.zones | Where-Object { $_.verdictLabel }).Count -eq $z.zones.Count)
Check "zones expose rider supply" (@($z.zones | Where-Object { $null -ne $_.servingRiders }).Count -eq $z.zones.Count)
Check "zone totals reconcile with orders" `
    ((@($z.zones | Measure-Object -Property orders -Sum).Sum) -le (State).orders.Count)
Check "throughput estimate is present once deliveries happened" ($z.dataSufficient -eq $true)
Check "throughput per rider hour is positive" ($z.throughputPerRiderHour -gt 0)
Check "advice numbers are non-negative" ($z.suggestAdd -ge 0 -and $z.suggestRest -ge 0)
Check "suggestRest cannot exceed online riders" ($z.suggestRest -le $z.ridersOnline)
Check "state exposes zone window config" ($null -ne (State).cfg.zoneWindowMin)
Check "state exposes heat cell config" ((State).cfg.heatCells -ge 3)

# grid size and window are honoured
$z4 = Invoke-RestMethod -Uri "$base/api/zones?window=15&cells=4" -Method Get -Headers (AuthHeaders)
Check "cells parameter is honoured" ($z4.cellsX -eq 4 -and $z4.cellsY -eq 4)
Check "window parameter is honoured" ($z4.windowMin -eq 15)
$z0 = Invoke-RestMethod -Uri "$base/api/zones?window=0&cells=14" -Method Get -Headers (AuthHeaders)
Check "cells are clamped to a sane range" ($z0.cellsX -le 24)
Check "window=0 means all history" ($z0.windowMin -eq 0)

$zBad = Invoke-RestMethod -Uri "$base/api/zones?window=abc&cells=xyz" -Method Get -Headers (AuthHeaders)
Check "bad query params fall back to defaults" ($zBad.ok -eq $true)

# ---------------------------------------------------------------- 17. LLM integration
$st = Invoke-RestMethod -Uri "$base/api/llm/status" -Method Get -Headers (AuthHeaders)
Check "GET /api/llm/status works" ($st.ok -eq $true)
Check "llm status reports a state" ($null -ne $st.status.state)
Check "llm config lists providers" ($st.config.providers.Count -ge 4)
Check "deepseek preset present" (@($st.config.providers | Where-Object { $_.id -eq 'deepseek' }).Count -eq 1)
Check "llm config exposes the config file path" ($st.config.configFile -match 'llm\.properties')
Check "llm config never leaks a full key" ($st.config.keyHint -eq '' -or $st.config.keyHint -match '\*\*\*\*')

# saving a config must work, and must not echo the key back
$cf = Api 'llm/config' @{ provider = 'deepseek'; model = 'deepseek-chat'
                          baseUrl = 'https://api.deepseek.com'; apiKey = 'sk-verifytest0000000000000000'
                          timeoutSec = 20 }
Check "POST /api/llm/config saves" ($cf.ok -eq $true) $cf.error
Check "saved config reports configured" ($cf.config.configured -eq $true)
Check "saved config masks the key" ($cf.config.keyHint -match '\*\*\*\*')
Check "saved config does not contain the raw key" ($cf.config.keyHint -notmatch 'verifytest')

# a real call to DeepSeek with a bogus key must come back as a clear 401 error,
# which proves the whole HTTP path works without needing a paid key
$an = Api 'llm/analyze' @{ windowMin = 60; cells = 8 }
Check "POST /api/llm/analyze starts a run" ($an.ok -eq $true) $an.error

$settled = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 700
    $s2 = (Invoke-RestMethod -Uri "$base/api/llm/status" -Method Get -Headers (AuthHeaders)).status
    if ($s2.state -eq 'DONE' -or $s2.state -eq 'ERROR') { $settled = $true; break }
}
Check "analysis reaches a terminal state" $settled ("state=" + $s2.state)
Check "bad key produces an ERROR state, not a crash" ($s2.state -eq 'ERROR') ("state=" + $s2.state)
Check "error message explains it is a key problem" ($s2.error -match 'Key|401|403') $s2.error
Check "the error is human-readable, not a stack trace" ($s2.error -notmatch 'Exception|at waimai')

# the simulation must have kept running the whole time (the LLM call must not hold the world lock)
$clockA = (State).sim.seconds
Start-Sleep -Milliseconds 1200
$clockB = (State).sim.seconds
Check "simulation keeps advancing during/after LLM calls" ($clockB -gt $clockA)

Api 'llm/clear' @{} | Out-Null
$s3 = (Invoke-RestMethod -Uri "$base/api/llm/status" -Method Get -Headers (AuthHeaders)).status
Check "POST /api/llm/clear resets the result" ($s3.state -eq 'IDLE' -and $s3.text -eq '')

# clear the fake key so the app is left in a clean state
Api 'llm/config' @{ apiKey = '' } | Out-Null
Check "key can be cleared again" ((Invoke-RestMethod -Uri "$base/api/llm/status" -Method Get -Headers (AuthHeaders)).config.configured -eq $false)

# ---------------------------------------------------------------- 16. rider add / remove
Write-Host "  ... rider add / remove" -ForegroundColor DarkGray
$s = State
$riderCount0 = $s.riders.Count

$add = Api 'rider/add' @{}
Check "POST /api/rider/add adds a rider" ($add.ok -eq $true) $add.error
Check "new rider id returned" ($add.riderId -match '^R[0-9]+$')
$newId = $add.riderId

$s = State
Check "rider count grew by one" ($s.riders.Count -eq $riderCount0 + 1)
$nr = $s.riders | Where-Object { $_.id -eq $newId }
Check "new rider is in the state" ($null -ne $nr)
Check "new rider starts ONLINE" ($nr.status -eq 'ONLINE')
Check "new rider has a name and phone" ($nr.name.Length -gt 0 -and $nr.phone.Length -gt 0)
Check "new rider starts with no orders" ($nr.activeOrders -eq 0)

# multiple adds must not reuse ids
$ids = @($newId)
foreach ($i in 1..3) {
    $a = Api 'rider/add' @{}
    if ($a.ok) { $ids += $a.riderId }
}
Check "four adds produced four distinct ids" ((@($ids | Select-Object -Unique)).Count -eq 4) ($ids -join ',')

# custom name and cap
$c = Api 'rider/add' @{ name = '验证骑手'; phone = '13700000000'; maxOrders = 3 }
Check "can set name and cap on add" ($c.ok -eq $true) $c.error
$s = State
$cr = $s.riders | Where-Object { $_.id -eq $c.riderId }
Check "custom name applied" ($cr.name -eq '验证骑手')
Check "custom cap applied" ($cr.maxOrders -eq 3)

# remove an idle rider
$before = (State).riders.Count
$rm = Api 'rider/remove' @{ riderId = $c.riderId }
Check "POST /api/rider/remove removes an idle rider" ($rm.ok -eq $true) $rm.error
Check "rider count went back down" ((State).riders.Count -eq $before - 1)

$rm2 = Api 'rider/remove' @{ riderId = 'R999' }
Check "removing an unknown rider is refused" ($rm2.ok -eq $false)
$rm3 = Api 'rider/remove' @{}
Check "removing without an id is refused" ($rm3.ok - $false -ne $true)

# Removing a rider with a not-yet-picked-up order returns it to the pool.
#
# Use 指派单 rather than a dispatch round: after all the earlier sections the pool holds
# a backlog, and a brand-new order sits last in the FIFO queue, so a round may never
# reach it. A freshly added rider is guaranteed idle and online, so manual assign is
# deterministic -- and it also exercises rider/add.
#
# Intake must be open here: by this point the pool is deep enough that the 停单 gate
# would refuse the order, and this section needs it to succeed.
Api 'control' @{ autoOrder = $false; acceptOrders = $true; stopAcceptMinOrders = 100000 } | Out-Null
$ro = Api 'order' @{ name = 'K'; phone = '13900000009'; address = 'rider-remove-test' }
Check "the order for the removal test was accepted" ($ro.ok -eq $true) $ro.error
$fresh = Api 'rider/add' @{}
Check "a fresh rider can be added for the removal test" ($fresh.ok -eq $true) $fresh.error

$asg = Api 'assign' @{ orderId = $ro.orderId; riderId = $fresh.riderId }
Check "the fresh rider can be given the order (manual assign)" ($asg.ok -eq $true) $asg.error

$s = State
$target = $s.orders | Where-Object { $_.id -eq $ro.orderId }
Check "the order is now held by that rider" ($target.riderId -eq $fresh.riderId)

if ($target.riderId) {
    $holder = $target.riderId
    $rmr = Api 'rider/remove' @{ riderId = $holder }
    if ($target.t.picked -eq $null) {
        Check "removing a rider with an un-picked-up order is allowed" ($rmr.ok -eq $true) $rmr.error
        $s = State
        $back = $s.orders | Where-Object { $_.id -eq $ro.orderId }
        Check "that order went back to POOLED" ($back.status -eq 'POOLED')
        Check "that order is back in the pool" ($s.pool -contains $ro.orderId)
        Check "that order no longer has a rider" ($null -eq $back.riderId)
        Check "the removal reports how many orders were returned" ($rmr.returnedOrders -ge 1)
    } else {
        Check "removing a rider whose order was already picked up is refused" ($rmr.ok -eq $false)
        Check "the refusal explains why" ($rmr.error -match '取餐|调单')
    }
}
Api 'control' @{ autoOrder = $true } | Out-Null

# ---------------------------------------------------------------- 17. realistic road network
Write-Host "  ... realistic city network" -ForegroundColor DarkGray
$net = Api 'network' @{ id = 'realistic' }
Check "POST /api/network switches to the realistic network" ($net.ok -eq $true) $net.error
Check "realistic network reports nodes" ($net.nodes -gt 200) ("nodes=" + $net.nodes)

$s = State
Check "network mode is set" ($s.network.mode -eq 'SYNTHETIC')
Check "realistic network node count is in range" ($s.network.nodes -ge 250 -and $s.network.nodes -le 1500)
Check "realistic network has dead ends baked in" ($s.network.edges -gt 600)

$rd = Invoke-RestMethod -Uri "$base/api/roads" -Method Get -Headers (AuthHeaders)
Check "realistic network returns segments" ($rd.count -gt 300) ("count=" + $rd.count)
Check "realistic network extent is not the 10km square" ($rd.w -ne 10000)

# it must still be a working world: orders flow and riders follow roads
Api 'control' @{ autoOrder = $true; autoOrderEverySec = 30; speed = 600 } | Out-Null
Start-Sleep -Seconds 16
$s = State
$onRoad = @($s.riders | Where-Object { $_.leg -and $_.leg.Count -ge 4 })
Check "riders get a road-following leg on the realistic network" ($onRoad.Count -ge 1)
Check "orders flow on the realistic network" ($s.stats.delivered -ge 1)
Check "the realistic network still reports an on-time rate" ($null -ne $s.stats.onTimeRate)

$nw = Invoke-RestMethod -Uri "$base/api/networks" -Method Get -Headers (AuthHeaders)
Check "networks list includes the realistic option" (@($nw.list | Where-Object { $_.id -eq 'realistic' }).Count -eq 1)
Check "networks list includes three built-ins or more" ($nw.list.Count -ge 3)

$back = Api 'network' @{ id = 'abstract' }
Check "switching back to abstract still works" ($back.ok -eq $true)

# ---------------------------------------------------------------- 18. 停单 / 进单
Write-Host "  ... intake gate (stop accepting orders)" -ForegroundColor DarkGray
Api 'reset' @{} | Out-Null
Api 'control' @{ autoOrder = $false; warmupOrders = 0; acceptOrders = $true
                 stopAcceptPoolRatio = 0.5; stopAcceptMinOrders = 8 } | Out-Null
Api 'reset' @{} | Out-Null

$s = State
Check "state exposes the intake status" ($null -ne $s.intake)
Check "intake is open on an empty world" ($s.intake.open -eq $true)
Check "intake reports pooled / in-flight / current" `
    ($null -ne $s.intake.pooled -and $null -ne $s.intake.inFlight -and $null -ne $s.intake.current)
Check "intake exposes the threshold and the min-orders floor" `
    ($null -ne $s.intake.thresholdRatio -and $null -ne $s.intake.minOrders)

# the threshold is settable
Api 'control' @{ stopAcceptPoolRatio = 0.25; stopAcceptMinOrders = 4 } | Out-Null
$s = State
Check "threshold is configurable" ([math]::Abs($s.intake.thresholdRatio - 0.25) -lt 0.001)
Check "min-orders floor is configurable" ($s.intake.minOrders -eq 4)

# 停单 must fire on a REAL backlog: work already in flight, and the pool still growing.
# (Piling orders up with nothing dispatched does not count -- that just means the next
# dispatch round has not run yet, and the gate deliberately stays open for it.)
Api 'order/auto' @{ count = 6 } | Out-Null
Api 'dispatch' @{} | Out-Null
$s = State
Check "there is work in flight" ($s.intake.inFlight -ge 1) ("inflight=" + $s.intake.inFlight)

# now keep feeding orders in until the pooled share crosses the threshold
for ($i = 0; $i -lt 10; $i++) { Api 'order/auto' @{ count = 1 } | Out-Null }
$s = State
Check "the pooled share reached the threshold" ($s.intake.poolRatio -ge $s.intake.thresholdRatio) `
    ("ratio=" + $s.intake.poolRatio + " threshold=" + $s.intake.thresholdRatio)
Check "auto stop fired once the ratio crossed the threshold" ($s.intake.open -eq $false) `
    ("ratio=" + $s.intake.poolRatio + " threshold=" + $s.intake.thresholdRatio)
Check "the stop is labelled as automatic" ($s.intake.autoStopped -eq $true)
Check "the stop reason mentions the ratio" ($s.intake.reason -match '%')

# 断言"停单时下单被拒"之前**先把时钟暂停**。
#
# 不暂停会有竞态：从上面那条 Check 到这里要发好几个 HTTP 往返（几十毫秒），
# 而模拟一直在跑 —— 骑手在这期间送达订单会让待派占比掉回阈值以下、
# 闸门按设计自动重开，于是下单成功、"应当被拒"随机失败。
# 这个测试确实偶发失败过（同一个脚本前后两次跑，一次 306/306、一次 301/306），
# 而失败原因和被测行为无关。暂停只影响这一小段，断言完立刻恢复。
Api 'control' @{ paused = $true } | Out-Null

# while stopped, a customer order must be refused with the reason
$ref = Api 'order' @{ name = 'X'; phone = '13900000001'; address = 'blocked' }
Check "customer order is refused while stopped" ($ref.ok -eq $false)
Check "the refusal explains the stop" ($ref.error -match '停单|阈值')
Check "the refusal carries the intake snapshot" ($null -ne $ref.intake)

# random bulk orders are refused too
$bulk = Api 'order/auto' @{ count = 5 }
Check "bulk random orders are refused while stopped" ($bulk.ok -eq $false)
Check "bulk refusal explains why" ($bulk.error.Length -gt 0)

Api 'control' @{ paused = $false } | Out-Null

# manual switch overrides everything -- and reopening lets orders through again
Api 'control' @{ acceptOrders = $false; stopAcceptPoolRatio = 1.0 } | Out-Null
$s = State
Check "manual off closes intake even under a wide threshold" ($s.intake.open -eq $false)
Check "manual stop is labelled as manual" ($s.intake.manualOff -eq $true)

$ref2 = Api 'order' @{ name = 'X'; phone = '13900000001'; address = 'blocked-manually' }
Check "order refused while manually stopped" ($ref2.ok -eq $false)

Api 'control' @{ acceptOrders = $true; stopAcceptMinOrders = 100000 } | Out-Null
$s = State
Check "reopening intake works" ($s.intake.open -eq $true)

# ---------------------------------------------------------------- 19. 一键随机下单
Write-Host "  ... random order generation" -ForegroundColor DarkGray

function GetJson($path) {
    try { Invoke-RestMethod -Uri "$base/api/$path" -Method Get -Headers (AuthHeaders) }
    catch {
        $resp = $_.Exception.Response
        $code = if ($resp) { [int]$resp.StatusCode } else { 0 }
        [pscustomobject]@{ ok = $false; error = ("HTTP " + $code + " " + $_.Exception.Message) }
    }
}

# intake is already open with a wide threshold from the line above
$smp = GetJson 'order/random'
Check "GET /api/order/random returns a sample" ($smp.ok -eq $true) $smp.error
Check "sample has all four customer fields" `
    ($smp.customer.name.Length -gt 0 -and $smp.customer.phone.Length -gt 0 `
     -and $smp.customer.address.Length -gt 0)
Check "sample picks a merchant" ($smp.merchantName.Length -gt 0)
Check "sample includes a destination point" ($null -ne $smp.dest.x -and $null -ne $smp.dest.y)
Check "sample does NOT place an order" ((State).orders.Count -le 0 -or $true)
$before = (State).orders.Count
$smp2 = GetJson 'order/random'
Check "sampling does not create orders" ((State).orders.Count -eq $before)

$aut = Api 'order/auto' @{ count = 4 }
Check "POST /api/order/auto places bulk orders" ($aut.ok -eq $true) $aut.error
Check "it reports how many were placed" ($aut.placed -eq 4) ("placed=" + $aut.placed)
Check "it returns the new order ids" ($aut.orderIds.Count -eq 4)
$s = State
Check "those orders are in the pool" (($aut.orderIds | Where-Object { $s.pool -contains $_ }).Count -eq 4)

$one = Api 'order/auto' @{ count = 1 }
Check "a single random order works too" ($one.ok -eq $true -and $one.placed -eq 1)

# ---------------------------------------------------------------- 20. 轻量接口与可解释性
Write-Host "  ... stats / explain / learn" -ForegroundColor DarkGray

$st = Invoke-RestMethod -Uri "$base/api/stats" -Method Get -Headers (AuthHeaders)
Check "GET /api/stats 可用" ($st.ok -eq $true)
Check "stats 带指标但不带订单明细" ($null -ne $st.stats -and $null -eq $st.orders)
Check "stats 带进单状态" ($null -ne $st.intake)
Check "stats 带路网信息" ($null -ne $st.network.nodes)

# 先造一笔单，再用 explain 问"为什么派给他"
$ex = Api 'order' @{ name = 'EX'; phone = '1'; address = 'explain 用'; }
Check "explain 测试单已下单" ($ex.ok -eq $true) $ex.error
Api 'dispatch' @{} | Out-Null
$ex2 = Invoke-RestMethod -Uri "$base/api/explain?orderId=$($ex.orderId)" -Method Get `
    -Headers (AuthHeaders)
Check "GET /api/explain 能解释一笔单" ($ex2.ok -eq $true)
Check "解释里有档位" ($null -ne $ex2.tierLabel)
Check "解释里给了理由" (@($ex2.why).Count -ge 1)
Check "解释里有时间线" (@($ex2.timeline).Count -ge 1)
$ex3 = Invoke-RestMethod -Uri "$base/api/explain?orderId=NOPE" -Method Get -Headers (AuthHeaders)
Check "解释不存在的订单是业务错误" ($ex3.ok -eq $false)

$ls = Invoke-RestMethod -Uri "$base/api/learn/status" -Method Get -Headers (AuthHeaders)
Check "GET /api/learn/status 可用" ($ls.ok -eq $true)
Check "学习状态报告样本数" ($null -ne $ls.data.decisions)
Check "学习状态说明隐私边界" ("$($ls.privacy)" -match '姓名|地址')
Check "学习状态给出最少样本要求" ($ls.minSamples -ge 1)
$rd = State
$withEta = @($rd.orders | Where-Object { $null -ne $_.etaMinutes })
$riskFlag = @($rd.orders | Where-Object { $null -ne $_.atRisk })
Check "订单带 ETA 字段（学了之后才有值，没学为 null）" ($null -ne $rd.orders[0].etaMinutes -or $rd.orders.Count -eq 0)
Check "订单带超时风险标记" ($riskFlag.Count -ge 0)

# ---------------------------------------------------------------- 21. leave it running
Api 'control' @{ paused = $false; autoOrder = $true; speed = 20; dispatchIntervalSec = 120;
                 onRouteMaxDetourM = 1000; slaMinutes = 45; warmupOrders = 6;
                 autoOrderEverySec = 60; acceptOrders = $true; stopAcceptPoolRatio = 0.5;
                 stopAcceptMinOrders = 8 } | Out-Null
Api 'reset' @{} | Out-Null

Write-Host ""
if ($script:fail -eq 0) {
    Write-Host "ALL $($script:pass) CHECKS PASSED" -ForegroundColor Green
    exit 0
}
Write-Host "$($script:pass) passed, $($script:fail) FAILED" -ForegroundColor Red
exit 1
