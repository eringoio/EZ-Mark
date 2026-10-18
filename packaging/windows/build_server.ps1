Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$OutputRoot = Join-Path $RepoRoot "dist-windows\EZMark"
$ServerOutput = Join-Path $OutputRoot "server"

Set-Location $RepoRoot

Remove-Item -Recurse -Force "build\ezmark-server" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $ServerOutput -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $ServerOutput | Out-Null

pyinstaller --noconfirm --distpath $OutputRoot --workpath "build\ezmark-server" "packaging\windows\ezmark-server.spec"

Write-Host "Server package written to $ServerOutput"
