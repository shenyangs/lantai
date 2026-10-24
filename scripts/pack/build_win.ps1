# Build one isolated, source-bound Windows installer and its actual evidence.
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{40}$')][string]$ExpectedSourceSha,
  [Parameter(Mandatory=$true)][ValidatePattern('^\d+\.\d+\.\d+$')][string]$ExpectedVersion,
  [Parameter(Mandatory=$true)][string]$ExpectedTag,
  [string]$CandidateId,
  [switch]$ArtifactOnly,
  [Parameter(Mandatory=$true)][ValidatePattern('^[a-fA-F0-9]{40}$')][string]$SigningCertificateThumbprint,
  [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{64}$')][string]$ExpectedSignerSha256,
  [Parameter(Mandatory=$true)][ValidatePattern('^https://')][string]$TimestampUrl
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'Windows packaging requires Windows' }
$RepoRoot = (Get-Item $PSScriptRoot).Parent.Parent.FullName
$PackDir = $PSScriptRoot
Set-Location $RepoRoot
$bootstrapPython = (Get-Command python -ErrorAction Stop).Source
& $bootstrapPython scripts/worktree_guard.py check --require-clean --require-no-active-lease
if ($LASTEXITCODE -ne 0) { throw 'Windows build requires clean source without a modification lease' }
if ((& git rev-parse HEAD) -cne $ExpectedSourceSha) { throw 'Source differs from independently expected SHA' }
if ($ExpectedTag -cne "v$ExpectedVersion") { throw 'Expected release tag/version mismatch' }
& $bootstrapPython scripts/check_release_version_coherence.py --project-root $RepoRoot --expected-version $ExpectedVersion
if ($LASTEXITCODE -ne 0) { throw 'Source version coherence failed' }
if ($ArtifactOnly) {
  if ($CandidateId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') { throw 'Artifact-only build requires a safe unique candidate ID' }
} else {
  if ($CandidateId) { throw 'Official build cannot carry a candidate ID' }
  & $bootstrapPython scripts/check_release_source_authority.py --root $RepoRoot --expected-source-sha $ExpectedSourceSha --require-clean
  if ($LASTEXITCODE -ne 0) { throw 'Official Windows source authority failed' }
  $tagSha = & git rev-parse "$ExpectedTag^{commit}"
  if ($LASTEXITCODE -ne 0 -or $tagSha -cne $ExpectedSourceSha) { throw 'Official tag is not the expected main source' }
}
$uv = (Get-Command uv -ErrorAction Stop).Source
$env:UV_PYTHON_DOWNLOADS = 'never'
& $uv sync --frozen --extra dev --extra full
if ($LASTEXITCODE -ne 0) { throw 'Locked source environment installation failed' }
$SourcePython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
Push-Location (Join-Path $RepoRoot 'console')
try {
  npm ci
  if ($LASTEXITCODE -ne 0) { throw 'Console test dependency installation failed' }
} finally { Pop-Location }
# This is the full gate in wheel_build.ps1; check the contract index before
# allocating any output and let the wheel entry run all verifications.
& $SourcePython scripts/check_foundation_experience_gate.py
if ($LASTEXITCODE -ne 0) { throw 'Foundation experience gate failed' }
$buildName = if ($ArtifactOnly) { $CandidateId } else { "official-$ExpectedTag-$($ExpectedSourceSha.Substring(0,12))" }
$Dist = Join-Path (Join-Path $RepoRoot 'dist\windows') $buildName
if (Test-Path -LiteralPath $Dist) { throw 'Refusing to replace any prior Windows build directory' }
New-Item -ItemType Directory -Path $Dist | Out-Null
$WheelDir = Join-Path $Dist 'wheels'
$Inputs = Join-Path $Dist 'pack-inputs'
$Archive = Join-Path $Dist 'lantai-env.zip'
$Unpacked = Join-Path $Dist 'win-unpacked'
$NsiPath = Join-Path $PackDir 'desktop.nsi'
$WheelBuildScript = Join-Path $RepoRoot 'scripts\wheel_build.ps1'
$wheelArgs = @{
  ExpectedSourceSha = $ExpectedSourceSha; ExpectedVersion = $ExpectedVersion
  OutputDir = $WheelDir; PythonExe = $SourcePython
}
if (-not $ArtifactOnly) { $wheelArgs.CanonicalMain = $true }
else { $wheelArgs.CandidateId = $CandidateId }
# Always rebuild from these exact source inputs, even without a version bump.
& $WheelBuildScript @wheelArgs
if ($LASTEXITCODE -ne 0) { throw 'Exact source wheel build failed' }
$wheels = @(Get-ChildItem -LiteralPath $WheelDir -Filter 'lantai-*.whl' -File)
if ($wheels.Count -ne 1) { throw 'Expected exactly one fresh source wheel' }
$packArgs = @((Join-Path $PackDir 'build_common.py'), '--wheel', $wheels[0].FullName,
  '--output', $Archive, '--format', 'zip', '--cache-wheels', '--inputs-dir', $Inputs,
  '--expected-source-sha', $ExpectedSourceSha, '--expected-version', $ExpectedVersion)
& $SourcePython @packArgs
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Archive -PathType Leaf)) { throw 'Locked runtime pack failed' }
Expand-Archive -LiteralPath $Archive -DestinationPath $Unpacked
$EnvRoot = $Unpacked
if (-not (Test-Path -LiteralPath (Join-Path $EnvRoot 'python.exe') -PathType Leaf)) {
  $roots = @(Get-ChildItem -LiteralPath $Unpacked -Directory | Where-Object {
    Test-Path -LiteralPath (Join-Path $_.FullName 'python.exe') -PathType Leaf
  })
  if ($roots.Count -ne 1) { throw 'Packed archive has no unique Python runtime root' }
  $EnvRoot = $roots[0].FullName
}
$pythonExe = Join-Path $EnvRoot 'python.exe'
$CondaUnpack = Join-Path $EnvRoot 'Scripts\conda-unpack.exe'
if (-not (Test-Path -LiteralPath $CondaUnpack -PathType Leaf)) { throw 'Required conda-unpack entry is missing' }
& $CondaUnpack
if ($LASTEXITCODE -ne 0) { throw 'Packed prefix relocation failed' }
$repairLock = Join-Path $Inputs 'repair.lock.txt'
$repairWheels = Join-Path $Inputs 'repair-wheels'
if (-not (Test-Path -LiteralPath $repairLock -PathType Leaf) -or -not (Test-Path -LiteralPath $repairWheels -PathType Container)) {
  throw 'Exact repair lock/cache is missing'
}
$repairArgs = @('-m', 'pip', 'install', '--force-reinstall', '--no-deps', '--no-index',
  '--find-links', $repairWheels, '--require-hashes', '-r', $repairLock,
  '--report', (Join-Path $Inputs 'repair-install.json'))
