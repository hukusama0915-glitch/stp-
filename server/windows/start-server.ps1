# STP加工時間算出ツールを waitress で起動する（タスクスケジューラから呼ばれる）
# 手動で試す場合: powershell -ExecutionPolicy Bypass -File server\windows\start-server.ps1
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "config.ps1")

if (-not (Test-Path $VenvPython)) {
    throw "仮想環境が見つかりません: $VenvPython  先に setup.ps1 を実行してください。"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$outLog = Join-Path $LogDir "server.out.log"   # 標準出力
$errLog = Join-Path $LogDir "server.log"       # waitress のログ・エラー（標準エラー）
$eventLog = Join-Path $LogDir "events.log"     # 起動・停止の記録

# 起動のたびに前回分を .old に退避する（大きくなりすぎないよう1世代だけ残す）
foreach ($file in @($outLog, $errLog)) {
    if (Test-Path $file) {
        Move-Item -Force $file ($file -replace "\.log$", ".old.log")
    }
}
if ((Test-Path $eventLog) -and ((Get-Item $eventLog).Length -gt $LogRotateMB * 1MB)) {
    Move-Item -Force $eventLog (Join-Path $LogDir "events.old.log")
}

$env:STP_TOOL_DB_PATH = $DbPath
$env:UPLOAD_RETENTION_DAYS = "$UploadRetentionDays"
$env:ENABLE_BREP_ANALYSIS = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

Add-Content -Path $eventLog -Encoding UTF8 -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 起動 $ListenHost`:$Port"
$proc = Start-Process -FilePath $VenvPython -WorkingDirectory $RepoRoot -NoNewWindow -Wait -PassThru `
    -ArgumentList @("-m", "waitress", "--listen=$ListenHost`:$Port", "--threads=$Threads", "app:app") `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog
Add-Content -Path $eventLog -Encoding UTF8 -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 停止 exit $($proc.ExitCode)"
# 0以外で終わった場合はタスクスケジューラの「失敗時に再起動」が働く
exit $proc.ExitCode
