# packaging/build_windows.ps1
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
if (-not $env:VERSION) { $env:VERSION = "0.1.0" }

function Test-SigningConfigured {
  return [bool]($env:WINDOWS_CERT_THUMBPRINT -or $env:WINDOWS_PFX_PATH)
}

function Assert-NativeCommandSucceeded {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Step
  )

  if ($LASTEXITCODE -ne 0) {
    throw "$Step failed with exit code $LASTEXITCODE."
  }
}

# A py.exe launcher can exist without an installed Python runtime. Probe each
# candidate instead of only checking whether the launcher is on PATH.
function Get-PythonCommand {
  $candidates = @(
    [pscustomobject]@{ Command = $env:PYTHON; PrefixArgs = @() },
    [pscustomobject]@{ Command = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"; PrefixArgs = @() },
    [pscustomobject]@{ Command = "python"; PrefixArgs = @() },
    [pscustomobject]@{ Command = "py"; PrefixArgs = @("-3.11") },
    [pscustomobject]@{ Command = "py"; PrefixArgs = @("-3") }
  )

  foreach ($candidate in $candidates) {
    if (-not $candidate.Command) { continue }
    if (-not (Get-Command $candidate.Command -ErrorAction SilentlyContinue)) { continue }
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $candidate.Command @($candidate.PrefixArgs) -c "import sys; assert sys.version_info >= (3, 11)" 2>$null
    $probeExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
    if ($probeExitCode -eq 0) { return $candidate }
  }

  throw "Python 3.11 or newer is required. Install it and rerun this script."
}

$PY = Get-PythonCommand
Write-Host "Using Python: $($PY.Command) $($PY.PrefixArgs -join ' ')"

Write-Host "==> [1/6] Build frontend"
if (-not (Test-Path frontend-enterprise\node_modules)) {
  npm ci --prefix frontend-enterprise --no-audit --no-fund
  Assert-NativeCommandSucceeded "npm ci"
}
npm --prefix frontend-enterprise run build
Assert-NativeCommandSucceeded "Frontend build"

Write-Host "==> [2/6] Create backend venv and install packaging dependencies"
& $PY.Command @($PY.PrefixArgs) -m venv backend\.venv
Assert-NativeCommandSucceeded "Backend virtual environment creation"
backend\.venv\Scripts\python -m pip install -U pip
Assert-NativeCommandSucceeded "pip upgrade"
# Extract runtime dependencies from pyproject without installing the project itself.
Push-Location backend
$deps = .\.venv\Scripts\python -c "import tomllib,pathlib; print('\n'.join(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['dependencies']))"
Assert-NativeCommandSucceeded "Runtime dependency extraction"
$deps | Out-File -Encoding utf8 ..\packaging\_win_reqs.txt
Pop-Location
backend\.venv\Scripts\python -m pip install -r packaging\_win_reqs.txt
Assert-NativeCommandSucceeded "Backend dependency installation"
backend\.venv\Scripts\python -m pip install "pyinstaller>=6.6.0" "certifi>=2024.2.2"
Assert-NativeCommandSucceeded "Packaging dependency installation"

Write-Host "==> [3/6] Build PyInstaller application"
Push-Location backend
.\.venv\Scripts\pyinstaller ..\packaging\ultrarag.spec --noconfirm --distpath ..\packaging\out --workpath ..\packaging\build
Assert-NativeCommandSucceeded "PyInstaller build"
Pop-Location

$signingConfigured = Test-SigningConfigured
if ($signingConfigured) {
  Write-Host "Signing staffdeck.exe"
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File packaging\sign_windows.ps1 `
    -FilePath packaging\out\staffdeck\staffdeck.exe
  Assert-NativeCommandSucceeded "staffdeck.exe signing"
  $env:WINDOWS_SIGN_ENABLED = "1"
} else {
  $env:WINDOWS_SIGN_ENABLED = "0"
  Write-Warning "Code signing is not configured; Windows artifacts will be UNSIGNED."
}

Write-Host "==> [4/6] Bundle the Python skill runtime"
backend\.venv\Scripts\python packaging\fetch_runtime_python.py packaging\runtime_dl --expect-arch x86_64
Assert-NativeCommandSucceeded "Python skill runtime download"
if (Test-Path packaging\out\staffdeck\runtime) { Remove-Item -Recurse -Force packaging\out\staffdeck\runtime }
Copy-Item -Recurse -Force packaging\runtime_dl\python packaging\out\staffdeck\runtime

Write-Host "==> [5/6] Build the Inno Setup installer"
$isccCandidates = @(
  $env:ISCC,
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ }
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
  throw "Inno Setup 6 was not found. Install it or set ISCC to the full path of ISCC.exe."
}
Write-Host "Using Inno Setup: $iscc"
$unsignedInstaller = "packaging\out\URStaff-setup.exe"
if (Test-Path $unsignedInstaller) {
  Remove-Item -Force $unsignedInstaller
}
if ($signingConfigured) {
  $signScript = (Resolve-Path packaging\sign_windows.ps1).Path
  $signCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$signScript`" -FilePath `$f"
  & "$iscc" "/Surstaff=$signCommand" packaging\installer\ultrarag.iss
} else {
  & "$iscc" packaging\installer\ultrarag.iss
}
Assert-NativeCommandSucceeded "Inno Setup build"
if (-not (Test-Path $unsignedInstaller)) {
  throw "Inno Setup completed without producing $unsignedInstaller."
}

Write-Host "==> [6/6] Name the release artifact"
$out = "packaging\out\URStaff-$($env:VERSION)-windows-x64-setup.exe"
if (Test-Path $out) { Remove-Item -Force $out }
Rename-Item $unsignedInstaller $out
if ($signingConfigured) {
  $signature = Get-AuthenticodeSignature $out
  if ($signature.Status -ne "Valid") {
    throw "Final installer signature is not valid: $($signature.StatusMessage)"
  }
  Write-Host "Authenticode signature valid: $($signature.SignerCertificate.Subject)"
}
Write-Host "built $out"
Get-ChildItem packaging\out\URStaff-*-windows-x64-setup.exe
