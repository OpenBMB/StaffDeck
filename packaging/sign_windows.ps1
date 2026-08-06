param(
  [Parameter(Mandatory = $true)]
  [string]$FilePath
)

$ErrorActionPreference = "Stop"

function Get-SignTool {
  if ($env:SIGNTOOL_EXE -and (Test-Path $env:SIGNTOOL_EXE)) {
    return (Resolve-Path $env:SIGNTOOL_EXE).Path
  }

  $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }

  $kitsRoot = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
  if (Test-Path $kitsRoot) {
    $candidate = Get-ChildItem $kitsRoot -Filter signtool.exe -File -Recurse |
      Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
      Sort-Object FullName -Descending |
      Select-Object -First 1
    if ($candidate) { return $candidate.FullName }
  }

  throw "signtool.exe was not found. Install the Windows SDK or set SIGNTOOL_EXE."
}

$target = (Resolve-Path $FilePath).Path
$timestampUrl = if ($env:WINDOWS_TIMESTAMP_URL) {
  $env:WINDOWS_TIMESTAMP_URL
} else {
  "http://timestamp.digicert.com"
}

if ($env:WINDOWS_SIGNER_SCRIPT) {
  $signerScript = (Resolve-Path $env:WINDOWS_SIGNER_SCRIPT).Path
  if ($signerScript -eq $PSCommandPath) {
    throw "WINDOWS_SIGNER_SCRIPT cannot point to packaging/sign_windows.ps1."
  }
  & $signerScript -FilePath $target
  if ($LASTEXITCODE -ne 0) {
    throw "External Windows signer failed for $target with exit code $LASTEXITCODE"
  }
} else {
  $signtool = Get-SignTool
  $arguments = @("sign", "/fd", "SHA256", "/tr", $timestampUrl, "/td", "SHA256")
  if ($env:WINDOWS_PFX_PATH) {
    $pfxPath = (Resolve-Path $env:WINDOWS_PFX_PATH).Path
    $arguments += @("/f", $pfxPath)
    if ($env:WINDOWS_PFX_PASSWORD) {
      $arguments += @("/p", $env:WINDOWS_PFX_PASSWORD)
    }
  } elseif ($env:WINDOWS_CERT_THUMBPRINT) {
    $thumbprint = $env:WINDOWS_CERT_THUMBPRINT -replace "\s", ""
    $arguments += @("/sha1", $thumbprint)
  } else {
    throw "Set WINDOWS_CERT_THUMBPRINT, WINDOWS_PFX_PATH, or WINDOWS_SIGNER_SCRIPT before signing."
  }

  $arguments += $target
  & $signtool @arguments
  if ($LASTEXITCODE -ne 0) {
    throw "signtool failed for $target with exit code $LASTEXITCODE"
  }
}

$signature = Get-AuthenticodeSignature -FilePath $target
if ($signature.Status -ne "Valid") {
  throw "Authenticode verification failed for ${target}: $($signature.StatusMessage)"
}

Write-Host "Signed and verified: $target ($($signature.SignerCertificate.Subject))"
