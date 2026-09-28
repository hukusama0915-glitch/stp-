# 【開発PCで実行】サーバーへ渡す配布用ZIPを作る（サーバー側に Git は不要）
#
#   powershell -ExecutionPolicy Bypass -File server\windows\make-package.ps1              # アプリのみ（数MB）
#   powershell -ExecutionPolicy Bypass -File server\windows\make-package.ps1 -WithWheels  # ライブラリ同梱（約200MB）
#
# -WithWheels: サーバーがインターネット（pypi.org）に出られない場合に使う。
#              ライブラリをこのPCで取得して同梱する（サーバーと同じ Windows 64bit / Python 3.12 用）。
# 出力先: dist\stp-time-tool-<日付>-<コミット>.zip
param(
    [switch]$WithWheels,
    [string]$OutDir
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "config.ps1")
. (Join-Path $PSScriptRoot "lib.ps1")
Add-Type -AssemblyName System.IO.Compression.FileSystem

Set-Location $RepoRoot
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "このスクリプトは Git のある開発PCで実行してください。" }
if (-not $OutDir) { $OutDir = Join-Path $RepoRoot "dist" }

$commit = (git rev-parse --short HEAD).Trim()
$subject = (git log -1 --format=%s).Trim()
$committed = (git log -1 --format=%ci).Trim()
$dirty = git status --porcelain --untracked-files=no
if ($dirty) {
    Write-Warning "コミットされていない変更があります。パッケージに入るのはコミット済みの内容（$commit）だけです。"
}

$stage = Join-Path ([IO.Path]::GetTempPath()) ("stp-package-" + [Guid]::NewGuid().ToString("N"))
$appDir = Join-Path $stage "STPTool"
New-Item -ItemType Directory -Force -Path $appDir | Out-Null
try {
    Write-Host "== 1. アプリ一式（コミット $commit）"
    $srcZip = Join-Path $stage "src.zip"
    git archive --format=zip -o $srcZip HEAD
    if ($LASTEXITCODE -ne 0) { throw "git archive に失敗しました。" }
    [IO.Compression.ZipFile]::ExtractToDirectory($srcZip, $appDir)
    Remove-Item $srcZip

    $suffix = ""
    if ($WithWheels) {
        Write-Host "== 2. ライブラリの同梱（時間がかかります）"
        & $VenvPython -m pip download -r (Join-Path $appDir "requirements.txt") -d (Join-Path $appDir "wheels") `
            --only-binary=:all: --quiet --disable-pip-version-check | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "ライブラリの取得に失敗しました。" }
        $suffix = "-offline"
    }

    $version = "$commit $committed $subject"
    Set-Content -Path (Join-Path $appDir "VERSION.txt") -Value $version -Encoding UTF8

    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
    $zipPath = Join-Path $OutDir ("stp-time-tool-{0}-{1}{2}.zip" -f (Get-Date -Format "yyyyMMdd"), $commit, $suffix)
    if (Test-Path $zipPath) { Remove-Item $zipPath }
    Write-Host "== 3. ZIP作成"
    [IO.Compression.ZipFile]::CreateFromDirectory($appDir, $zipPath)
    $sizeMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
    Write-Host ""
    Write-Host "作成しました: $zipPath（$sizeMB MB）"
    Write-Host "バージョン: $version"
} finally {
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
}
