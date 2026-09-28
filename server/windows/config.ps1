# STP加工時間算出ツール Windowsサーバー用の設定（既定値）
# 各スクリプトから読み込まれる。
#
# 社内環境に合わせて変える場合は、このファイルではなく同じフォルダに config.local.ps1 を作り、
# 変えたい行だけ書く（例: $Port = 8080）。config.local.ps1 は更新パッケージで上書きされない。
# テスト用に STP_TOOL_PORT / STP_TOOL_BACKUP_DIR / STP_TOOL_LOG_DIR 環境変数でも上書きできる。

# アプリのフォルダ（このファイルの2つ上）
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

# 待ち受けポート（社内の他サービスと重ならない番号にする）
$Port = 5000

# 待ち受けアドレス。0.0.0.0 = 社内LANの他PCからアクセス可能 / 127.0.0.1 = このサーバー内だけ
$ListenHost = "0.0.0.0"

# waitress の同時処理スレッド数（形状解析はCPUを使うので、CPUコア数程度まで）
$Threads = 4

# 使用する Python のバージョン（.python-version と合わせる）
$PythonVersion = "3.12"

# 自動起動に使うタスクスケジューラのタスク名
$TaskName = "STPTimeTool"

# 履歴DB（SQLite）の場所。変更する場合は既存ファイルも移動すること
$DbPath = Join-Path $RepoRoot "stp_time_tool.sqlite3"

# アップロードファイルの保存日数（0で削除しない）
$UploadRetentionDays = 7

# ログの出力先と、ローテーションするサイズ（MB）
$LogDir = Join-Path $RepoRoot "logs"
$LogRotateMB = 10

# バックアップの保存先と保存世代数
$BackupDir = "C:\STPTool-backup"
$BackupKeep = 30

# 毎日のバックアップ時刻（タスクスケジューラに登録）
$BackupTime = "02:00"
$BackupTaskName = "STPTimeToolBackup"

# ファイアウォールで許可する接続元（LocalSubnet = 同じLANのみ）
$FirewallRemoteAddress = "LocalSubnet"

# 更新時に、更新前のアプリ一式を退避するフォルダ（失敗時の巻き戻し用）
$PreviousDir = "$RepoRoot.prev"

# ---- サーバー固有の設定・環境変数で上書き ----
$localConfig = Join-Path $PSScriptRoot "config.local.ps1"
if (Test-Path $localConfig) { . $localConfig }
if ($env:STP_TOOL_PORT) { $Port = [int]$env:STP_TOOL_PORT }
if ($env:STP_TOOL_BACKUP_DIR) { $BackupDir = $env:STP_TOOL_BACKUP_DIR }
if ($env:STP_TOOL_LOG_DIR) { $LogDir = $env:STP_TOOL_LOG_DIR }

# ---- 上の設定から決まる値（ここは編集しない） ----
$FirewallRuleName = "STP Time Tool (TCP $Port)"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$WheelDir = Join-Path $RepoRoot "wheels"
$HealthUrl = "http://127.0.0.1:$Port/api/health"

# 更新でアプリを差し替えるとき、上書き・削除しないもの（データ・環境・サーバー固有設定）
$PreserveDirs = @(".venv", "uploads", "logs")
$PreserveFiles = @("stp_time_tool.sqlite3*", "config.local.ps1")

# PowerShell 5.1 でも日本語が化けないよう、Python とのやり取りを UTF-8 にする
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$env:PYTHONIOENCODING = "utf-8"
