# Control-path latency probe through the C5 SPI tunnel.
#
# Path: PC --WiFi--> ESP32-C5 (192.168.3.69) --SPI tunnel--> RV1106 board :80
#
# Why raw TcpClient and not Invoke-WebRequest: IWR's own overhead measured
# 65-86ms p50 on a path whose true p50 is ~30ms. It invalidates the result.
# Keep-alive + raw stream read measures the path, not PowerShell.
param(
    [int]$Samples = 60,
    [string]$Target = "192.168.3.69",
    [int]$Port = 80,
    [string]$Path = "/api/ping",
    [int]$GapMs = 60
)

$ErrorActionPreference = 'Continue'

$conn = New-Object System.Net.Sockets.TcpClient
$conn.NoDelay = $true
try {
    $t = $conn.ConnectAsync($Target, $Port)
    if (-not $t.Wait(6000)) { throw "connect timeout" }
} catch {
    Write-Host "connect failed: $($_.Exception.Message)"
    exit 1
}
$stream = $conn.GetStream()
$req = [Text.Encoding]::ASCII.GetBytes("GET $Path HTTP/1.1`r`nHost: $Target`r`nConnection: keep-alive`r`n`r`n")

$times = New-Object System.Collections.ArrayList
for ($i = 0; $i -lt $Samples; $i++) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $stream.Write($req, 0, $req.Length)
        $buf = New-Object byte[] 4096
        $n = $stream.Read($buf, 0, $buf.Length)
        $sw.Stop()
        if ($n -gt 0) { [void]$times.Add($sw.Elapsed.TotalMilliseconds) } else { break }
    } catch {
        $sw.Stop()
        Write-Host "  read error at sample $i : $($_.Exception.Message)"
        break
    }
    Start-Sleep -Milliseconds $GapMs
}
$conn.Close()

if ($times.Count -lt 3) { Write-Host "too few samples ($($times.Count))"; exit 1 }

$s = $times | Sort-Object
function P($a, $p) { $a[[Math]::Min($a.Count - 1, [int][Math]::Floor($a.Count * $p))] }
Write-Host ("n={0}  min={1:N1}  p50={2:N1}  p90={3:N1}  p99={4:N1}  max={5:N1}  avg={6:N1}" -f `
    $times.Count, $s[0], (P $s 0.50), (P $s 0.90), (P $s 0.99), $s[-1], ($times | Measure-Object -Average).Average)
