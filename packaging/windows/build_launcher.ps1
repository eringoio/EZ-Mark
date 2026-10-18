Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$OutputRoot = Join-Path $RepoRoot "dist-windows\EZMark"

Set-Location $RepoRoot

Remove-Item -Recurse -Force "build\ezmark-launcher" -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $OutputRoot | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutputRoot "data") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutputRoot "logs") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $OutputRoot "config") | Out-Null

pyinstaller --noconfirm --distpath $OutputRoot --workpath "build\ezmark-launcher" "packaging\windows\ezmark-launcher.spec"

Copy-Item -Force "packaging\windows\sample.local.env" (Join-Path $OutputRoot "config\local.env")

@"
EZ Mark Windows package

Run EZMark.exe to start the local desktop launcher.
Data and logs are stored outside the packaged app when deployed to C:\EZMark.
"@ | Set-Content -Encoding UTF8 (Join-Path $OutputRoot "README.txt")

Write-Host "Launcher package written to $OutputRoot"
