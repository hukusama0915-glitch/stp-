# 自動起動・バックアップのタスクとファイアウォール許可を削除する。
# 履歴DB・アップロード・バックアップのファイルは削除しない。
#   powershell -ExecutionPolicy Bypass -File server\windows\uninstall.ps1
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "config.ps1")
. (Join-Path $PSScriptRoot "lib.ps1")

Assert-Admin
Stop-ToolServer
foreach ($name in @($TaskName, $BackupTaskName)) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "タスクを削除しました: $name"
    }
}
Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Host "ファイアウォールの許可を削除しました: $FirewallRuleName"
Write-Host "データ（$DbPath、uploads、$BackupDir）は残しています。"
