# Lantai installer for Windows PowerShell.
param(
  [string]$Version = "",
  [switch]$FromSource,
  [string]$SourceDir = "",
  [string]$Extras = "",
  [string]$UvPath = "",
  [switch]$Help
)

$ErrorActionPreference = "Stop"

if ($Help) {
  @"
Usage: .\install.ps1 [options]

Options:
  -Version <ver>       Install a specific PyPI version.
  -FromSource          Install from -SourceDir or from LANTAI_REPO.
  -SourceDir <dir>     Local source tree.
  -Extras <extras>     Optional extras, for example: local
  -UvPath <path>       Path to an existing uv.exe.
  -Help                Show this help text.

Environment:
  LANTAI_HOME          Install directory. Default: %USERPROFILE%\.lantai
  LANTAI_REPO          Git URL used with -FromSource when -SourceDir is absent.
"@
  exit 0
}

function Info([string]$Message) { Write-Host "[lantai] $Message" -ForegroundColor Green }
function Warn([string]$Message) { Write-Host "[lantai] WARNING: $Message" -ForegroundColor Yellow }
function Fail([string]$Message) { Write-Host "[lantai] ERROR: $Message" -ForegroundColor Red; exit 1 }

$LantaiHome = if ($env:LANTAI_HOME) { $env:LANTAI_HOME } else { Join-Path $HOME ".lantai" }
$LantaiVenv = Join-Path $LantaiHome "venv"
$LantaiBin = Join-Path $LantaiHome "bin"
$PythonVersion = if ($env:LANTAI_PYTHON_VERSION) { $env:LANTAI_PYTHON_VERSION } else { "3.12" }

function Ensure-Uv {
  if ($UvPath) {
    if (-not (Test-Path $UvPath)) { Fail "uv not found: $UvPath" }
    $env:Path = "$(Split-Path $UvPath -Parent);$env:Path"
    return
  }
  if (Get-Command uv -ErrorAction SilentlyContinue) { return }

  $candidates = @(
    (Join-Path $HOME ".local\bin\uv.exe"),
    (Join-Path $HOME ".cargo\bin\uv.exe"),
    (Join-Path $env:LOCALAPPDATA "uv\uv.exe")
  )
  foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
      $env:Path = "$(Split-Path $candidate -Parent);$env:Path"
      return
    }
  }

  Info "Installing uv"
  try {
    $script = Invoke-RestMethod https://astral.sh/uv/install.ps1 -TimeoutSec 20
    Invoke-Expression $script
  } catch {
    Fail "Failed to install uv automatically. Install uv manually, then rerun."
  }
  $env:Path = "$HOME\.local\bin;$HOME\.cargo\bin;$env:LOCALAPPDATA\uv;$env:Path"
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail "uv is still not available" }
}

function Prepare-Console([string]$RepoDir) {
  $consoleSrc = Join-Path $RepoDir "console\dist"
  $consoleDest = Join-Path $RepoDir "src\lantai\console"
  if (Test-Path (Join-Path $consoleDest "index.html")) { return }
  if (-not (Test-Path (Join-Path $RepoDir "console\package.json"))) { return }
  if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Warn "npm not found; source install will continue without rebuilding console assets"
    return
  }
  Info "Building console frontend"
  Push-Location (Join-Path $RepoDir "console")
  try {
    npm ci
    if ($LASTEXITCODE -ne 0) { Fail "npm ci failed" }
    npm run build
    if ($LASTEXITCODE -ne 0) { Fail "npm run build failed" }
  } finally {
    Pop-Location
  }
  if (Test-Path (Join-Path $consoleSrc "index.html")) {
    if (Test-Path $consoleDest) { Remove-Item $consoleDest -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $consoleDest | Out-Null
    Copy-Item -Path (Join-Path $consoleSrc "*") -Destination $consoleDest -Recurse -Force
  }
}

function Install-Target {
  $extrasSuffix = if ($Extras) { "[$Extras]" } else { "" }
  if ($FromSource) {
    if ($SourceDir) {
      $resolved = (Resolve-Path $SourceDir).Path
      Prepare-Console $resolved
      return "$resolved$extrasSuffix"
    }
    if (-not $env:LANTAI_REPO) { Fail "Set LANTAI_REPO or pass -SourceDir" }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "git is required for source install" }
    $cloneDir = Join-Path $env:TEMP "lantai-install-$(Get-Random)"
    git clone --depth 1 $env:LANTAI_REPO $cloneDir
    if ($LASTEXITCODE -ne 0) { Fail "git clone failed" }
    Prepare-Console $cloneDir
    return "$cloneDir$extrasSuffix"
  }
  if ($Version) { return "lantai==$Version$extrasSuffix" }
  return "lantai$extrasSuffix"
}

Info "Installing Lantai into $LantaiHome"
Ensure-Uv
New-Item -ItemType Directory -Force -Path $LantaiHome, $LantaiBin | Out-Null

uv venv $LantaiVenv --python $PythonVersion --quiet --clear
if ($LASTEXITCODE -ne 0) { Fail "Failed to create virtual environment" }

$python = Join-Path $LantaiVenv "Scripts\python.exe"
$target = Install-Target
Info "Installing $target"
uv pip install $target --python $python --prerelease=allow --refresh-package lantai
if ($LASTEXITCODE -ne 0) { Fail "Installation failed" }

$realBin = Join-Path $LantaiVenv "Scripts\lantai.exe"
if (-not (Test-Path $realBin)) { Fail "lantai CLI not found after install" }

$psWrapper = Join-Path $LantaiBin "lantai.ps1"
@'
$ErrorActionPreference = "Stop"
$LantaiHome = if ($env:LANTAI_HOME) { $env:LANTAI_HOME } else { Join-Path $HOME ".lantai" }
& (Join-Path $LantaiHome "venv\Scripts\lantai.exe") @args
'@ | Set-Content -Path $psWrapper -Encoding UTF8

$cmdWrapper = Join-Path $LantaiBin "lantai.cmd"
@'
@echo off
if "%LANTAI_HOME%"=="" set "LANTAI_HOME=%USERPROFILE%\.lantai"
"%LANTAI_HOME%\venv\Scripts\lantai.exe" %*
'@ | Set-Content -Path $cmdWrapper -Encoding ASCII

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not (($userPath -split ';') -contains $LantaiBin)) {
  [Environment]::SetEnvironmentVariable("Path", "$LantaiBin;$userPath", "User")
  $env:Path = "$LantaiBin;$env:Path"
}

Info "Installed successfully"
Write-Host ""
Write-Host "Run:"
Write-Host "  lantai init"
Write-Host "  lantai app"