& $pythonExe @repairArgs
if ($LASTEXITCODE -ne 0) { throw 'Hash-bound conda-unpack repair failed' }
& $pythonExe -c "from huggingface_hub import file_download; import discord, certifi; import guanlan.integrations.mcp_server"
if ($LASTEXITCODE -ne 0) { throw 'Repaired packaged runtime import verification failed' }
& $pythonExe -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Installed runtime dependency consistency failed' }
$installedVersion = & $pythonExe -c "from importlib.metadata import version; print(version('lantai'))"
if ($LASTEXITCODE -ne 0 -or $installedVersion -cne $ExpectedVersion) { throw 'Installed Lantai version differs from expected version' }
& $pythonExe -c "import struct,sys; assert sys.platform == 'win32' and struct.calcsize('P') == 8"
if ($LASTEXITCODE -ne 0) { throw 'Packed runtime is not x64 Windows' }
$Console = Join-Path $EnvRoot 'Lib\site-packages\lantai\console'
Copy-Item -LiteralPath (Join-Path $Console 'release-provenance.json') -Destination (Join-Path $EnvRoot 'release-provenance.json')
& $pythonExe scripts/check_release_product_contract.py --console-dist $Console --expected-sha $ExpectedSourceSha --expected-version $ExpectedVersion
if ($LASTEXITCODE -ne 0) { throw 'Installed product/provenance contract failed' }
& $pythonExe -m compileall -q -j 0 $EnvRoot
if ($LASTEXITCODE -ne 0) { throw 'Packed Python source compilation failed' }

# Main launcher .bat (will be hidden by VBS)
$LauncherBat = Join-Path $EnvRoot "Lantai Desktop.bat"
@"
@echo off
cd /d "%~dp0"

REM Isolate packaged Python from user site-packages to prevent conflicts
set "PYTHONNOUSERSITE=1"

REM Preserve system PATH for accessing system commands
REM Prepend packaged env to PATH so packaged Python takes precedence
set "PATH=%~dp0;%~dp0Scripts;%PATH%"

REM Log level: env var LANTAI_LOG_LEVEL or default to "info"
if not defined LANTAI_LOG_LEVEL set "LANTAI_LOG_LEVEL=info"

