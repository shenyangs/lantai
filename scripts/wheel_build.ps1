# Build the exact source wheel and sdist with an isolated hash-pinned backend.
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$ExpectedSourceSha,
  [Parameter(Mandatory=$true)][string]$ExpectedVersion,
  [Parameter(Mandatory=$true)][string]$OutputDir,
  [string]$PythonExe = 'python',
  [switch]$CanonicalMain,
  [string]$CandidateId
)
$ErrorActionPreference = 'Stop'
$RepoRoot = (Get-Item $PSScriptRoot).Parent.FullName
Set-Location $RepoRoot
& $PythonExe scripts/worktree_guard.py check --require-clean --require-no-active-lease
if ($LASTEXITCODE -ne 0) { throw 'Wheel build requires clean source without a modification lease' }
if ((& git rev-parse HEAD) -cne $ExpectedSourceSha) { throw 'Wheel source differs from independent expected SHA' }
& $PythonExe scripts/check_release_version_coherence.py --project-root $RepoRoot --expected-version $ExpectedVersion
if ($LASTEXITCODE -ne 0) { throw 'Source versions disagree' }
if ($CanonicalMain) {
  & $PythonExe scripts/check_release_source_authority.py --root $RepoRoot --expected-source-sha $ExpectedSourceSha --require-clean
  if ($LASTEXITCODE -ne 0) { throw 'Canonical-main source authority failed' }
}
& $PythonExe scripts/check_foundation_experience_gate.py --run-verifications
if ($LASTEXITCODE -ne 0) { throw 'Foundation experience verification failed' }
$ConsoleDir = Join-Path $RepoRoot 'console'
Push-Location $ConsoleDir
try {
  npm ci
  if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }
  npm run build
  if ($LASTEXITCODE -ne 0) { throw 'npm run build failed' }
} finally { Pop-Location }
$identityArgs = @('scripts/write_release_build_identity.py', '--root', $RepoRoot,
  '--output', (Join-Path $ConsoleDir 'dist/release-provenance.json'), '--version', $ExpectedVersion,
  '--source-sha', $ExpectedSourceSha)
if ($CanonicalMain) { $identityArgs += '--canonical-main' }
else { $identityArgs += @('--artifact-only', '--candidate-id', $CandidateId) }
& $PythonExe @identityArgs
if ($LASTEXITCODE -ne 0) { throw 'Build provenance failed' }
# Keep this shared static-fresh entry; it checks both new and stale assets.
& $PythonExe scripts/check_console_static_fresh.py --sync
if ($LASTEXITCODE -ne 0) { throw 'Embedded console sync failed' }
& $PythonExe scripts/check_release_product_contract.py --console-dist (Join-Path $RepoRoot 'src/lantai/console') --expected-sha $ExpectedSourceSha --expected-version $ExpectedVersion
if ($LASTEXITCODE -ne 0) { throw 'Wheel product contract failed' }
$uv = (Get-Command uv -ErrorAction Stop).Source
& $PythonExe scripts/pack/locked_runtime_inputs.py build-wheel --root $RepoRoot --output $OutputDir --uv $uv --requirements (Join-Path $PSScriptRoot 'pack/packaging-tools.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Pinned wheel build failed' }
$wheels = @(Get-ChildItem -LiteralPath $OutputDir -Filter 'lantai-*.whl' -File)
if ($wheels.Count -ne 1) { throw 'Expected exactly one wheel from this build' }
& $PythonExe scripts/pack/locked_runtime_inputs.py verify-wheel --wheel $wheels[0].FullName --source-sha $ExpectedSourceSha --version $ExpectedVersion
if ($LASTEXITCODE -ne 0) { throw 'Wheel identity validation failed' }
Write-Host "Wheel and sdist built in $OutputDir"
