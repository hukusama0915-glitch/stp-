# 履歴DB（SQLite）をバックアップする。稼働中でも安全にコピーできるよう SQLite のバックアップ機能を使う。
# 手動実行: powershell -ExecutionPolicy Bypass -File server\windows\backup.ps1
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "config.ps1")

if (-not (Test-Path $DbPath)) {
    Write-Host "履歴DBがまだありません（$DbPath）。バックアップは不要です。"
    exit 0
}

New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dest = Join-Path $BackupDir "stp_time_tool-$stamp.sqlite3"

$script = @'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close()
src.close()
'@
$script | & $VenvPython - $DbPath $dest
if ($LASTEXITCODE -ne 0) { throw "バックアップに失敗しました。" }

# 古い世代を削除する
Get-ChildItem -Path $BackupDir -Filter "stp_time_tool-*.sqlite3" |
    Sort-Object Name -Descending |
    Select-Object -Skip $BackupKeep |
    Remove-Item -Force

Write-Host "バックアップしました: $dest"