REM Set SSL certificate paths for packaged environment
REM Use temp file to avoid for /f blocking issue in bat scripts
set "CERT_TMP=%TEMP%\lantai_cert_%RANDOM%.txt"
"%~dp0python.exe" -u -c "import certifi; print(certifi.where())" > "%CERT_TMP%" 2>nul
set /p CERT_FILE=<"%CERT_TMP%"
del "%CERT_TMP%" 2>nul
if defined CERT_FILE (
  if exist "%CERT_FILE%" (
    set "SSL_CERT_FILE=%CERT_FILE%"
    set "REQUESTS_CA_BUNDLE=%CERT_FILE%"
    set "CURL_CA_BUNDLE=%CERT_FILE%"
  )
)

if not exist "%USERPROFILE%\.lantai\config.json" (
  "%~dp0python.exe" -u -m lantai init --defaults --accept-security
)
"%~dp0python.exe" -u -m lantai desktop --log-level %LANTAI_LOG_LEVEL%
"@ | Set-Content -Path $LauncherBat -Encoding ASCII

# Debug launcher .bat (shows console)
$DebugBat = Join-Path $EnvRoot "Lantai Desktop (Debug).bat"
@"
@echo off
cd /d "%~dp0"

REM Isolate packaged Python from user site-packages to prevent conflicts
set "PYTHONNOUSERSITE=1"

REM Preserve system PATH for accessing system commands
REM Prepend packaged env to PATH so packaged Python takes precedence
set "PATH=%~dp0;%~dp0Scripts;%PATH%"

REM Debug mode: use debug log level by default (can override with LANTAI_LOG_LEVEL)
if not defined LANTAI_LOG_LEVEL set "LANTAI_LOG_LEVEL=debug"

REM Set SSL certificate paths for packaged environment
REM Use temp file to avoid for /f blocking issue in bat scripts
set "CERT_TMP=%TEMP%\lantai_cert_%RANDOM%.txt"
"%~dp0python.exe" -u -c "import certifi; print(certifi.where())" > "%CERT_TMP%" 2>nul
set /p CERT_FILE=<"%CERT_TMP%"
del "%CERT_TMP%" 2>nul
if defined CERT_FILE (
  if exist "%CERT_FILE%" (
    set "SSL_CERT_FILE=%CERT_FILE%"
    set "REQUESTS_CA_BUNDLE=%CERT_FILE%"
    set "CURL_CA_BUNDLE=%CERT_FILE%"
  )
)

echo ====================================
echo Lantai Desktop - Debug Mode
echo ====================================
echo Working Directory: %cd%
echo Python: "%~dp0python.exe"
echo PATH: %PATH%
echo PYTHONNOUSERSITE: %PYTHONNOUSERSITE%
echo Log Level: %LANTAI_LOG_LEVEL%
echo SSL_CERT_FILE: %SSL_CERT_FILE%
echo REQUESTS_CA_BUNDLE: %REQUESTS_CA_BUNDLE%
echo CURL_CA_BUNDLE: %CURL_CA_BUNDLE%
echo.
if not exist "%USERPROFILE%\.lantai\config.json" (
  echo [Init] Creating config...
  "%~dp0python.exe" -u -m lantai init --defaults --accept-security
)
echo [Launch] Starting Lantai Desktop with log-level=%LANTAI_LOG_LEVEL%...
echo Press Ctrl+C to stop
echo.
"%~dp0python.exe" -u -m lantai desktop --log-level %LANTAI_LOG_LEVEL%
echo.
echo [Exit] Lantai Desktop closed
pause
"@ | Set-Content -Path $DebugBat -Encoding ASCII

# VBScript launcher (no console window)
$LauncherVbs = Join-Path $EnvRoot "Lantai Desktop.vbs"
@"
Set WshShell = CreateObject("WScript.Shell")
batPath = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\Lantai Desktop.bat"
WshShell.Run Chr(34) & batPath & Chr(34), 0, False
Set WshShell = Nothing
"@ | Set-Content -Path $LauncherVbs -Encoding ASCII

# Create lantai.cmd wrapper in env root so "lantai" resolves to this
# instead of Scripts\lantai.exe whose embedded Python path may be stale
# after conda-pack/unpack.
$LantaiCmd = Join-Path $EnvRoot "lantai.cmd"
@"
@"%~dp0python.exe" -u -m lantai %*
"@ | Set-Content -Path $LantaiCmd -Encoding ASCII

# Copy icon.ico to env root so NSIS can find it
$IconSrc = Join-Path $PackDir "assets\icon.ico"
if (Test-Path $IconSrc) {
  Copy-Item $IconSrc -Destination $EnvRoot -Force
  Write-Host "[build_win] Copied icon.ico to env root"
} else {
  Write-Host "[build_win] WARN: icon.ico not found at $IconSrc"
}


