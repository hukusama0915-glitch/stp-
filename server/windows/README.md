# STP加工時間算出ツール Windowsサーバー運用手順

社内の Windows サーバーでツールを常時稼働させるための手順です。

- サーバーに **Git は不要** です。開発PCで作った配布用ZIPを、共有フォルダやUSBでサーバーへ渡します
- サーバーがインターネットに出られなくても、ライブラリ同梱版のZIPで導入・更新できます
- アプリは waitress（本番用Webサーバー）で動かし、Windows 標準のタスクスケジューラで
  サーバー起動時に自動で立ち上げます（外部ツールのダウンロードは不要）

| スクリプト | 実行する場所 | 用途 | 管理者権限 |
|---|---|---|---|
| `make-package.ps1` | 開発PC | 配布用ZIPの作成 | 不要 |
| `setup.ps1` | サーバー | 初回セットアップ（ライブラリ導入・自動起動・バックアップ・ファイアウォール） | 必要 |
| `update.ps1` | サーバー | ZIPで最新版に更新（失敗時は自動で元に戻す） | 必要 |
| `backup.ps1` | サーバー | 履歴DBのバックアップ（毎日自動でも実行） | 不要 |
| `start-server.ps1` | サーバー | サーバー本体の起動（タスクから呼ばれる） | 不要 |
| `uninstall.ps1` | サーバー | 自動起動・バックアップ・ファイアウォール設定の削除（データは残す） | 必要 |
| `config.ps1` | ― | 設定の既定値（直接は編集しない。下記の `config.local.ps1` を使う） | ― |

## 1. 配布用ZIPを作る（開発PC）

コミット済みの内容からZIPを作ります。

```powershell
cd <開発PCのリポジトリ>
# サーバーがインターネット（pypi.org）に出られる場合: アプリのみ（1MB未満）
powershell -ExecutionPolicy Bypass -File server\windows\make-package.ps1
# 出られない場合: ライブラリ同梱（約200MB）
powershell -ExecutionPolicy Bypass -File server\windows\make-package.ps1 -WithWheels
```

`dist\stp-time-tool-<日付>-<コミット>.zip`（同梱版は末尾が `-offline`）ができます。
中の `VERSION.txt` にコミットと日時が入り、更新時の表示に使われます。

- 初回セットアップは、サーバーがインターネットに出られる場合でも同梱版を使うと、ライブラリのダウンロードが不要になり確実です
- 同梱されるライブラリは開発PCと同じ Windows 64bit / Python 3.12 用です。サーバーもこれに合わせてください

## 2. サーバーの事前準備

1. **Python 3.12（64bit）** をインストールする（python.org の Windows 用インストーラ）
   - 「py launcher」にチェックを入れる（`py -3.12` で起動できる状態にする）
   - 「Add python.exe to PATH」は不要
   - サーバーがインターネットに出られない場合は、インストーラをUSB等で持ち込む
2. アプリを置く場所を決める。**`D:\STPTool` のような短いパス** にする
   - ライブラリ（casadi 等）のフォルダ階層が深く、Windows の既定のパス長上限（260文字）を超えやすいため
   - 長い場所に置きたい場合は、Windows の長いパスを有効にする（グループポリシー「Win32 の長いパスを有効にする」）

## 3. 初回セットアップ（サーバー）

1. ZIPを `D:\STPTool` に展開する（右クリック →「すべて展開」、展開先に `app.py` が直接並ぶようにする）
2. 環境に合わせて設定を変える場合は、`server\windows\config.local.ps1` を作り、変えたい行だけ書く
   （例: `$Port = 8080`、`$BackupDir = "E:\STPTool-backup"`。変えられる項目は `config.ps1` を参照）
3. 管理者として PowerShell を開いて実行する

```powershell
cd D:\STPTool
powershell -ExecutionPolicy Bypass -File server\windows\setup.ps1
```

`setup.ps1` が行うこと:

1. Python 3.12 の確認と、パスの長さの確認
2. 仮想環境（`.venv`）の作成とライブラリ導入（同梱版なら `wheels` フォルダから、インターネット不要）
3. 動作確認（一時DBでアプリを読み込み、サンプルSTPの形状解析を実行。本番DBには触れない）
4. 自動起動タスク `STPTimeTool` の登録と起動（SYSTEM で実行、異常終了時は1分後に自動再起動）
5. 毎日のバックアップタスク `STPTimeToolBackup` の登録（既定 2:00、30世代）
6. ファイアウォールの許可（既定: TCP 5000、同じLANからのみ）

最後に表示される `http://<サーバー名>:5000/` を社内のPCのブラウザで開いて確認します。

動作確認だけ先に行いたい場合は、登録系を省略できます（管理者権限も不要）:

```powershell
powershell -ExecutionPolicy Bypass -File server\windows\setup.ps1 -SkipTask -SkipFirewall -SkipBackupTask
```

## 4. 更新（新しい版を反映する）

