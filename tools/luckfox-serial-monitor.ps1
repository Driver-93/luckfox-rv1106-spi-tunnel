# ============================================================
#  Luckfox Pico MAX 串口读取脚本
#  用法:
#    powershell -ExecutionPolicy Bypass -File .\luckfox-serial-monitor.ps1
#    可选参数:
#      -Port  COM5         串口号 (默认自动检测新端口, 或指定 COM5)
#      -Baud  115200       波特率 (默认 115200)
#      -Log   out.log      日志文件 (默认 luckfox-serial-<时间戳>.log)
#  按 Ctrl+C 停止, 已读内容会保存在日志文件中
# ============================================================
param(
    [string]$Port,
    [int]$Baud = 115200,
    [string]$Log
)

$ErrorActionPreference = 'Stop'

# 未指定端口时, 列出可用端口
if (-not $Port) {
    $ports = [System.IO.Ports.SerialPort]::GetPortNames()
    Write-Host "未指定 -Port 参数。当前可用串口: $($ports -join ', ')"
    Write-Host "示例: .\luckfox-serial-monitor.ps1 -Port COM5 -Baud 115200"
    exit 1
}

if (-not $Log) {
    $Log = "luckfox-serial-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
}

Write-Host ">>> 打开 $Port @ $Baud 8N1, 日志: $Log"
Write-Host ">>> 按 Ctrl+C 停止"

$sp = New-Object System.IO.Ports.SerialPort($Port, $Baud, 'None', 8, 'One')
$sp.ReadTimeout = 1000
$sp.NewLine = "`n"
$sp.Open()

# 清空接收缓冲区中已有的旧数据
Start-Sleep -Milliseconds 200
$sp.DiscardInBuffer()

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$out = New-Object System.Text.StringBuilder

try {
    while ($true) {
        try {
            $line = $sp.ReadLine()
        } catch [System.TimeoutException] {
            continue
        }
        $ts = $sw.Elapsed.ToString('hh\:mm\:ss\.fff')
        $outLine = "[$ts] $line"
        Write-Host $outLine
        [void]$out.AppendLine($outLine)
        # 每 100 行写一次盘, 防止 Ctrl+C 丢失数据
        if (($out.Length -gt 100000) -or ($line -match 'login:|#|~')) {
            [System.IO.File]::AppendAllText((Join-Path (Get-Location) $Log), $out.ToString())
            [void]$out.Clear()
        }
    }
} finally {
    if ($out.Length -gt 0) {
        [System.IO.File]::AppendAllText((Join-Path (Get-Location) $Log), $out.ToString())
    }
    if ($sp.IsOpen) { $sp.Close() }
    Write-Host "`n>>> 已停止, 日志保存于: $(Join-Path (Get-Location) $Log)"
}
