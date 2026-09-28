# 【サーバーで実行】配布用ZIPで最新版に更新する（Git 不要）
#
# 管理者の PowerShell で、アプリのフォルダから実行:
#   powershell -ExecutionPolicy Bypass -File server\windows\update.ps1 -Package \\fileserver\share\stp-time-tool-20260926-abc1234.zip
#
# 1. 履歴DBのバックアップ  2. サーバー停止  3. 更新前のアプリを退避
# 4. アプリ差し替え（履歴DB・アップロード・ログ・仮想環境・config.local.ps1 は残す）
# 5. ライブラリ更新  6. 動作確認（失敗したら退避した版に自動で戻す）  7. 再起動
param(
    [Parameter(Mandatory = $true)][string]$Package
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "config.ps1")
. (Join-Path $PSScriptRoot "lib.ps1")
Add-Type -AssemblyName System.IO.Compression.FileSystem

Assert-Admin
if (-not (Test-Path $Package)) { throw "パッケージが見つかりません: $Package" }
Set-Location $RepoRoot

Write-Host "== 1. パッケージの展開と確認"
$stage = Join-Path ([IO.Path]::GetTempPath()) ("stp-update-" + [Guid]::NewGuid().ToString("N"))
[IO.Compression.ZipFile]::ExtractToDirectory((Resolve-Path $Package).Path, $stage)
try {
    foreach ($required in @("app.py", "requirements.txt", "server\windows\start-server.ps1")) {
        if (-not (Test-Path (Join-Path $stage $required))) {
            throw "このZIPはアプリのパッケージではありません（$required がありません）。サーバーは停止していません。"
        }
    }
    $before = Get-PackageVersion $RepoRoot
    $after = Get-PackageVersion $stage
    Write-Host "   現在: $before"
    Write-Host "   更新: $after"

    Write-Host "== 2. 更新前のバックアップ"
    & (Join-Path $PSScriptRoot "backup.ps1")

    Write-Host "== 3. サーバー停止"
    Stop-ToolServer

    try {
        Write-Host "== 4. 更新前のアプリを退避（$PreviousDir）"
        Copy-AppFiles $RepoRoot $PreviousDir

        Write-Host "== 5. アプリの差し替え"
        Copy-AppFiles $stage $RepoRoot

        Write-Host "== 6. ライブラリ更新"
        $ok = Install-Requirements
        if ($ok) {
            Write-Host "== 7. 動作確認"
            $ok = Test-AppImport
        }
        if (-not $ok) {
            Write-Warning "新しい版の導入・動作確認に失敗したため、更新前の版に戻します。"
            Copy-AppFiles $PreviousDir $RepoRoot
            Install-Requirements | Out-Null
            throw "更新を取り消しました（$before のまま）。上のエラーを確認してください。"
        }
    } finally {
        Write-Host "== 8. サーバー起動"
        Start-ScheduledTask -TaskName $TaskName
    }
} finally {
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
}

$health = Wait-ToolHealth
if (-not $health) { throw "起動確認できませんでした。$LogDir\server.log を確認してください。" }
Write-Host ""
Write-Host "更新完了: $after（アプリ version $($health.version)）"
Write-Host "問題があれば、退避した版（$PreviousDir）から戻せます。手順は README の「更新の取り消し」を参照。"