新しいZIPを開発PCで作り、サーバーから見える場所（共有フォルダ等）に置いて、管理者の PowerShell で実行します。

```powershell
cd D:\STPTool
powershell -ExecutionPolicy Bypass -File server\windows\update.ps1 -Package \\fileserver\share\stp-time-tool-20260926-abc1234.zip
```

1. ZIPを展開して中身を確認（アプリのパッケージでなければ、何もせず中止）
2. 履歴DBをバックアップ
3. サーバー停止
4. 更新前のアプリ一式を `D:\STPTool.prev` に退避
5. アプリを差し替え（**履歴DB・アップロード・ログ・仮想環境・`config.local.ps1` は残す**）
6. ライブラリ更新 → 動作確認
7. 導入・動作確認に失敗した場合は、**退避した版に自動で戻して** 再起動
8. 再起動して応答を確認

更新中（通常1〜2分）はツールを使えません。
アプリのフォルダ（`D:\STPTool`）には、アプリ以外のファイルを置かないでください（更新時に削除されます）。

### 更新の取り消し（手動）

更新は成功したが、使ってみて問題があった場合は、退避した版に戻します（管理者の PowerShell）:

```powershell
cd D:\STPTool
. .\server\windows\config.ps1; . .\server\windows\lib.ps1
Stop-ToolServer
Copy-AppFiles $PreviousDir $RepoRoot
Install-Requirements
Start-ScheduledTask -TaskName $TaskName
```

退避されるのは直前の1世代だけです。履歴DBを更新前に戻す場合は「5. バックアップと復元」を参照してください。

## 5. バックアップと復元

- 自動: 毎日 `$BackupTime`（既定 2:00）に `$BackupDir`（既定 `C:\STPTool-backup`）へ保存（`$BackupKeep` 世代）
- 手動: `powershell -ExecutionPolicy Bypass -File server\windows\backup.ps1`
- 更新時にも自動で1つ保存します
- 稼働中でも SQLite のバックアップ機能で安全にコピーします
- 保存されるのは履歴DB（解析結果・機械マスタ等）です。アップロードされたSTPは保存期間（既定7日）で自動削除されるため対象外です

復元する場合（管理者の PowerShell）:

```powershell
cd D:\STPTool
. .\server\windows\config.ps1; . .\server\windows\lib.ps1
Stop-ToolServer
Copy-Item C:\STPTool-backup\stp_time_tool-YYYYMMDD-HHMMSS.sqlite3 $DbPath -Force
Start-ScheduledTask -TaskName $TaskName
```

## 6. ログと状態確認

| ファイル | 内容 |
|---|---|
| `logs\events.log` | 起動・停止の記録 |
| `logs\server.log` | アプリのエラー（解析失敗時のトレースバック等） |
| `logs\*.old.log` | 前回起動時のログ（起動のたびに1世代退避） |

```powershell
Get-ScheduledTask -TaskName STPTimeTool | Select-Object TaskName, State
Invoke-RestMethod http://127.0.0.1:5000/api/health    # ok と version が返れば稼働中
Get-Content D:\STPTool\VERSION.txt                     # 導入中のパッケージ
```

## 7. トラブルシューティング

| 症状 | 確認すること |
|---|---|
| セットアップで「パスが長すぎます」 | アプリを `D:\STPTool` など短い場所に置く（2. の説明を参照） |
| ライブラリ導入に失敗 | インターネットに出られない場合は同梱版ZIPを使う。プロキシ環境では pip のプロキシ設定が必要 |
| 他のPCから開けない | ファイアウォール（`$FirewallRemoteAddress`）、サーバー名の名前解決、`$ListenHost` が `0.0.0.0` か |
| 起動確認できない | `logs\server.log` のエラー。ポートが他サービスと重複していないか（`netstat -ano \| findstr :5000`） |
| 解析が遅い・同時に使うと待たされる | `$Threads`（CPUコア数程度まで）。形状解析は1件で数秒〜数十秒CPUを使う |
| スクリプトの日本語が文字化けする | スクリプトを「UTF-8 (BOM 付き)」のまま保存しているか。`config.local.ps1` も BOM 付きで保存する |

## 8. セキュリティ上の注意

- ツールにはログイン機能がありません。**社内LANの中だけで使う**前提です（ファイアウォールは同じLANからのみ許可）
- 顧客のCADデータを扱うため、社外に公開する場合は Cloudflare Tunnel と Cloudflare Access（ログイン認証）の併用を推奨します。サーバー側でポートを開ける必要がなく、アプリの改修も不要です
- タスクは SYSTEM アカウントで動くため、アプリのフォルダは管理者以外が書き換えられないようにしてください

## 9. 削除

```powershell
powershell -ExecutionPolicy Bypass -File server\windows\uninstall.ps1
```

自動起動・バックアップのタスクとファイアウォールの許可を削除します。履歴DB・アップロード・バックアップのファイルは残るので、不要なら手動で削除してください。
