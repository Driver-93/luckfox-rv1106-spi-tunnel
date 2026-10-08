# Single-client takeover: end-to-end policy test.
#
# Simulates two browser "tabs" purely at the HTTP level, by tagging every
# request with a different X-Page-Id header. That exercises the real policy
# (claim on pageinfo, revoke on takeover, 403 for the non-owner, takeover
# button) without needing two actual browsers.
#
# What this does NOT cover: the mediamtx-level kick of a live WebRTC session
# (needs a real browser to create one) and the on-screen banner. Those are
# verified separately / by eye.
$ErrorActionPreference = 'Continue'
$Target = '192.168.3.69'

$fails = @()
function Check($name, $got, $want) {
    $ok = ("$got" -eq "$want")
    if (-not $ok) { $script:fails += $name }
    Write-Host ("  {0} {1,-56} got={2} want={3}" -f $(if ($ok) { 'OK ' } else { 'FAIL' }), $name, $got, $want)
}

function Req($path, $method, $tab, $body) {
    $h = @{}
    if ($tab) { $h['X-Page-Id'] = $tab }
    $args = @{ Uri = "http://${Target}${path}"; Method = $method; TimeoutSec = 8
               UseBasicParsing = $true; Headers = $h }
    if ($body) {
        $args['Body'] = $body
        $args['ContentType'] = 'application/json'
    }
    try {
        $r = Invoke-WebRequest @args
        $c = $r.Content; if ($c -is [byte[]]) { $c = [Text.Encoding]::UTF8.GetString($c) }
        return @{ code = [int]$r.StatusCode; text = $c }
    } catch {
        $code = 0
        try { $code = [int]$_.Exception.Response.StatusCode } catch {}
        return @{ code = $code; text = "" }
    }
}

# Invoke-WebRequest throws on non-2xx and does not hand back the body, so the
# 403 payload has to be read over a raw socket.
function RawReq($reqText) {
    $c = New-Object System.Net.Sockets.TcpClient
    $c.Connect($Target, 80)
    $s = $c.GetStream()
    $b = [Text.Encoding]::ASCII.GetBytes($reqText)
    $s.Write($b, 0, $b.Length); $s.Flush()
    $all = (New-Object IO.StreamReader($s)).ReadToEnd()
    $c.Close()
    return $all
}

$A = 'tab-AAAA-test'
$B = 'tab-BBBB-test'

Write-Host "=== 1) tab A loads the page (pageinfo claims ownership) ==="
$r = Req '/api/pageinfo' 'POST' $A '{"page":"test"}'
Check "A pageinfo -> 200" $r.code 200
$j = $r.text | ConvertFrom-Json
Check "A claim accepted" $j.claim "True"
$r = Req '/api/status' 'GET' $A
$st = $r.text | ConvertFrom-Json
Check "A sees owner=true" $st.owner.owner "True"

Write-Host ""
Write-Host "=== 2) tab B loads the page -> must take over ==="
$r = Req '/api/pageinfo' 'POST' $B '{"page":"test"}'
$j = $r.text | ConvertFrom-Json
Check "B pageinfo -> 200" $r.code 200
Check "B claim accepted" $j.claim "True"
Check "B takeover flagged" $j.takeover "True"

$r = Req '/api/status' 'GET' $B
$stB = $r.text | ConvertFrom-Json
Check "B sees owner=true" $stB.owner.owner "True"

$r = Req '/api/status' 'GET' $A
$stA = $r.text | ConvertFrom-Json
Check "A now sees owner=false (will show banner)" $stA.owner.owner "False"
Check "A /api/status still reachable (must NOT be gated)" $r.code 200

Write-Host ""
Write-Host "=== 3) revoked tab A must be refused on control ==="
$r = Req '/api/move'  'POST' $A '{"vx":0,"vy":0,"w":0}'
Check "A /api/move -> 403" $r.code 403
$body = '{"vx":0,"vy":0,"w":0}'
$raw = RawReq ("POST /api/move HTTP/1.1`r`nHost: $Target`r`nX-Page-Id: $A`r`n" +
               "Content-Type: application/json`r`nContent-Length: $($body.Length)`r`n" +
               "Connection: close`r`n`r`n$body")
$jb = (($raw -split "`r`n`r`n", 2)[1]) | ConvertFrom-Json
Check "403 body says taken_over" $jb.taken_over "True"
Check "A /api/cmd -> 403"  (Req '/api/cmd'  'POST' $A '{"c":"stop"}').code 403
Check "A /api/speed -> 403" (Req '/api/speed' 'POST' $A '{"s":50}').code 403
Check "A /api/quality -> 403" (Req '/api/quality' 'POST' $A '{"brightness":50}').code 403
Check "A failsafe timeout change -> 403" (Req '/api/failsafe?t=5' 'GET' $A).code 403

Write-Host ""
Write-Host "=== 4) owner B still works ==="
Check "B /api/move -> 200" (Req '/api/move' 'POST' $B '{"vx":0,"vy":0,"w":0}').code 200
Check "B /api/speed -> 200" (Req '/api/speed' 'POST' $B '{"s":60}').code 200
Check "B read-only status -> 200" (Req '/api/status' 'GET' $B).code 200

Write-Host ""
Write-Host "=== 5) the 'take back' button path ==="
$r = Req '/api/takeover' 'POST' $A '{}'
Check "A takeover -> 200" $r.code 200
Check "A is owner again" (($r.text | ConvertFrom-Json).owner.owner) "True"
Check "B is now revoked" ((Req '/api/status' 'GET' $B).text | ConvertFrom-Json).owner.owner "False"
Check "B /api/move -> 403" (Req '/api/move' 'POST' $B '{"vx":0,"vy":0,"w":0}').code 403
Check "A /api/move -> 200" (Req '/api/move' 'POST' $A '{"vx":0,"vy":0,"w":0}').code 200

Write-Host ""
Write-Host "=== 6) clients with no page id (old page / curl / latency probe) ==="
Check "no-id /api/status -> 200 (read-only ok)" (Req '/api/status' 'GET' $null).code 200
Check "no-id /api/ping   -> 200 (probe must keep working)" (Req '/api/ping' 'GET' $null).code 200
Check "no-id /api/move   -> 403" (Req '/api/move' 'POST' $null '{"vx":0,"vy":0,"w":0}').code 403

Write-Host ""
if ($fails.Count) { Write-Host ("FAILURES: " + ($fails -join ', ')) } else { Write-Host "all takeover policy tests passed" }
