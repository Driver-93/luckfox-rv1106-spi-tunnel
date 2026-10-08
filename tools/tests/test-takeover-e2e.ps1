# End-to-end test of the single-client takeover, including the mediamtx kick.
#
# Unlike the unit tests (which stub the mediamtx API), this creates a REAL
# WebRTC session by POSTing a synthetic SDP offer to the WHEP endpoint, then
# verifies that claiming ownership from another page actually kicks it.
#
# That covers the parts a stub cannot: the real API payload shape
# (v1.11.3 returns {"itemCount":..,"items":[...]}, not a bare array), the real
# field name for the peer address, and the real kick endpoint.
$ErrorActionPreference = 'Continue'
$Target = '192.168.3.69'
$board  = '192.168.3.65'   # the only live address on this PC right now

function RawHttp($hostname, $port, $requestText) {
    $c = New-Object System.Net.Sockets.TcpClient
    $c.Connect($hostname, $port)
    $s = $c.GetStream()
    $b = [Text.Encoding]::ASCII.GetBytes($requestText)
    $s.Write($b, 0, $b.Length); $s.Flush()
    $sr = New-Object IO.StreamReader($s)
    $all = $sr.ReadToEnd()
    $c.Close()
    $parts = $all -split "`r`n`r`n", 2
    $head = $parts[0]
    $body = if ($parts.Count -gt 1) { $parts[1] } else { "" }
    $status = 0
    if ($head -match '^HTTP/1\.[01] (\d+)') { $status = [int]$Matches[1] }
    return @{ status = $status; head = $head; body = $body }
}

# A minimal but structurally valid recvonly video offer. pion (mediamtx's
# WebRTC stack) only needs a well-formed offer to accept the session.
$sdp = @(
  'v=0',
  'o=- 0 0 IN IP4 127.0.0.1',
  's=-',
  't=0 0',
  'a=group:BUNDLE 0',
  'a=msid-semantic: WMS',
  'm=video 9 UDP/TLS/RTP/SAVPF 96',
  'c=IN IP4 0.0.0.0',
  'a=rtcp:9 IN IP4 0.0.0.0',
  'a=ice-ufrag:abcdefgh',
  'a=ice-pwd:abcdefghijklmnopqrstuvwxyz',
  'a=ice-options:trickle',
  'a=fingerprint:sha-256 11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00',
  'a=setup:actpass',
  'a=mid:0',
  'a=recvonly',
  'a=rtcp-mux',
  'a=rtpmap:96 H264/90000',
  'a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f',
  ''
) -join "`r`n"

Write-Host "=== 1) create a real WebRTC session via WHEP ==="
$req = "POST /car/whep HTTP/1.1`r`nHost: ${Target}:8889`r`n" +
       "Content-Type: application/sdp`r`nContent-Length: $($sdp.Length)`r`n" +
       "Connection: close`r`n`r`n$sdp"
$r = RawHttp $Target 8889 $req
Write-Host ("  WHEP POST -> HTTP {0}" -f $r.status)
if ($r.status -ne 201) {
    Write-Host "  body: $($r.body.Substring(0, [Math]::Min(300, $r.body.Length)))"
    Write-Host "  (cannot create a session this way -- stopping)"
    exit 2
}
$loc = ''
if ($r.head -match '(?im)^Location:\s*(\S+)') { $loc = $Matches[1].Trim() }
Write-Host ("  session Location: {0}" -f $loc)

Write-Host ""
Write-Host "=== 2) does it show up? (query the board's mediamtx API) ==="
$adb = "C:\Users\pcX\Documents\luckfox-flash\platform-tools\platform-tools\adb.exe"
function BoardList {
    $raw = (& $adb shell "wget -q -O - http://127.0.0.1:9997/v3/webrtcsessions/list" 2>&1 | Out-String).Trim()
    return $raw
}
Start-Sleep -Seconds 2
$raw = BoardList
Write-Host ("  raw: {0}" -f $raw.Substring(0, [Math]::Min(260, $raw.Length)))
$obj = $null
try { $obj = $raw | ConvertFrom-Json } catch {}
$items = $null
if ($obj) { $items = $obj.items }
$count = if ($items) { @($items).Count } else { 0 }
Write-Host ("  live sessions: {0}" -f $count)
if ($count -gt 0) {
    Write-Host ("  id={0}  remoteAddr={1}" -f $items[0].id, $items[0].remoteAddr)
}

Write-Host ""
Write-Host "=== 3) a NEW tab claims ownership -> the old session must be kicked ==="
$claim = '{"page":"e2e-test"}'
$req2 = "POST /api/pageinfo HTTP/1.1`r`nHost: $Target`r`nX-Page-Id: e2e-newtab`r`n" +
        "Content-Type: application/json`r`nContent-Length: $($claim.Length)`r`n" +
        "Connection: close`r`n`r`n$claim"
$r2 = RawHttp $Target 80 $req2
Write-Host ("  pageinfo -> HTTP {0}" -f $r2.status)

Start-Sleep -Seconds 3   # enforce_loop ticks once a second
$after = BoardList
$obj2 = $null
try { $obj2 = $after | ConvertFrom-Json } catch {}
$n2 = if ($obj2 -and $obj2.items) { @($obj2.items).Count } else { 0 }
Write-Host ("  live sessions after takeover: {0}" -f $n2)
if ($n2 -lt $count) {
    Write-Host "  RESULT: the old session WAS kicked by the takeover"
} else {
    Write-Host "  RESULT: session still alive -- enforcement did not fire"
}
Write-Host ""
Write-Host "--- board log (kick lines) ---"
& $adb shell "grep -E 'owner' /tmp/web.log | tail -6" 2>&1 | Out-String
