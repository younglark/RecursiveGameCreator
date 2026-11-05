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

function ImageId($Image) {
    $json = & wslc image inspect $Image -f json 2>$null | Out-String
    if ($LASTEXITCODE -ne 0 -or -not $json.Trim()) { return "" }
    try { return (@($json | ConvertFrom-Json) | Select-Object -First 1).Id } catch { return "" }
}

function BuildImage($Name, $Arguments) {
    $before = ImageId $Name
    & wslc @Arguments
    $code = $LASTEXITCODE
    $after = ImageId $Name
    if ($after -and $after -ne $before) {
        # wslc returns 1 when a build logs warnings even after exporting the image.
        if ($code -ne 0) { Write-Host "wslc build returned $code, but $Name was rebuilt; continuing." }
        return
    }
    if ($code -ne 0) { Fail "$Name build failed." }
}

Write-Host ""
Write-Host "==> Building the game runtime with wslc"
BuildImage "rsi-gamecreator/runtime:full-v1" @("build", "-f", "docker/runtime.Dockerfile", "-t", "rsi-gamecreator/runtime:full-v1", "docker")
Write-Host ""
Write-Host "==> Building the role image with wslc"
BuildImage "game-agent-lite/codex:0.153.4-godot-v1" @("build", "-t", "game-agent-lite/codex:0.153.4-godot-v1", "docker")

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
