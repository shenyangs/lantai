# Verify fresh installed bytes on Windows. A transported receipt is an input
# to compare, never a substitute for Windows trust/certificate/version checks.
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$Installer,
  [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{40}$')][string]$ExpectedSourceSha,
  [Parameter(Mandatory=$true)][ValidatePattern('^\d+\.\d+\.\d+$')][string]$ExpectedVersion,
  [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{64}$')][string]$ExpectedSignerSha256,
  [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{64}$')][string]$ExpectedBuildIdentitySha256,
  [string]$Receipt,
  [string]$ReceiptOutput
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
  throw 'Authenticode release verification requires the actual Windows OS'
}
if ([bool]$Receipt -eq [bool]$ReceiptOutput) { throw 'Specify exactly one receipt input or output' }
$file = Get-Item -LiteralPath $Installer
if ($file.PSIsContainer -or ($file.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
  throw 'Installer must be a regular file'
}
$version = [Diagnostics.FileVersionInfo]::GetVersionInfo($file.FullName)
if ($version.ProductVersion -ne $ExpectedVersion -or $version.FileVersion -ne $ExpectedVersion -or
    $version.Comments -cne "source=$ExpectedSourceSha;identity=$ExpectedBuildIdentitySha256") { throw 'Installer PE source/version/build identity mismatch' }
$signature = Get-AuthenticodeSignature -LiteralPath $file.FullName
if ($signature.Status.ToString() -ne 'Valid' -or -not $signature.SignerCertificate -or
    -not $signature.TimeStamperCertificate) { throw 'Installer signature or timestamp is not valid' }
$algorithm = [Security.Cryptography.SHA256]::Create()
try {
  $signerDigest = ([BitConverter]::ToString($algorithm.ComputeHash($signature.SignerCertificate.RawData))).Replace('-', '').ToLowerInvariant()
} finally { $algorithm.Dispose() }
if ($signerDigest -cne $ExpectedSignerSha256) { throw 'Signer differs from independently approved certificate' }
$signtool = (Get-Command signtool.exe -ErrorAction Stop).Source
& $signtool verify /pa /all /tw $file.FullName
if ($LASTEXITCODE -ne 0) { throw "signtool verify failed: $LASTEXITCODE" }
$digest = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
$observed = [ordered]@{
  schema = 'com.lantai.windows-signing-evidence.v1'
  source = @{ sha = $ExpectedSourceSha }
  release = @{ version = $ExpectedVersion; arch = 'x64'; platform = 'win32' }
  artifact = @{ name = $file.Name; path = $file.FullName; bytes = $file.Length; sha256 = $digest }
  status = 'Valid'; verificationExitCode = 0; timestampPresent = $true
  signerCertificateSha256 = $signerDigest
  productVersion = $version.ProductVersion; fileVersion = $version.FileVersion
  buildIdentitySha256 = $ExpectedBuildIdentitySha256
}
if ($Receipt) {
  $old = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
  if ($old.schema -ne $observed.schema -or $old.testDouble -eq $true -or
      $old.source.sha -cne $ExpectedSourceSha -or $old.release.version -ne $ExpectedVersion -or
      $old.release.arch -ne 'x64' -or $old.release.platform -ne 'win32' -or
      $old.artifact.name -cne $file.Name -or $old.artifact.sha256 -cne $digest -or
      $old.artifact.bytes -ne $file.Length -or $old.status -ne 'Valid' -or
      $old.verificationExitCode -ne 0 -or $old.timestampPresent -ne $true -or
      $old.signerCertificateSha256 -cne $signerDigest -or
      $old.buildIdentitySha256 -cne $ExpectedBuildIdentitySha256 -or
      $old.productVersion -ne $version.ProductVersion -or $old.fileVersion -ne $version.FileVersion) {
    throw 'Transported signing receipt differs from fresh Windows verification'
  }
} else {
  if (Test-Path -LiteralPath $ReceiptOutput) { throw 'Refusing to overwrite an existing signing receipt' }
  $observed | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReceiptOutput -Encoding utf8
}
Write-Host 'Windows signature, timestamp, signer, source and PE version verified'
