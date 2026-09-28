# 各スクリプト共通の関数

function Assert-Admin {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "管理者として実行してください（PowerShell を右クリック →「管理者として実行」）。"
    }
}

function Stop-ToolServer {
    # タスクを止めても子プロセスの python が残ることがあるため、このリポジトリの waitress も止める
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.ExecutablePath -eq $VenvPython -and $_.CommandLine -like "*waitress*" }
    foreach ($proc in $procs) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($procs) { Start-Sleep -Seconds 2 }
}

function Wait-ToolHealth([int]$TimeoutSec = 90) {
    # 起動直後はカタログ・切削条件の読み込みに時間がかかるので、応答するまで待つ
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $res = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5
            if ($res.ok) { return $res }
        } catch {
            Start-Sleep -Seconds 3
        }
    }
    return $null
}

function Test-AppImport {
    # 一時DBでアプリを読み込み、依存ライブラリと形状解析が動くか確認する（本番DBには触れない）
    $check = @'
import os, sys, tempfile
from pathlib import Path
os.environ["STP_TOOL_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "check.sqlite3")
sys.path.insert(0, ".")
import app
import cadquery
sample = Path("samples/wire_cut_test_plate.stp")
if sample.exists():
    shape = cadquery.importers.importStep(str(sample)).val()
    print("B-Rep解析OK: 体積", round(shape.Volume()))
print("アプリ読み込みOK: version", app.APP_VERSION)
'@
    Push-Location $RepoRoot
    try {
        # 出力は画面にだけ流し、関数の戻り値は合否（真偽値）だけにする
        $check | & $VenvPython - | Out-Host
        return ($LASTEXITCODE -eq 0)
    } finally {
        Pop-Location
    }
}

function Install-Requirements {
    # パッケージに wheels フォルダが同梱されていればそこから（インターネット不要）、無ければ pypi.org から導入する
    $requirements = Join-Path $RepoRoot "requirements.txt"
    if (Test-Path (Join-Path $WheelDir "*.whl")) {
        Write-Host "   同梱のライブラリ（$WheelDir）から導入します"
        & $VenvPython -m pip install --no-index --find-links $WheelDir -r $requirements --quiet --disable-pip-version-check | Out-Host
    } else {
        Write-Host "   インターネット（pypi.org）から導入します"
        & $VenvPython -m pip install -r $requirements --quiet --disable-pip-version-check | Out-Host
    }
    return ($LASTEXITCODE -eq 0)
}

function Copy-AppFiles([string]$Source, [string]$Destination) {
    # アプリ一式を差し替える。データ・仮想環境・サーバー固有設定（$PreserveDirs / $PreserveFiles）は上書きも削除もしない
    $arguments = @($Source, $Destination, "/MIR", "/XD") + $PreserveDirs + @("/XF") + $PreserveFiles +
        @("/R:2", "/W:2", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
    & robocopy @arguments | Out-Null
    # robocopy は 0～7 が成功（8以上が失敗）
    if ($LASTEXITCODE -ge 8) { throw "ファイルのコピーに失敗しました（robocopy $LASTEXITCODE）: $Source → $Destination" }
    $global:LASTEXITCODE = 0
}

function Get-PackageVersion([string]$Root) {
    $versionFile = Join-Path $Root "VERSION.txt"
    if (Test-Path $versionFile) { return (Get-Content $versionFile -Encoding UTF8 | Select-Object -First 1) }
    return "（バージョン情報なし）"
}