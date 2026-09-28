# STP加工時間算出ツールを Windows サーバーにセットアップする（初回・設定変更時）
#
# 管理者の PowerShell で、リポジトリのフォルダから実行:
#   powershell -ExecutionPolicy Bypass -File server\windows\setup.ps1
#
# オプション:
#   -SkipTask         自動起動タスクを登録しない（動作確認だけしたいとき）
#   -SkipFirewall     ファイアウォールの許可ルールを作らない
#   -SkipBackupTask   毎日のバックアップタスクを登録しない
param(
    [switch]$SkipTask,
    [switch]$SkipFirewall,
    [switch]$SkipBackupTask
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "config.ps1")
. (Join-Path $PSScriptRoot "lib.ps1")

if (-not ($SkipTask -and $SkipFirewall -and $SkipBackupTask)) { Assert-Admin }
Set-Location $RepoRoot

Write-Host "== 1. Python $PythonVersion の確認"
$basePython = $null
try {
    $basePython = (& py "-$PythonVersion" -c "import sys; print(sys.executable)") 2>$null
} catch {
    $basePython = $null
}
if (-not $basePython) {
    throw "Python $PythonVersion が見つかりません。python.org から Windows 用 Python $PythonVersion（64bit）をインストールし、「py launcher」も有効にしてから再実行してください。"
}
Write-Host "   $basePython"

# ライブラリ（casadi 等）はフォルダ階層が深く、Windows の既定のパス長上限（260文字）を超えることがある
$longPaths = (Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -ErrorAction SilentlyContinue).LongPathsEnabled
if ($longPaths -ne 1 -and $RepoRoot.Length -gt 40) {
    throw ("アプリのフォルダのパスが長すぎます（{0}文字: {1}）。D:\STPTool のような短い場所に置くか、" +
        "Windows の長いパスを有効にしてください（グループポリシー「Win32 の長いパスを有効にする」、" +
        "または管理者で Set-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem LongPathsEnabled 1）。") -f $RepoRoot.Length, $RepoRoot
}
Write-Host "== 2. 仮想環境とライブラリ"
if (-not (Test-Path $VenvPython)) {
    & py "-$PythonVersion" -m venv (Join-Path $RepoRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "仮想環境の作成に失敗しました。" }
}
# pip 自体の更新はインターネットがある場合だけ行う（失敗しても続行）
if (-not (Test-Path (Join-Path $WheelDir "*.whl"))) { & $VenvPython -m pip install --upgrade pip --quiet --disable-pip-version-check }
if (-not (Install-Requirements)) { throw "ライブラリの導入に失敗しました。上の pip のエラーを確認してください（インターネットに出られないサーバーでは、ライブラリ同梱版のパッケージを使います）。" }

Write-Host "== 3. 動作確認（一時DBで読み込みテスト）"
if (-not (Test-AppImport)) { throw "アプリの読み込みテストに失敗しました。上のエラーを確認してください。" }

if (-not $SkipTask) {
    Write-Host "== 4. 自動起動タスクの登録（$TaskName）"
    Stop-ToolServer
    $startScript = Join-Path $PSScriptRoot "start-server.ps1"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$startScript`"" `
        -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    # 異常終了したら1分後に再起動、実行時間の上限なし
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description "STP加工時間算出ツール（waitress, port $Port）" -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    $health = Wait-ToolHealth
    if ($health) {
        Write-Host "   起動しました: version $($health.version)"
    } else {
        Write-Warning "起動確認できませんでした。$LogDir\server.log を確認してください。"
    }
}

if (-not $SkipBackupTask) {
    Write-Host "== 5. 毎日のバックアップタスク（$BackupTaskName、$BackupTime）"
    $backupScript = Join-Path $PSScriptRoot "backup.ps1"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$backupScript`"" -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -Daily -At $BackupTime
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $BackupTaskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description "STP加工時間算出ツールの履歴DBバックアップ（$BackupDir）" -Force | Out-Null
    Write-Host "   保存先: $BackupDir（$BackupKeep 世代）"
}

if (-not $SkipFirewall) {
    Write-Host "== 6. ファイアウォール（TCP $Port、接続元 $FirewallRemoteAddress）"
    Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $FirewallRuleName -Direction Inbound -Protocol TCP -LocalPort $Port `
        -RemoteAddress $FirewallRemoteAddress -Profile Domain, Private -Action Allow | Out-Null
}

$hostName = [System.Net.Dns]::GetHostName()
Write-Host ""
Write-Host "セットアップ完了。社内のPCから http://$hostName`:$Port/ を開いてください。"