Write-Host '== Building NSIS installer =='
$numericParts = @($ExpectedVersion.Split('.') | ForEach-Object { [int]$_ })
if (@($numericParts | Where-Object { $_ -gt 65535 }).Count) { throw 'Version cannot fit Windows PE metadata' }
$NumericVersion = "$ExpectedVersion.0"
$OutInstaller = Join-Path $Dist "Lantai-Setup-$ExpectedVersion.exe"
$BuildIdentitySha256 = & $SourcePython -c 'import json,sys; from scripts.release_build_identity import identity_digest; print(identity_digest(json.load(open(sys.argv[1], encoding="utf-8-sig"))))' (Join-Path $EnvRoot 'release-provenance.json')
if ($LASTEXITCODE -ne 0 -or $BuildIdentitySha256 -notmatch '^[a-f0-9]{64}$') { throw 'Actual payload identity digest failed' }
$makensis = (Get-Command makensis -ErrorAction Stop).Source
$nsiArgs = @("/DLANTAI_VERSION=$ExpectedVersion", "/DLANTAI_NUMERIC_VERSION=$NumericVersion",
  "/DLANTAI_SOURCE_SHA=$ExpectedSourceSha", "/DLANTAI_BUILD_IDENTITY_SHA256=$BuildIdentitySha256",
  "/DOUTPUT_EXE=$OutInstaller", "/DUNPACKED=$EnvRoot", $NsiPath)
& $makensis @nsiArgs
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $OutInstaller -PathType Leaf)) { throw 'NSIS installer compilation failed' }
# Sign the final installer, then perform fresh Authenticode and timestamp
# verification before writing the hash-bound artifact-manifest, supply-chain-manifest and sbom.
$signtool = (Get-Command signtool.exe -ErrorAction Stop).Source
& $signtool sign /fd SHA256 /tr $TimestampUrl /td SHA256 /sha1 $SigningCertificateThumbprint $OutInstaller
if ($LASTEXITCODE -ne 0) { throw 'Installer signing failed' }
$SignatureVerifier = Join-Path $RepoRoot 'scripts\verify_windows_release_signature.ps1'
$SigningReceipt = Join-Path $Dist 'lantai-windows-x64-signing-receipt.json'
& $SignatureVerifier -Installer $OutInstaller -ExpectedSourceSha $ExpectedSourceSha -ExpectedVersion $ExpectedVersion -ExpectedSignerSha256 $ExpectedSignerSha256 -ExpectedBuildIdentitySha256 $BuildIdentitySha256 -ReceiptOutput $SigningReceipt
if ($LASTEXITCODE -ne 0) { throw 'Fresh installer signature verification failed' }
$evidenceArgs = @((Join-Path $PackDir 'write_windows_release_evidence.py'),
  '--root', $RepoRoot, '--payload', $EnvRoot, '--inputs', $Inputs, '--output', $Dist,
  '--source-sha', $ExpectedSourceSha, '--version', $ExpectedVersion, '--tag', $ExpectedTag)
if ($ArtifactOnly) { $evidenceArgs += @('--artifact-only', '--candidate-id', $CandidateId) }
& $pythonExe @evidenceArgs
if ($LASTEXITCODE -ne 0) { throw 'Actual installed dependency/binary evidence failed' }
$admissionArgs = @('scripts/check_release_upload.py', 'validate', '--root', $Dist,
  '--source-sha', $ExpectedSourceSha, '--version', $ExpectedVersion, '--arch', 'x64', '--platform', 'windows')
if (-not $ArtifactOnly) {
  $admissionArgs += @('--public', '--authority-root', $RepoRoot, '--windows-signer-sha256', $ExpectedSignerSha256)
}
& $SourcePython @admissionArgs
if ($LASTEXITCODE -ne 0) { throw 'Final Windows release admission failed' }
# Re-read the final signed bytes even for isolated candidates.
& $SignatureVerifier -Installer $OutInstaller -ExpectedSourceSha $ExpectedSourceSha -ExpectedVersion $ExpectedVersion -ExpectedSignerSha256 $ExpectedSignerSha256 -ExpectedBuildIdentitySha256 $BuildIdentitySha256 -Receipt $SigningReceipt
if ($LASTEXITCODE -ne 0) { throw 'Final installer changed after evidence capture' }
& $SourcePython scripts/worktree_guard.py check --require-clean --require-no-active-lease
if ($LASTEXITCODE -ne 0 -or (& git rev-parse HEAD) -cne $ExpectedSourceSha) { throw 'Source changed before Windows build completion' }
Write-Host "Windows installer and bound evidence: $Dist"
