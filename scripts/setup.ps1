# Windows setup for WSLC. Does not install Harbor or require host Node.js.
# Run from anywhere: powershell -File scripts/setup.ps1
$ErrorActionPreference = "Stop"

function Fail($Message) {
    Write-Host ""
    Write-Host "Setup error: $Message"
    exit 1
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root
if (-not (Test-Path "pyproject.toml")) { Fail "Run this script from a complete source checkout." }

if (-not (Get-Command wslc -ErrorAction SilentlyContinue)) { Fail "wslc is missing. Update WSL and open a new terminal." }
& wslc version
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "Install Git before setup." }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail "Install uv 0.11.11 and reopen the terminal." }

Write-Host ""
Write-Host "==> Installing Python 3.12 and locked project dependencies"
& uv python install 3.12
if ($LASTEXITCODE -ne 0) { Fail "Python install failed." }
$env:UV_PROJECT_ENVIRONMENT = Join-Path $Root ".venv"
& uv sync --python 3.12 --extra test --locked
if ($LASTEXITCODE -ne 0) { Fail "Dependency install failed." }

Write-Host ""
Write-Host "==> Building the game runtime with wslc"
& wslc build -f docker/runtime.Dockerfile -t rsi-gamecreator/runtime:full-v1 docker
if ($LASTEXITCODE -ne 0) { Fail "Runtime image build failed." }
Write-Host ""
Write-Host "==> Building the role image with wslc"
& wslc build -t game-agent-lite/codex:0.153.4-godot-v1 docker
if ($LASTEXITCODE -ne 0) { Fail "Role image build failed." }

Write-Host ""
Write-Host "==> Checking the isolated engine"
& wslc run --rm --pull=never --network none --entrypoint godot rsi-gamecreator/runtime:full-v1 --version
if ($LASTEXITCODE -ne 0) { Fail "Godot did not start in the runtime image." }
& wslc run --rm --pull=never --network none --entrypoint codex game-agent-lite/codex:0.153.4-godot-v1 --version
if ($LASTEXITCODE -ne 0) { Fail "Codex did not start in the role image." }

$Agent = Join-Path $Root ".venv\Scripts\game-agent.exe"
if (-not (Test-Path $Agent)) { $Agent = Join-Path $Root ".venv\Scripts\game-agent" }
& $Agent --help | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "game-agent --help failed." }
$Smoke = Join-Path $Root (".cache\setup-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $Smoke | Out-Null
try {
    & $Agent start examples/mock.json --run-dir (Join-Path $Smoke "run")
    if ($LASTEXITCODE -ne 0) { Fail "Offline smoke test failed." }
} finally {
    if (Test-Path $Smoke) { Remove-Item -Recurse -Force $Smoke }
}

Write-Host ""
Write-Host "Setup complete. Activate the environment in each new terminal:"
Write-Host "  .venv\Scripts\Activate.ps1"
Write-Host "Use examples/godot-wslc.json for a container run. Copy it to a local config before adding credentials."
