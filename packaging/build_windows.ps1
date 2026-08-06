# packaging/build_windows.ps1
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
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

function ConvertTo-WindowsVersionInfoVersion {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Version
  )

  $match = [regex]::Match($Version, '^[vV]?([0-9]+(?:\.[0-9]+){0,3})')
  if (-not $match.Success) {
    throw "VERSION must start with a numeric version for Windows VersionInfoVersion. Current value: $Version"
  }

  $parts = @($match.Groups[1].Value.Split('.') | ForEach-Object { [int]$_ })
  foreach ($part in $parts) {
    if ($part -lt 0 -or $part -gt 65535) {
      throw "Windows version component out of range 0..65535 in VERSION: $Version"
    }
  }
  while ($parts.Count -lt 4) {
    $parts += 0
  }
  return ($parts[0..3] -join ".")
}

$env:WINDOWS_VERSION_INFO_VERSION = ConvertTo-WindowsVersionInfoVersion $env:VERSION
Write-Host "Windows VersionInfoVersion: $env:WINDOWS_VERSION_INFO_VERSION"

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
& packaging\out\staffdeck\staffdeck.exe --packaging-smoke
Assert-NativeCommandSucceeded "Packaged Lark SDK smoke test"

$signingConfigured = Test-SigningConfigured
if ($signingConfigured) {
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

Write-Host "==> [4b/6] Bundle SRT + Node runtime"
if (Test-Path packaging\sandbox_runtime) { Remove-Item -Recurse -Force packaging\sandbox_runtime }
if (Test-Path packaging\out\staffdeck\sandbox) { Remove-Item -Recurse -Force packaging\out\staffdeck\sandbox }
& $PY.Command @($PY.PrefixArgs) packaging\fetch_sandbox_runtime.py packaging\sandbox_runtime
Assert-NativeCommandSucceeded "SRT runtime preparation"
Copy-Item -Recurse -Force packaging\sandbox_runtime packaging\out\staffdeck\sandbox
& $PY.Command @($PY.PrefixArgs) packaging\smoke_sandbox_bundle.py packaging\out\staffdeck\sandbox
Assert-NativeCommandSucceeded "Final SRT bundle smoke test"

if ($signingConfigured) {
  Write-Host "Signing bundled Windows executable payload"
  $signableExtensions = @(".exe", ".dll", ".pyd", ".node")
  $signableFiles = Get-ChildItem packaging\out\staffdeck -Recurse -File |
    Where-Object { $signableExtensions -contains $_.Extension.ToLowerInvariant() }
  foreach ($file in $signableFiles) {
    & packaging\sign_windows.ps1 -FilePath $file.FullName
    Assert-NativeCommandSucceeded "Payload signing: $($file.FullName)"
  }
}

& packaging\out\staffdeck\runtime\python.exe -c `
  "import ssl, sqlite3, requests, docx, openpyxl; print(sqlite3.sqlite_version)"
Assert-NativeCommandSucceeded "Final bundled Python runtime smoke test"

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
$unsignedInstaller = "packaging\out\StaffDeck-setup.exe"
if (Test-Path $unsignedInstaller) {
  Remove-Item -Force $unsignedInstaller
}
if ($signingConfigured) {
  $signScript = (Resolve-Path packaging\sign_windows.ps1).Path
  $signCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$signScript`" -FilePath `$f"
  & "$iscc" "/Sstaffdeck=$signCommand" packaging\installer\ultrarag.iss
} else {
  & "$iscc" packaging\installer\ultrarag.iss
}
Assert-NativeCommandSucceeded "Inno Setup build"
if (-not (Test-Path $unsignedInstaller)) {
  throw "Inno Setup completed without producing $unsignedInstaller."
}

Write-Host "==> [6/6] Name the release artifact"
$out = "packaging\out\StaffDeck-windows-x64-setup.exe"
if (Test-Path -LiteralPath $out) { Remove-Item -LiteralPath $out -Force }
Move-Item -LiteralPath $unsignedInstaller -Destination $out
if ($signingConfigured) {
  $signature = Get-AuthenticodeSignature $out
  if ($signature.Status -ne "Valid") {
    throw "Final installer signature is not valid: $($signature.StatusMessage)"
  }
  Write-Host "Authenticode signature valid: $($signature.SignerCertificate.Subject)"
}
Write-Host "built $out"
Get-ChildItem packaging\out\StaffDeck-windows-x64-setup.exe
